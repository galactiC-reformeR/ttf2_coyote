"""一次性自动标定：从游戏进程内存里自动找出插件所需的字段地址。

做法不是"人工对着日志猜"，而是：

1. 让标定模组（NSCalibDamage）按固定剧本改血量，形成一条**已知形状**的轨迹：
   满血 → 64% → 48% → 28% → 濒死池（1/5）→ 0 → 复活 → 新泰坦满血；
2. 本工具以 20Hz 密集记录候选地址的 **int32 值**（血量锚点区 ±0x2000，外加 28 条静态链
   指向的结构），记录的是"每次变化的 (时刻, 新值)"；
3. 用"比值形状"去匹配：不依赖绝对血量，所以 7500 / 10000 / 12500 / 15000 / 17500 都能标；
4. 按事件时刻区分 doomed 与 m25： doomed 在"进濒死"那一刻置位、在复活时清零；
   m25 在"泰坦被毁"那一刻置位、复活时清零 —— 两者时刻不同，不会混淆；
5. 输出 tools\\calibration.json（可直接粘贴进配置页）与 calibration-report.txt（判据 + 证据）。

用法::

    python tools\\calibrate.py --selftest                 # 用合成轨迹验证匹配算法
    python tools\\calibrate.py --replay tools\\calibration-trace.json
    python tools\\calibrate.py --minutes 4                # 实机跑：按 README 的顺序敲标定命令

只读：只用 PROCESS_VM_READ | PROCESS_QUERY_INFORMATION，绝不写入游戏内存。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import source  # noqa: E402
import flag_watch  # noqa: E402

CFG = {"mem_module": "auto"}
#: 血量比例形状（相对满血）：正常 → 64% → 48% → 28% → 濒死池 1/5
RATIO_SHAPE = (1.0, 0.64, 0.48, 0.28, 0.2)
RATIO_TOL = 0.06


# ---------------------------------------------------------------- 轨迹容器

class Trace:
    """{(区域名, 区域基址, 地址): [(tick_ms, int32), ...]} 以及区域元信息。"""

    def __init__(self) -> None:
        self.regions: list = []          # [(name, base, size)]
        self.points: dict = {}           # addr -> [(t, value)]

    def add(self, addr: int, t: int, value: int) -> None:
        seq = self.points.get(addr)
        if seq is None:
            self.points[addr] = [(t, value)]
        elif seq[-1][1] != value:
            seq.append((t, value))

    def where(self, addr: int) -> str:
        for name, base, size in self.regions:
            if base <= addr < base + size:
                return "%s +0x%X" % (name, addr - base)
        return "未知区域 0x%X" % addr

    def values(self, addr: int) -> list:
        return [v for _t, v in self.points.get(addr, [])]

    def first_value(self, addr: int) -> int | None:
        seq = self.points.get(addr)
        return seq[0][1] if seq else None

    def to_json(self) -> dict:
        return {"regions": self.regions,
                "points": {("0x%X" % a): [(t, v) for t, v in seq]
                           for a, seq in self.points.items()}}

    @classmethod
    def from_json(cls, data: dict) -> "Trace":
        tr = cls()
        tr.regions = [tuple(r) for r in data.get("regions", [])]
        for key, seq in data.get("points", {}).items():
            tr.points[int(key, 16)] = [(t, v) for t, v in seq]
        return tr


def synthetic_trace() -> Trace:
    """合成一条"标准剧本"轨迹，用来验证匹配算法（不需要游戏）。

    植入：health(111E1E68) / max_health(111E1E6C) / doomed(111E20C4, bit0，bit1 常驻) /
    m25(111E0F88) / epoch，外加大量噪声地址。
    """
    import random
    random.seed(7)
    tr = Trace()
    base = 0x100000000
    anchor = base + 0x1E68
    tr.regions = [("血量锚点区", anchor - 0x1000, 0x2000)]

    def off(delta: int) -> int:
        return anchor + delta

    health, max_health = off(0), off(4)
    doomed, m25 = off(0x25C), off(0xF88 - 0x1E68)

    t = 0
    tr.add(health, t, 12500)
    tr.add(max_health, t, 12500)
    tr.add(doomed, t, 2)          # bit1 常驻、bit0 清
    tr.add(m25, t, 0)

    def step(dt, hp, mx=None):
        nonlocal t
        t += dt
        tr.add(health, t, hp)
        if mx is not None:
            tr.add(max_health, t, mx)

    step(1000, 12500)                  # 满血基线
    step(1000, 8000)                   # 64%
    step(1000, 6000)                   # 48%
    step(1000, 3500)                   # 28%
    t += 1000
    tr.add(health, t, 2500)            # 濒死池：血量与最大值一起塌缩
    tr.add(max_health, t, 2500)
    tr.add(doomed, t, 3)               # bit0 置位 = 濒死
    step(1500, 0, 2500)                # 被毁
    tr.add(m25, t, 1)                  # 死亡界面
    step(500, 25, 25)                  # 铁驭复活
    tr.add(doomed, t, 0)
    tr.add(m25, t, 0)
    step(2000, 12500, 12500)           # 新泰坦

    for i in range(300):               # 噪声：大量不断变化的值
        addr = anchor - 0x1000 + (i * 4)
        if addr in (health, max_health, doomed, m25):
            continue
        val = random.randint(0, 3)
        for k in range(3):
            val = (val + random.randint(1, 3)) % 4
            tr.add(addr, 100 + k * 700 + i, val)
    return tr


# ---------------------------------------------------------------- 匹配

def _ratio_ok(value: int, want: float, full: float) -> bool:
    return full > 0 and abs(value / full - want) <= RATIO_TOL


def find_health(tr: Trace) -> list:
    """找"血量"字段：值序列的形状符合 1.0 → 0.64 → 0.48 → 0.28 → 0.2 → 0。

    返回 [(addr, full, 证据链)]，按置信度排序（越靠前越可信）。
    """
    out = []
    for addr, seq in tr.points.items():
        vals = [v for _t, v in seq]
        if len(vals) < 6:
            continue
        full = vals[0]
        if full <= 1000:          # 血量锚点区外的噪声：满血至少上千
            continue
        idx, want = 1, RATIO_SHAPE[1]
        hits = []
        for i in range(1, len(vals)):
            if _ratio_ok(vals[i], want, full):
                hits.append((seq[i][0], vals[i], want))
                idx += 1
                if idx >= len(RATIO_SHAPE):
                    break
                want = RATIO_SHAPE[idx]
        if len(hits) < len(RATIO_SHAPE) - 1:
            continue
        # 形状末尾必须出现 0（被毁）
        tail = [v for _t, v in seq if v == 0]
        out.append((addr, full, hits, bool(tail)))
    out.sort(key=lambda x: (not x[3], -len(x[2])))
    return out


def find_max_health(tr: Trace, health_addr: int, doom_tick: int) -> list:
    """找最大血量：初始值等于血量满值，且在"进濒死"那一帧同步塌缩成血量的同期值。"""
    out = []
    full = tr.first_value(health_addr) or 0
    for addr, seq in tr.points.items():
        if addr == health_addr:
            continue
        if (tr.first_value(addr) or -1) != full:
            continue
        for t, v in seq:
            if abs(t - doom_tick) <= 120 and v < full:
                out.append((addr, full, v))
                break
    return out


def _transitions(tr: Trace, addr: int) -> list:
    return tr.points.get(addr, [])


def _changed_near(tr: Trace, addr: int, tick: int, tol: int = 150) -> bool:
    return any(abs(t - tick) <= tol for t, _v in _transitions(tr, addr))


def find_flag(tr: Trace, tick_on: int, tick_off: int, exclude: set) -> list:
    """找在 tick_on 变、在 tick_off 复位的标志字节，并给出位掩码。

    返回 [(addr, 置位前的值, 置位后的值, 掩码)]。
    """
    out = []
    for addr, seq in tr.points.items():
        if addr in exclude:
            continue
        before = None
        for t, v in seq:
            if t < tick_on - 150:      # 只取"置位窗口之前"的值，别把置位那一帧算进来
                before = v
        if before is None:
            continue
        after = None
        for t, v in seq:
            if abs(t - tick_on) <= 150:
                after = v
                break
        if after is None or after == before:
            continue
        if not _changed_near(tr, addr, tick_off, 400):
            continue
        bit = after & ~before
        if bit == 0:
            continue
        out.append((addr, before, after, bit))
    return out


def calibrate(tr: Trace, doomed_tick: int | None = None, m25_tick: int | None = None) -> dict:
    """跑完整匹配，返回 {字段: 结论}。"""
    res = {"health": None, "max_health": None, "doomed": None, "m25": None, "notes": []}
    cands = find_health(tr)
    if not cands:
        res["notes"].append("没能找到血量字段：请确认剧本（64%/48%/28%/濒死池）是否跑完")
        return res
    health_addr, full, hits, has_zero = cands[0]
    res["health"] = {"addr": health_addr, "where": tr.where(health_addr), "full": full,
                     "evidence": [{"t": t, "value": v, "want_ratio": w} for t, v, w in hits],
                     "reached_zero": has_zero}
    if len(cands) > 1:
        res["notes"].append("血量有 %d 个候选，已选形状最完整的一个；其余：%s"
                            % (len(cands), [tr.where(c[0]) for c in cands[1:4]]))

    doom_tick = doomed_tick if doomed_tick is not None else (hits[-1][0] if hits else 0)
    mx = find_max_health(tr, health_addr, doom_tick)
    if mx:
        res["max_health"] = {"addr": mx[0][0], "where": tr.where(mx[0][0]),
                             "full": mx[0][1], "collapsed_to": mx[0][2]}
    else:
        res["notes"].append("没能找到最大血量：剧本里有没有跑到濒死池（1/5）那一步？")

    # 复活时刻：血量从 0 回到小值；被打爆时刻：血量第一次变 0
    respawn, death_tick, prev = None, None, None
    for t, v in tr.points.get(health_addr, []):
        if death_tick is None and v == 0:
            death_tick = t
        if prev == 0 and v > 0:
            respawn = t
            break
        prev = v
    if respawn is None:
        res["notes"].append("没看到复活（血量 0 → 正数），doomed / m25 的复位时刻无法确认")

    exclude = {health_addr}
    if res["max_health"]:
        exclude.add(res["max_health"]["addr"])
    off_tick = respawn or (doom_tick + 2000)
    d = find_flag(tr, doom_tick, off_tick, exclude)
    if d:
        addr, before, after, bit = d[0]
        res["doomed"] = {"addr": addr, "where": tr.where(addr),
                         "value_before": before, "value_at_doom": after,
                         "flag_true_value": after, "flag_mask": bit}
        exclude.add(addr)
        if len(d) > 1:
            res["notes"].append("濒死标志还有 %d 个候选：%s"
                                % (len(d) - 1, [tr.where(x[0]) for x in d[1:4]]))

    # 死亡界面：在"泰坦被打爆"那一帧置位、复活时复位（与濒死标志的时刻不同，所以不会混淆）
    m25_tick = m25_tick if m25_tick is not None else death_tick
    if m25_tick is not None:
        m = find_flag(tr, m25_tick, off_tick, exclude)
        if m:
            addr, before, after, bit = m[0]
            res["m25"] = {"addr": addr, "where": tr.where(addr),
                          "value_before": before, "value_at_m25": after, "flag_mask": bit}
        else:
            res["notes"].append("没能找到死亡界面标志：泰坦被打爆那一段有没有跑到？")
    return res


def render_report(res: dict) -> str:
    lines = ["自动标定报告", "=" * 40, ""]

    def show(key, title, extras=""):
        item = res.get(key)
        if not item:
            lines.append("%-12s 未标定" % title)
            return
        lines.append("%-12s %s（%s）%s" % (title, item["where"], item.get("full", ""), extras))

    show("health", "当前血量")
    show("max_health", "最大血量")
    if res.get("doomed"):
        d = res["doomed"]
        lines.append("%-12s %s  真值=%d 掩码=%d（置位前 %d）"
                     % ("濒死标志", d["where"], d["flag_true_value"], d["flag_mask"],
                        d["value_before"]))
    else:
        lines.append("%-12s 未标定" % "濒死标志")
    if res.get("m25"):
        lines.append("%-12s %s" % ("死亡界面", res["m25"]["where"]))
    lines.append("")
    if res["notes"]:
        lines.append("备注：")
        lines.extend("  - " + n for n in res["notes"])
    return "\n".join(lines)


def selftest() -> int:
    tr = synthetic_trace()
    anchor = tr.regions[0][1] + 0x1000
    exp_health = anchor
    exp_max = anchor + 4
    exp_doomed = anchor + 0x25C
    exp_m25 = anchor + (0xF88 - 0x1E68)
    res = calibrate(tr)
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print("  %s %-12s %s" % ("ok  " if good else "FAIL", label,
                                 "%s" % ("0x%X" % got if isinstance(got, int) else got)))

    print("自检（合成轨迹）:")
    check("当前血量", (res.get("health") or {}).get("addr"), exp_health)
    check("最大血量", (res.get("max_health") or {}).get("addr"), exp_max)
    check("濒死标志", (res.get("doomed") or {}).get("addr"), exp_doomed)
    check("濒死掩码", (res.get("doomed") or {}).get("flag_mask"), 1)
    print()
    print(render_report(res))
    print()
    print("自检结果:", "通过" if ok else "失败")
    return 0 if ok else 1


# ---------------------------------------------------------------- 实机采样

def record(minutes: float, interval: float, outdir: str,
           anchor_span: int = 0x2000, chain_span: int = 0x400) -> Trace:
    ms = source.MemorySource(CFG)
    ms.open()
    regions, anchor = flag_watch.build_regions(ms, anchor_span, chain_span)
    tr = Trace()
    tr.regions = [(name, base, size) for name, base, size in regions]
    print("监测 %d 个窗口（血量锚点 ±0x%X，每条链 ±0x%X），%g 分钟，%.0fms 一轮"
          % (len(regions), anchor_span // 2, chain_span // 2, minutes, interval * 1000))

    t0 = time.monotonic()
    prev: dict = {}
    deadline = time.monotonic() + minutes * 60
    last_dump = 0.0
    trace_path = os.path.join(outdir, "calibration-trace.json")
    while time.monotonic() < deadline:
        now = time.monotonic()
        tick = int((now - t0) * 1000)
        for name, base, size in regions:
            buf = ms.read_bytes(base, size)
            if not buf:
                continue
            for off in range(0, size - 3, 4):
                addr = base + off
                value = int.from_bytes(buf[off:off + 4], "little", signed=True)
                if prev.get(addr) != value:
                    prev[addr] = value
                    tr.add(addr, tick, value)
        if now - last_dump > 10:
            last_dump = now
            with open(trace_path, "w", encoding="utf-8") as fh:
                json.dump(tr.to_json(), fh)
            print("  %5.1fs  已记录 %d 个地址" % (tick / 1000.0, len(tr.points)))
        time.sleep(interval)
    with open(trace_path, "w", encoding="utf-8") as fh:
        json.dump(tr.to_json(), fh)
    print("轨迹已保存:", trace_path)
    return tr


def main() -> int:
    ap = argparse.ArgumentParser(description="一次性自动标定内存字段（只读）")
    ap.add_argument("--selftest", action="store_true", help="用合成轨迹验证匹配算法")
    ap.add_argument("--replay", default="", help="用已保存的轨迹 JSON 复算")
    ap.add_argument("--minutes", type=float, default=0.0, help="实机采样时长（分钟）")
    ap.add_argument("--interval", type=float, default=0.05, help="采样间隔（秒）")
    ap.add_argument("--outdir", default=HERE)
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.replay:
        tr = Trace.from_json(json.load(open(args.replay, encoding="utf-8")))
    elif args.minutes > 0:
        os.makedirs(args.outdir, exist_ok=True)
        tr = record(args.minutes, args.interval, args.outdir)
    else:
        ap.print_help()
        return 2

    res = calibrate(tr)
    print(render_report(res))

    payload = {"off_health": res["health"]["where"] if res.get("health") else "",
               "off_max_health": res["max_health"]["where"] if res.get("max_health") else ""}
    if res.get("doomed"):
        payload["off_doomed"] = res["doomed"]["where"]
        payload["flag_true_value"] = res["doomed"]["flag_true_value"]
        payload["flag_mask"] = res["doomed"]["flag_mask"]
    if res.get("m25"):
        payload["off_m25"] = res["m25"]["where"]

    out_cfg = os.path.join(args.outdir, "calibration.json")
    with open(out_cfg, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("\n可直接填进配置页：", out_cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
