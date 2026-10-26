"""标志位标定：只读轮询一批内存窗口，记录"哪个字节在什么时候变了"。

用途：找 `doomed`（濒死）/ `alive`（存活）/ `m25`（死亡界面）/ `m20`（处决动画）这类布尔标志。
思路：这些标志一定和已知数据在**同一个结构**里或附近，所以同时盯住：

* 已知静态地址 `engine.dll+111E1E68`（泰坦血量）周围的整块内存；
* `tools/chains.txt` 里那 28 条静态链解析出的结构（上次验证过都落在泰坦相关数据上）。

你在游戏里按顺序做动作（进濒死 / 泰坦被打爆 / 铁驭阵亡 …），工具按时间线记录每个字节的
跳变；做完后看"变化明细"，某个"只跳一次、然后一直保持"的字节就是你要的标志位。

只读：仅 PROCESS_VM_READ | PROCESS_QUERY_INFORMATION，绝不写入游戏内存。

用法::

    python tools/flag_watch.py --label doom1 --minutes 6
    python tools/flag_watch.py --label all --minutes 8 --anchor-span 0x2000 --chain-span 0x400
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import source  # noqa: E402

CFG = {"mem_module": "auto"}
HEALTH_ANCHOR = "engine.dll+111E1E68"


def build_regions(ms, anchor_span: int, chain_span: int):
    """返回 [(名称, 绝对地址, 长度)]。"""
    regions = []

    module, offsets = source.parse_chain_full(HEALTH_ANCHOR)
    base = ms._resolve(offsets, module)
    if not base:
        raise RuntimeError("找不到血量锚点 %s" % HEALTH_ANCHOR)
    regions.append(("血量锚点区 %s" % HEALTH_ANCHOR, base - anchor_span // 2, anchor_span))
    anchor_abs = base

    chain_file = os.path.join(HERE, "chains.txt")
    with open(chain_file, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    for line in lines:
        try:
            module, offsets = source.parse_chain_full(line)
            addr = ms._resolve(offsets, module or ms._proc_name)
        except Exception:  # noqa: BLE001
            continue
        if addr:
            regions.append((line, addr - chain_span // 2, chain_span))

    return regions, anchor_abs


def dump_json(path: str, label: str, rounds: int, seconds: float, history: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"label": label, "rounds": rounds, "seconds": round(seconds, 1),
                   "health": history.get("血量锚点", []),
                   "changes": {k: v for k, v in history.items() if k != "血量锚点"}},
                  fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def fmt_bytes(before: bytes, after: bytes, index: int) -> str:
    import struct
    width = 4
    if index + width <= len(before):
        i_before = struct.unpack_from("<i", before, index)[0]
        i_after = struct.unpack_from("<i", after, index)[0]
        f_before = struct.unpack_from("<f", before, index)[0]
        f_after = struct.unpack_from("<f", after, index)[0]
        extra = " i32 %d→%d" % (i_before, i_after)
        if abs(f_before) < 1e7 and abs(f_after) < 1e7 and (f_before != f_after):
            extra += " f32 %.3f→%.3f" % (f_before, f_after)
    else:
        extra = ""
    return extra


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--minutes", type=float, default=6.0)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--anchor-span", type=lambda s: int(s, 0), default=0x2000)
    ap.add_argument("--chain-span", type=lambda s: int(s, 0), default=0x400)
    ap.add_argument("--max-print", type=int, default=3, help="同一地址最多打印几次（超过视为噪音）")
    ap.add_argument("--print-cap", type=int, default=600, help="总共最多打印多少行（其余只进 JSON）")
    ap.add_argument("--bulk-bytes", type=int, default=48, help="一轮内变化超过这么多字节视为批量变化")
    ap.add_argument("--snapshot-every", type=float, default=10.0, help="每隔多少秒把 JSON 落盘一次")
    ap.add_argument("--outdir", default=os.path.join(HERE, "flags"))
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    log_path = os.path.join(args.outdir, "%s.log" % args.label)
    json_path = os.path.join(args.outdir, "%s.json" % args.label)

    ms = source.MemorySource(lambda k, d=None: CFG.get(k, d))
    if not ms._ensure_open():
        print("打开进程失败：%s" % ms._last_error)
        return 1
    regions, anchor = build_regions(ms, args.anchor_span, args.chain_span)
    print("进程 %s (PID %d)，模块 engine.dll=0x%X" % (ms._proc_name, ms._pid, ms._module_base("engine.dll")))
    print("盯住 %d 个窗口（血量锚点 ±0x%X，每条链 ±0x%X）" % (len(regions), args.anchor_span // 2, args.chain_span // 2))
    for name, addr, span in regions[:4]:
        print("  %-42s 0x%X .. 0x%X" % (name, addr, addr + span))
    print("  … 其余 %d 个窗口略" % max(0, len(regions) - 4))

    log = open(log_path, "w", encoding="utf-8")
    log.write("# 标志位标定记录 label=%s 开始 %s\n" % (args.label, time.strftime("%H:%M:%S")))

    prev = {}
    history = {}
    deadline = time.monotonic() + args.minutes * 60
    t0 = time.monotonic()
    health_prev = None
    rounds = 0
    changes = 0
    printed = [0]
    last_dump = time.monotonic()

    while time.monotonic() < deadline:
        rounds += 1
        rel = time.monotonic() - t0
        for name, addr, span in regions:
            raw = ms._read_bytes(addr, span)
            if not raw or len(raw) != span:
                continue
            old = prev.get(addr)
            prev[addr] = raw
            if old is None or len(old) != len(raw):
                continue
            if old == raw:
                continue
            # 先数一遍本区域这一轮变了多少字节：进局/换关会让成千上万个字节同时变，
            # 这种"批量变化"只记一行摘要，不把打印额度耗光（否则真正的标志位会被刷掉）。
            diff_indexes = [i for i in range(len(raw)) if old[i] != raw[i]]
            bulk = len(diff_indexes) > args.bulk_bytes
            starts = []
            seen = set()
            for index in diff_indexes:
                start = index - (index % 4)
                if start in seen:
                    continue
                seen.add(start)
                starts.append(start)
            if bulk:
                line = "[%7.3fs] %-38s 批量变化 %d 字节（%d 处）—— 已记入 JSON，不逐条打印" % (
                    rel, name, len(diff_indexes), len(starts))
                print(line, flush=True)
                log.write(line + "\n")
            for start in starts:
                before, after = old[start], raw[start]
                extra = fmt_bytes(old, raw, start)
                key = "%s@0x%X" % (name, addr + start)
                bucket = history.setdefault(key, [])
                bucket.append([round(rel, 3), before, after])
                changes += 1
                if bulk or len(bucket) > args.max_print or printed[0] >= args.print_cap:
                    continue                      # 噪音/超量：只进汇总与 JSON，不再刷屏
                printed[0] += 1
                line = "[%7.3fs] %-38s @0x%X  u8 %3d→%-3d%s%s" % (
                    rel, name, addr + start, before, after, extra,
                    "   (已超出 %d 次，后续静默)" % args.max_print if len(bucket) == args.max_print else "")
                print(line, flush=True)
                log.write(line + "\n")

        # 血量锚点（作为"事件对齐"的时间标记）
        raw_h = ms._read_bytes(anchor, 4)
        if raw_h and len(raw_h) == 4:
            import struct
            health = struct.unpack_from("<i", raw_h, 0)[0]
            if health_prev is None:
                health_prev = health
                line = "[%7.3fs] [血量锚点] 初始 health=%d" % (rel, health)
                print(line, flush=True)
                log.write(line + "\n")
            elif health != health_prev:
                line = "[%7.3fs] [血量锚点] health %d → %d" % (rel, health_prev, health)
                print(line, flush=True)
                log.write(line + "\n")
                history.setdefault("血量锚点", []).append([round(rel, 3), health_prev, health])
                health_prev = health

        # 定期把 JSON 落盘：中途被中断也能拿到已有数据，也可以边跑边分析
        if time.monotonic() - last_dump >= args.snapshot_every:
            last_dump = time.monotonic()
            dump_json(json_path, args.label, rounds, rounds * args.interval, history)

        time.sleep(args.interval)

    # 汇总：按变化次数分档 —— 布尔标志位通常只跳 1–4 次，高频变化的都是噪音
    print("\n===== 变化汇总 =====")
    ranked = sorted(history.items(), key=lambda kv: (len(kv[1]), kv[0]))
    buckets = {"标志位候选（1–4 次）": [], "低频（5–15 次）": [], "噪音（>15 次，忽略）": []}
    for key, items in ranked:
        if key == "血量锚点":
            continue
        n = len(items)
        grade = ("标志位候选（1–4 次）" if n <= 4 else
                 "低频（5–15 次）" if n <= 15 else "噪音（>15 次，忽略）")
        trail = "  ".join("%.1fs %d→%d" % (t, a, b) for t, a, b in items[:10])
        buckets[grade].append("%-46s %3d 次   %s" % (key, n, trail))
    for grade, rows in buckets.items():
        print("\n--- %s：%d 个 ---" % (grade, len(rows)))
        for row in rows[:60]:
            print(row)
    print("\n血量锚点变化（事件对齐用）：")
    for t, a, b in history.get("血量锚点", [])[:40]:
        print("  %.2fs  %d → %d" % (t, a, b))

    log.write("\n# 轮询 %d 次，变化 %d 处\n" % (rounds, changes))
    log.close()
    dump_json(json_path, args.label, rounds, rounds * args.interval, history)
    print("\n记录：%s\n数据：%s" % (log_path, json_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
