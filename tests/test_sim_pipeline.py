# -*- coding: utf-8 -*-
"""端到端回放：剧本文件 → SimSource → Runtime._to_sample → 引擎。

`run_vectors.py` 直接构造 `Sample` 喂引擎，因此**绕过了插件的采样转换**（模拟源的键名映射、
以及"模拟数据即权威、不套用内存推导"这条规则）。实测漏掉过两个真 bug：

* 剧本用驼峰键 `maxHealth` / `inTitan`，而 `_to_sample` 读的是下划线键 `max_health` /
  `in_titan` —— 模拟模式下这两个字段永远取默认值 0 / False，每一帧都被判无效，
  A 档位与 B 受击全部发不出来，日志只剩死亡脉冲和满屏 invalid；
* `alive` 被"血量 > 0"推导覆盖 —— 剧本里 health=0 表示"泰坦被毁"，不等于铁驭阵亡。

本文件把同一批黄金场景按插件的真实链路再跑一遍，期望值仍以 scenarios.json 为准。
不联网、不需要 DGHub。

    python tests/test_sim_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

_ws_mod = types.ModuleType("websockets")


async def _fake_connect(*_args, **_kwargs):  # pragma: no cover - 只为导入 main
    raise RuntimeError("本测试不建立连接")


_ws_mod.connect = _fake_connect
sys.modules.setdefault("websockets", _ws_mod)

import main as plugin_main                       # noqa: E402
import run_vectors as rv                         # noqa: E402
from engine import Limits                        # noqa: E402
from source import SimSource, make_vectors_path  # noqa: E402

VECTORS = os.path.join(ROOT, "testvectors", "scenarios.json")


def run_scenario(sc: dict, defaults: dict) -> list:
    """按插件真实链路回放一个场景，返回错误列表。"""
    rt = plugin_main.Runtime({"id": "ttf2_coyote", "config_schema": []})
    rt.cfg.update({"sim_scenario": sc["id"], "change_log": False, "debug_log": False,
                   "source_mode": "sim"})
    rt._apply_config()
    # 触发门槛属于部署配置（默认 1500 / 2000）：剧本里都是小额伤害，回放时按旧规则跑，
    # 这样场景验证的是判定与输出逻辑，而不是部署里的触发门槛（门槛由 case_hit_trigger_thresholds 覆盖）
    rt.engine.cfg.b_hit_min_delta = int(defaults.get("b_hit_min_delta", 0) or 0)
    rt.engine.cfg.b_burst_delta = int(defaults.get("b_burst_delta", 0) or 0)
    rt.engine.cfg.b_burst_window_ms = int(defaults.get("b_burst_window_ms", 1000) or 1000)
    src = SimSource(rt.cfg.get, make_vectors_path(ROOT))
    assert src.scenario_id == sc["id"], "剧本选择错位：%s" % src.scenario_id

    limits = Limits()
    switch_b = defaults["switchB"]
    estop = defaults["estop"]
    last = {"A": None, "B": None}
    errs: list = []

    for step in sc["steps"]:
        tick = step["tick"]
        for key, value in (step.get("limits") or {}).items():
            setattr(limits, rv.LIMIT_KEYS[key], value)
        if "config" in step:
            c = step["config"]
            rt.engine.cfg.tier_intensity[c["slot"]] = int(c["intensity"])
        if "switchB" in step:
            switch_b = bool(step["switchB"])
        if "estop" in step:
            estop = bool(step["estop"])

        raw, _note = src.read(tick)
        if raw is None:
            errs.append("%s t=%d: 模拟源没有产出样本" % (sc["id"], tick))
            continue
        sample = rt._to_sample(tick, raw)
        frame = rt.engine.feed(sample, limits=limits, estop=estop, switch_b=switch_b)

        tag = "%s t=%d" % (sc["id"], tick)
        exp = step["expect"]
        errs += rv.check(tag + " A", exp.get("A"), frame.a, last["A"])
        errs += rv.check(tag + " B", exp.get("B"), frame.b, last["B"])
        if exp.get("log", "") != frame.log_channels:
            errs.append("%s log: 期望 %r，实际 %r" % (tag, exp.get("log", ""), frame.log_channels))
        if exp.get("dispatch", "") != frame.dispatch_channels:
            errs.append("%s dispatch: 期望 %r，实际 %r"
                        % (tag, exp.get("dispatch", ""), frame.dispatch_channels))
        for key in ("n", "dt", "dur"):
            if key in (step.get("extra") or {}):
                got = frame.b.extra.get(key)
                if got != step["extra"][key]:
                    errs.append("%s B.extra[%s]: 期望 %s，实际 %s"
                                % (tag, key, step["extra"][key], got))
        for key, want in ((step.get("extra") or {}).get("engineFlag") or {}).items():
            got = frame.flags.get(key)
            if got != want:
                errs.append("%s flag %s: 期望 %s，实际 %s" % (tag, key, want, got))
        last["A"], last["B"] = frame.a, frame.b

    return errs


async def main_async() -> int:
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

    print("\n模拟链路 %d/%d 场景通过" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
