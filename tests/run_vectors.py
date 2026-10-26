# -*- coding: utf-8 -*-
"""黄金测试向量离线回归。

引擎是纯逻辑（不碰内存、不碰设备、不读系统时钟），因此本脚本**不需要游戏、
不需要郊狼设备、不需要 DGHub**，可在任何 CPython 上跑：

    python tests/run_vectors.py

用例与期望值全部来自 ../testvectors/scenarios.json；修改判定规则时必须同步
更新该文件，否则视为回归失败。
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine import Config, Engine, Limits, Sample  # noqa: E402

VECTORS = os.path.join(ROOT, "testvectors", "scenarios.json")
LIMIT_KEYS = {"aOut": "a_out", "aAbs": "a_abs", "bOut": "b_out", "bAbs": "b_abs"}
SAMPLE_KEYS = {
    "readOk": "read_ok", "epoch": "epoch", "inTitan": "in_titan", "alive": "alive",
    "health": "health", "maxHealth": "max_health", "doomed": "doomed",
    "m20": "m20", "m25": "m25",
}


def fmt(dec) -> list:
    clamp = dec.clamp if dec.clamp == "none" else "%s:%d" % (dec.clamp, dec.clamp_limit)
    return [dec.slot, dec.cfg, dec.eff, clamp, dec.reason]


def check(tag: str, exp, dec, last) -> list:
    """exp 为 None 表示「与上一帧完全相同」。"""
    if exp is None:
        if last is None:
            return ["%s: 期望无变化，但没有上一帧可比" % tag]
        if dec.key_log() != last.key_log():
            return ["%s: 期望无变化，实际 %s（上一帧 %s）" % (tag, fmt(dec), fmt(last))]
        return []
    if tuple(exp) != tuple(fmt(dec)):
        return ["%s: 期望 %s，实际 %s" % (tag, list(exp), fmt(dec))]
    return []


def run_scenario(sc: dict, defaults: dict) -> list:
    errs: list = []
    cfg = Config()
    cfg.tier_intensity.update(defaults["config"])
    # 触发门槛属于部署配置：黄金场景默认按「任何掉血都算受击」跑，需要时在 defaults 里声明
    cfg.b_hit_min_delta = int(defaults.get("b_hit_min_delta", 0) or 0)
    cfg.b_burst_delta = int(defaults.get("b_burst_delta", 0) or 0)
    cfg.b_burst_window_ms = int(defaults.get("b_burst_window_ms", 1000) or 1000)
    limits = Limits()
    state = dict(defaults["sample"])
    switch_b = defaults["switchB"]
    estop = defaults["estop"]
    engine = Engine(cfg)
    last = {"A": None, "B": None}

    for step in sc["steps"]:
        tick = step["tick"]
        tag = "%s t=%d" % (sc["id"], tick)

        for key, value in (step.get("sample") or {}).items():
            state[key] = value
        for key, value in (step.get("limits") or {}).items():
            setattr(limits, LIMIT_KEYS[key], value)
        if "config" in step:
            c = step["config"]
            cfg.tier_intensity[c["slot"]] = int(c["intensity"])
        if "switchB" in step:
            switch_b = bool(step["switchB"])
        if "estop" in step:
            estop = bool(step["estop"])

        sample = Sample(tick=tick, **{SAMPLE_KEYS[k]: v for k, v in state.items()})
        frame = engine.feed(sample, limits=limits, estop=estop, switch_b=switch_b)

        exp = step["expect"]
        errs += check(tag + " A", exp.get("A"), frame.a, last["A"])
        errs += check(tag + " B", exp.get("B"), frame.b, last["B"])
        if exp.get("log", "") != frame.log_channels:
            errs.append("%s log: 期望 %r，实际 %r" % (tag, exp.get("log", ""), frame.log_channels))
        if exp.get("dispatch", "") != frame.dispatch_channels:
            errs.append("%s dispatch: 期望 %r，实际 %r"
                        % (tag, exp.get("dispatch", ""), frame.dispatch_channels))

        extra = step.get("extra") or {}
        for key, want in extra.get("engineFlag", {}).items():
            got = frame.flags.get(key)
            if got != want:
                errs.append("%s flag %s: 期望 %s，实际 %s" % (tag, key, want, got))
        for key in ("n", "dt", "dur"):
            if key in extra:
                got = frame.b.extra.get(key)
                if got != extra[key]:
                    errs.append("%s B.extra[%s]: 期望 %s，实际 %s" % (tag, key, extra[key], got))

        last["A"], last["B"] = frame.a, frame.b

    return errs


def main() -> int:
    with open(VECTORS, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    defaults = data["defaults"]

    total = 0
    failed = 0
    for sc in data["scenarios"]:
        errs = run_scenario(sc, defaults)
        total += 1
        if errs:
            failed += 1
            print("FAIL %s" % sc["id"])
            for line in errs:
                print("     " + line)
        else:
            print("ok   %s（%d 步）" % (sc["id"], len(sc["steps"])))

    print("\n%d/%d 场景通过" % (total - failed, total))
    if failed:
        print("失败场景数：%d" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
