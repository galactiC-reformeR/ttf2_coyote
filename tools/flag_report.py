"""分析 flag_watch.py 产出的 JSON，找出标志位并翻译成插件可直接填的地址格式。

为什么要有这个工具：回合结束/进局会让成千上万个字节同时变化，实时打印只能看到噪声。
JSON 里存的是**逐地址完整变化记录**（不受打印额度限制），所以分析要基于它做。

用法::

    python tools/flag_report.py --json tools/flags/session2.json
    python tools/flag_report.py --json tools/flags/session2.json --rise-only --top 30
    python tools/flag_report.py --json tools/flags/session2.json --t-min 30 --t-max 90

输出：
* 血量轨迹（把你做的动作和内存变化对齐起来）；
* 候选地址：只跳 1–3 次、且变化方向符合"标志位"特征的那些；
* 每个候选的**插件可用写法**：
  - 落在血量锚点区 → `engine.dll+0x……`（静态地址，重启不变）；
  - 落在某条链指向的结构里 → `"client.dll"+XXXX -> YYYY`（原链末段偏移已加好）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import source  # noqa: E402
import flag_watch  # noqa: E402

CFG = {"mem_module": "auto"}
ANCHOR_OFFSET = int("111E1E68", 16)


def region_forms(anchor_span: int, chain_span: int, cache: str | None = None):
    """返回 (anchor_base, [(区域名, 起始地址, 长度)])。

    分析需要游戏进程活着才能解析链地址；但**旧数据要在很久以后还能分析**（比如游戏已重启），
    所以支持把「这次会话的窗口布局」缓存下来复用 —— 绝对地址只对同一局游戏会话有效。
    """
    if cache and os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        return saved["anchor"], [tuple(r) for r in saved["regions"]]
    ms = source.MemorySource(lambda k, d=None: CFG.get(k, d))
    if not ms._ensure_open():
        raise SystemExit("打开进程失败：%s（首次分析需要游戏仍在运行；之后可用缓存）"
                         % ms._last_error)
    regions, anchor = flag_watch.build_regions(ms, anchor_span, chain_span)
    if cache:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"anchor": anchor, "regions": regions}, fh, ensure_ascii=False, indent=1)
    return anchor, regions


def form_for(addr: int, anchor: int, regions, anchor_span: int, chain_span: int) -> str:
    """把绝对地址翻译成插件配置里能直接用的写法。"""
    if anchor - anchor_span // 2 <= addr < anchor + anchor_span // 2:
        return "engine.dll+%X" % (ANCHOR_OFFSET + (addr - anchor))
    for name, start, span in regions:
        if name.startswith("血量锚点区") or not (start <= addr < start + span):
            continue
        extra = addr - (start + chain_span // 2)          # 相对链解析出的地址的偏移
        module, offsets = source.parse_chain_full(name)
        if not module or not offsets:
            continue
        if len(offsets) == 1:                             # 静态地址：直接加偏移
            return "%s+%X" % (module, offsets[0] + extra)
        # 链形如 "mod"+head -> last：解析 = deref(base+head) + last，
        # 所以"再加 extra"等价于把末段偏移写成 last+extra（不能多一级解引用）。
        return '"%s"+%X -> %X' % (module, offsets[0], offsets[-1] + extra)
    return "不在已知窗口内"


def value_at(items, t: float):
    """某时刻的字节值：最后一次变化（≤t）之后的值；若 t 早于第一次变化则取第一次的"变化前"值。"""
    prev = None
    for t0, a, b in items:
        if t0 <= t:
            prev = b
        else:
            return a if prev is None else prev
    return prev


def window_const(items, s: float, e: float):
    """窗口 [s,e] 内保持不变时返回值，否则返回 None（窗口内变过 / 完全无记录）。"""
    value = value_at(items, s)
    if value is None:
        return None
    for t0, _a, _b in items:
        if s < t0 <= e:
            return None
    return value


def state_diff(data: dict, a: tuple, b: tuple, top: int, out: str) -> None:
    """稳态差分：只在"状态 A 窗口"和"状态 B 窗口"内各自保持恒定、且两者取值不同的字节。

    这是标定最硬的判据 —— 瞬时闪烁、枚举抖动、批量变化全都不会同时满足这两个条件。
    """
    rows = []
    for key, items in (data.get("changes") or {}).items():
        try:
            addr = int(key.split("@0x")[-1], 16)
        except ValueError:
            continue
        va = window_const(items, a[0], a[1])
        vb = window_const(items, b[0], b[1])
        if va is None or vb is None or va == vb:
            continue
        rows.append((va, vb, addr, key.split("@0x")[0]))
    rows.sort(key=lambda r: (abs(r[1] - r[0]), r[2]))
    print("\n===== 稳态差分 A[%.1f,%.1f] vs B[%.1f,%.1f]：%d 个字节取值不同 =====" % (
        a[0], a[1], b[0], b[1], len(rows)))
    anchor, regions = region_forms(0x2000, 0x400)
    print("%-12s %-12s %-28s %s" % ("A 窗口值", "B 窗口值", "插件写法", "所属窗口"))
    fh = open(out, "w", encoding="utf-8") if out else None
    try:
        for va, vb, addr, name in rows[:top]:
            line = "%-12s %-12s %-28s %s" % (
                va, vb, form_for(addr, anchor, regions, 0x2000, 0x400), name[:40])
            print(line)
            if fh:
                fh.write(line + "\n")
    finally:
        if fh:
            fh.close()
    if out:
        print("已写入 %s（共 %d 行）" % (out, min(top, len(rows))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--anchor-span", type=lambda s: int(s, 0), default=0x2000)
    ap.add_argument("--chain-span", type=lambda s: int(s, 0), default=0x400)
    ap.add_argument("--t-min", type=float, default=0.0)
    ap.add_argument("--t-max", type=float, default=1e9)
    ap.add_argument("--rise-only", action="store_true", help="只看 0→非0 的变化")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--regions-cache", default=os.path.join(HERE, "flags", "regions.json"),
                    help="缓存本次会话的窗口布局，游戏重启后仍可分析旧数据")
    ap.add_argument("--out", default="", help="把报告同时写入文件")
    ap.add_argument("--state-a", default="", help="稳态 A 窗口，如 150,190")
    ap.add_argument("--state-b", default="", help="稳态 B 窗口，如 205,215")
    args = ap.parse_args()

    with open(args.json, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    print("数据 %s：%s 秒 / %s 轮" % (os.path.basename(args.json), data.get("seconds"), data.get("rounds")))
    print("\n===== 血量锚点轨迹（用来对齐你做的动作） =====")
    for t, a, b in data.get("health", []):
        print("  %7.2fs  %d → %d" % (t, a, b))

    if args.state_a and args.state_b:
        sa = tuple(float(x) for x in args.state_a.split(","))
        sb = tuple(float(x) for x in args.state_b.split(","))
        state_diff(data, sa, sb, args.top, args.out)
        return 0

    anchor, regions = region_forms(args.anchor_span, args.chain_span, args.regions_cache)

    rows = []
    for key, items in (data.get("changes") or {}).items():
        try:
            addr = int(key.split("@0x")[-1], 16)
        except ValueError:
            continue
        picked = [it for it in items if args.t_min <= it[0] <= args.t_max]
        if not picked:
            continue
        if args.rise_only:
            picked = [it for it in picked if it[1] == 0 and it[2] != 0]
            if not picked:
                continue
        rows.append((len(picked), addr, key.split("@0x")[0], picked))

    rows.sort(key=lambda r: (r[0], r[1]))
    print("\n===== 候选（按变化次数升序） =====")
    print("%-10s %-26s %-34s %s" % ("次数", "插件写法", "所属窗口", "变化"))
    for n, addr, name, picked in rows[:args.top]:
        trail = "  ".join("%.1fs %d→%d" % (t, a, b) for t, a, b in picked[:6])
        print("%-10d %-26s %-34s %s" % (n, form_for(addr, anchor, regions, args.anchor_span,
                                                     args.chain_span), name[:34], trail))
    print("\n共 %d 个地址在时间窗内有变化" % len(rows))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("数据 %s，窗口 [%.1f, %.1f]s，rise-only=%s\n" % (args.json, args.t_min, args.t_max, args.rise_only))
            for t, a, b in data.get("health", []):
                fh.write("血量锚点 %7.2fs  %d → %d\n" % (t, a, b))
            for n, addr, name, picked in rows:
                fh.write("%-6d %-30s %-34s %s\n" % (
                    n, form_for(addr, anchor, regions, args.anchor_span, args.chain_span), name[:34],
                    "  ".join("%.1fs %d→%d" % (t, a, b) for t, a, b in picked[:8])))
        print("报告已写入 %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
