"""泰坦陨落 2 × DGHub/郊狼 —— 纯逻辑引擎。

职责边界
--------
本模块**不碰进程内存、不碰设备、不读系统时钟**：输入是一次采样与当前配置，
输出是两条通道的决策 `(slot, cfg, eff, clamp, reason)` 以及「是否下发 / 是否记录」
标记。因此可用采样轨迹离线回归（见 tests/run_vectors.py），无需游戏与设备。

单位
----
`cfg` / `eff` / 用户上限一律使用**郊狼强度值**（0..coyote_max，默认 0..200）。
换算成 DGHub 的百分点由设备适配层负责，引擎内不做换算。

处理顺序（严格）
----------------
1 纪元切换复位 → 2 死亡序列边沿 → 3 死亡窗口丢弃 → 4 元组校验 → 5 静音门 →
6 检测（濒死边沿 / 恢复 / 受击 + 不应期 / 脉冲到期）→ 7 输出分配 → 8 限幅 →
9 去重键与日志键

顺序要点：**先检测、后分配**；**限幅只在最后一步，绝不回写 cfg**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------- slot 全集

A_70 = "A_70"
A_50 = "A_50"
A_30 = "A_30"
A_DOOM = "A_DOOM"
A_NONE = "A_NONE"
E_DEATH = "E_DEATH"
B_HIT = "B_HIT"
B_NONE = "B_NONE"

#: 通道 B 上「无脉冲」时可长期保持的原因（避免日志抖动）
_B_STICKY = {"init", "idle", "pulse-end"}


@dataclass
class Sample:
    """一次采样。`read_ok=False` 表示读取失败，**绝不等于血量 0**。"""

    tick: int
    read_ok: bool = True
    epoch: int = 1
    in_titan: bool = True
    alive: bool = True
    health: int = 0
    max_health: int = 0
    doomed: bool = False
    m25: bool = False          # 死亡界面已出现


@dataclass
class Limits:
    """用户设备上限（郊狼强度值）；None 表示不可读。"""

    a_out: Optional[int] = None
    a_abs: Optional[int] = None
    b_out: Optional[int] = None
    b_abs: Optional[int] = None


@dataclass
class Config:
    """条目配置（郊狼强度值 / 毫秒）。"""

    tier_intensity: Dict[str, int] = field(default_factory=lambda: {
        A_70: 20, A_50: 30, A_30: 40, A_DOOM: 60,
        E_DEATH: 45, B_HIT: 25, A_NONE: 0, B_NONE: 0,
    })
    b_enabled: bool = True             # 受击开关；关闭 = 通道 B 全静音（含切断在途 E）
    b_duration_ms: int = 300
    #: 触发受击脉冲的门槛：单次掉血 ≥ 该值（0 = 关闭这条规则）
    b_hit_min_delta: int = 0
    #: 触发受击脉冲的门槛：滑动窗口内累计掉血 ≥ 该值（0 = 关闭这条规则）
    b_burst_delta: int = 0
    #: 上面那条"累计"规则用的滑动窗口
    b_burst_window_ms: int = 1000
    refractory_ms: int = 200
    m25_min_hold_ms: int = 500         # 死亡界面标志的保持时间门槛（去抖，防瞬时误触发）
    e_duration_ms: int = 5000
    hysteresis_pct: int = 2
    epoch_jump_pct: int = 50           # 单次扣减超过最大血量的该比例 → 判为状态切换而非受击；0 = 关闭该保护
    death_window_ceiling_ms: int = 8000
    in_titan_false_hold_ms: int = 500
    invalid_streak_to_relocate: int = 5
    #: 读数恢复稳定多久后撤销"需要重新定位"提示（否则这条提示会永远每 30 秒刷一次）
    relocate_clear_ms: int = 3000
    plugin_max_intensity: int = 80     # 插件自身安全钳制（郊狼强度值）
    coyote_max: int = 200
    host_pct_max: int = 100


@dataclass
class Decision:
    """一条通道的决策。"""

    channel: str
    slot: str
    cfg: int
    eff: int
    reason: str
    clamp: str = "none"                # none | plugin | out | abs
    clamp_limit: int = 0
    extra: Dict[str, int] = field(default_factory=dict)

    def key_dispatch(self) -> Tuple[str, int]:
        return (self.slot, self.eff)

    def key_log(self) -> Tuple:
        return (self.slot, self.cfg, self.eff, self.clamp, self.clamp_limit, self.reason)


@dataclass
class Frame:
    """一帧结果。"""

    a: Decision
    b: Decision
    log_channels: str = ""             # "A" / "B" / "AB" / ""
    dispatch_channels: str = ""
    flags: Dict[str, bool] = field(default_factory=dict)


class Engine:
    """采样 → 输出决策的状态机。"""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.cfg = config or Config()
        self.reset()

    # ------------------------------------------------------------ 生命周期

    def reset(self) -> None:
        self.first_frame = True
        self.epoch: Optional[int] = None
        self.baseline: Optional[int] = None
        self.baseline_ready = False

        self.prev_alive = True
        self.prev_doomed = False
        self.prev_m25 = False
        self.prev_max_health: Optional[int] = None   # 上一帧最大血量 / 是否在泰坦内：
        self.prev_in_titan: Optional[bool] = None    # 用来识别「测的对象换了」而不是垃圾读数
        self.m25_since: Optional[int] = None   # 死亡界面标志本次上升的 tick（去抖用）
        self.m25_fired = False

        self.window = {"active": False, "epoch": None, "start": 0, "not_in_titan_since": None}

        self.e: Optional[Dict[str, int]] = None
        self.pulse: Optional[Dict[str, int]] = None
        self.last_hit_tick: Optional[int] = None
        self.last_pulse_extra: Dict[str, int] = {}
        #: 最近一段时间内的掉血记录 [(tick, delta)]，用于"窗口内累计伤害"触发规则
        self._recent: List[Tuple[int, int]] = []

        self.invalid_streak = 0
        self.last_invalid_reason = ""
        self.last_invalid_tick = None
        self.needs_relocate = False

        self.prev_decision: Dict[str, Optional[Decision]] = {"a": None, "b": None}
        self.dispatch_key: Dict[str, Optional[Tuple]] = {"a": None, "b": None}
        self.log_key: Dict[str, Optional[Tuple]] = {"a": None, "b": None}
        self.last_log_channels = ""
        self.last_dispatch_channels = ""

    # ------------------------------------------------------------ 主入口

    def feed(self, s: Sample, limits: Optional[Limits] = None, estop: bool = False,
             switch_b: Optional[bool] = None, device_lost: bool = False) -> Frame:
        limits = limits or Limits()
        if switch_b is None:
            switch_b = self.cfg.b_enabled

        # ---- 1. 纪元切换复位（最先做，否则实体槽位复用会污染一切）
        if self.epoch is not None and s.epoch != self.epoch:
            self._on_epoch_change(s)

        # ---- 2. 死亡界面上升沿（带去抖：泰坦被毁那一帧该标志会瞬时抖动约 0.3 秒，
        #         不去抖会误发一次 5 秒死亡脉冲；真实死亡界面持续 8 秒以上，去抖很安全）
        #
        # 先算有效性并把它当作**状态迁移的前置条件**：无效帧不得点亮死亡脉冲。
        # 元组校验的"决策"仍在第 4 步处理（无效帧照旧输出 0），这里只挡住"新起的事件"——
        # 否则一次错位读数只要 m25=1，就能换来 5 秒的真实刺激。
        invalid_reason = self.invalid_reason(s)
        invalid = bool(invalid_reason)
        if invalid:
            self.last_invalid_reason = invalid_reason
            self.last_invalid_tick = s.tick

        m25_now = bool(s.m25)
        if m25_now and not self.prev_m25:
            self.m25_since = s.tick
            self.m25_fired = False
        elif not m25_now:
            self.m25_since = None
            self.m25_fired = False

        m25_rise = False
        e_started = False
        if (not invalid and m25_now and not self.m25_fired and self.m25_since is not None
                and s.tick - self.m25_since >= self.cfg.m25_min_hold_ms):
            m25_rise = True
            self.m25_fired = True
            if not self.window["active"]:
                self._open_window(s)
            self.pulse = None                     # 死亡起不再保留在途受击脉冲
            self.last_hit_tick = None
            # 任何死亡界面都给一次死亡脉冲（含步行当铁驭被打死）；
            # 泰坦被毁那一帧该标志会瞬时抖动约 0.3 秒，由上面的保持时间去抖挡掉。
            self.e = {"start": s.tick, "end": s.tick + self.cfg.e_duration_ms}
            e_started = True

        # ---- 3. 死亡窗口：丢弃一切采样（垃圾读数也在此被吞掉；epoch 变化优先关窗）
        if self.window["active"]:
            if not self._window_close_reason(s):
                a_dec = self._none_decision("a", "death-window")
                b_dec, _ = self._decide_b_window(s, limits, estop, device_lost,
                                                 switch_b, e_started)
                self._commit(s, a_dec, b_dec)
                return self._finish(a_dec, b_dec)
            self._close_window(s)

        # ---- 4. 元组校验（fail-closed）：无效帧不得驱动任何状态迁移
        if invalid:
            self.invalid_streak += 1
            if self.invalid_streak >= self.cfg.invalid_streak_to_relocate:
                self.needs_relocate = True
            a_dec = self._none_decision("a", "invalid")
            b_dec = self._decide_b_invalid(s, limits, estop, device_lost, switch_b)
            self._commit(s, a_dec, b_dec)
            return self._finish(a_dec, b_dec)
        self.invalid_streak = 0
        if (self.needs_relocate and self.last_invalid_tick is not None
                and s.tick - self.last_invalid_tick >= self.cfg.relocate_clear_ms):
            # 读数已经稳定一段时间：撤销提示，避免"指针链可能已失效"被无限重播
            self.needs_relocate = False
            self.last_invalid_reason = ""

        # ---- 5. 死亡边沿（alive 由真转假）：开窗，且死亡帧本身按窗口处理
        alive_fall = self.prev_alive and not s.alive
        if alive_fall:
            self._open_window(s)
            self.pulse = None
            self.last_hit_tick = None
            a_dec = self._none_decision("a", "death-window")
            b_dec, _ = self._decide_b_window(s, limits, estop, device_lost, switch_b, False)
            self._commit(s, a_dec, b_dec)
            return self._finish(a_dec, b_dec)

        # ---- 6. 检测
        # 注：原「处决动画静音门」（m20 ∧ 濒死 期间 A/B 全静音）已按需求删除 ——
        # 处决动画期间保持濒死档输出，死亡脉冲不受影响。m20 字段随之移除。
        recover = False
        hit_started = False
        pulse_ended = False
        e_ended = False

        doomed_rise = s.doomed and not self.prev_doomed
        doomed_fall = self.prev_doomed and not s.doomed
        if not self.baseline_ready:
            self.baseline = s.health
            self.baseline_ready = True
        elif doomed_rise:
            self.baseline = s.health              # 濒死切换：静默重基线，不产生受击
        elif doomed_fall:
            self.baseline = s.health              # 插电池解除濒死
            recover = True
        elif s.health > self.baseline:
            self.baseline = s.health              # 恢复：只重设基线，绝不产生脉冲
            recover = True
        elif s.health < self.baseline:
            delta = self.baseline - s.health
            self.baseline = s.health
            jump = s.max_health * self.cfg.epoch_jump_pct // 100 if self.cfg.epoch_jump_pct > 0 else 0
            if jump and delta > jump:
                pass                              # 单次扣减过大 → 疑似换机/上下机/泰坦被摧毁，静默重基线
            elif not alive_fall:                  # 死亡帧收尾扣减：不产生受击
                hit_started = self._register_hit(s, delta)

        if self.pulse and s.tick >= self.pulse["end"]:
            self._end_pulse()
            pulse_ended = True
        if self.e and s.tick >= self.e["end"]:
            self.e = None
            e_ended = True
            self.last_pulse_extra = {"dur": self.cfg.e_duration_ms}

        # ---- 8. 输出分配
        a_dec = self._allocate_a(s, limits, estop, device_lost, recover)
        b_dec = self._allocate_b(s, limits, estop, device_lost, switch_b,
                                 hit_started, pulse_ended, e_ended)
        self._commit(s, a_dec, b_dec)
        return self._finish(a_dec, b_dec)

    # ------------------------------------------------------------ 通道 A

    def _allocate_a(self, s: Sample, limits: Limits, estop: bool, device_lost: bool,
                    recover: bool) -> Decision:
        prev = self.prev_decision["a"]
        prev_slot = prev.slot if prev else None

        if estop:
            return self._finalize(self._none_decision("a", "estop"), limits)
        if device_lost:
            return self._finalize(self._none_decision("a", "device-lost"), limits)
        if not s.in_titan:
            return self._finalize(self._none_decision("a", "no-titan"), limits)
        if not s.alive:
            return self._finalize(self._none_decision("a", "dead"), limits)

        slot = self._tier_slot(s, prev_slot)
        cfg = self._tier(slot)
        if slot == A_NONE:
            # 无档位：保持 init/idle 以免日志每帧抖动；阻断类原因由上面的早退分支给出
            keep_idle = prev is not None and prev.slot == A_NONE and prev.reason in ("init", "idle")
            reason = prev.reason if keep_idle else "idle"
        elif recover:
            reason = "recover-refresh"
        elif slot != prev_slot:
            reason = "tier-enter"
        else:
            reason = prev.reason if prev else "tier-enter"
        return self._finalize(Decision("a", slot, cfg, cfg, reason), limits)

    def _tier_slot(self, s: Sample, prev_slot: Optional[str]) -> str:
        """档位判定：整数交叉相乘 + 滞回（进档严格小于阈值，退档需超出阈值 + 滞回带）。"""
        if s.doomed:
            return A_DOOM
        m = s.max_health
        if m <= 0:
            return A_NONE
        h = s.health
        band = max(0, self.cfg.hysteresis_pct)
        if prev_slot == A_DOOM and 100 * h < (0 + band) * m:
            return A_DOOM
        for slot, pct in ((A_30, 30), (A_50, 50), (A_70, 70)):
            if 100 * h < pct * m:
                return slot
            if prev_slot == slot and 100 * h < (pct + band) * m:
                return slot
        if prev_slot in (A_30, A_50, A_70):
            pct = {A_30: 30, A_50: 50, A_70: 70}[prev_slot]
            if 100 * h < (pct + band) * m:
                return prev_slot
        return A_NONE

    # ------------------------------------------------------------ 通道 B

    def _allocate_b(self, s: Sample, limits: Limits, estop: bool, device_lost: bool,
                    switch_b: bool, hit_started: bool, pulse_ended: bool,
                    e_ended: bool) -> Decision:
        if estop:
            self.pulse = None
            self.e = None
            return self._finalize(self._none_decision("b", "estop"), limits)
        if device_lost:
            self.pulse = None
            self.e = None
            return self._finalize(self._none_decision("b", "device-lost"), limits)
        if not switch_b:
            self.pulse = None
            self.e = None
            return self._finalize(self._none_decision("b", "switch-off"), limits)
        if e_ended:
            return self._finalize(Decision("b", B_NONE, 0, 0, "pulse-end",
                                           extra=dict(self.last_pulse_extra)), limits)
        if self.e:
            cfg = self._tier(E_DEATH)
            return self._finalize(Decision("b", E_DEATH, cfg, cfg, self._b_keep("death-screen")),
                                  limits)
        if pulse_ended:
            return self._finalize(Decision("b", B_NONE, 0, 0, "pulse-end",
                                           extra=dict(self.last_pulse_extra)), limits)
        if self.pulse:
            cfg = self._tier(B_HIT)
            reason = "hit" if hit_started else "hit"
            return self._finalize(Decision("b", B_HIT, cfg, cfg, reason), limits)
        if hit_started:
            cfg = self._tier(B_HIT)
            return self._finalize(Decision("b", B_HIT, cfg, cfg, "hit"), limits)
        return self._finalize(self._none_decision("b", self._b_keep("idle")), limits)

    def _decide_b_window(self, s: Sample, limits: Limits, estop: bool, device_lost: bool,
                         switch_b: bool, e_started: bool):
        """死亡窗口内的通道 B：E 照常播放（独立 5 秒），其余静默。"""
        if estop:
            self.e = None
            return self._finalize(self._none_decision("b", "estop"), limits), False
        if device_lost:
            self.e = None
            return self._finalize(self._none_decision("b", "device-lost"), limits), False
        if not switch_b:
            self.e = None
            return self._finalize(self._none_decision("b", "switch-off"), limits), False
        if self.e:
            if s.tick >= self.e["end"]:
                self.e = None
                extra = {"dur": self.cfg.e_duration_ms}
                self.last_pulse_extra = dict(extra)
                return self._finalize(Decision("b", B_NONE, 0, 0, "pulse-end", extra=extra),
                                      limits), True
            cfg = self._tier(E_DEATH)
            reason = "death-screen" if e_started else self._b_keep("death-screen")
            return self._finalize(Decision("b", E_DEATH, cfg, cfg, reason), limits), False
        return self._finalize(self._none_decision("b", self._b_keep("idle")), limits), False

    def _decide_b_invalid(self, s: Sample, limits: Limits, estop: bool, device_lost: bool,
                          switch_b: bool) -> Decision:
        if estop:
            self.e = None
            self.pulse = None
            return self._finalize(self._none_decision("b", "estop"), limits)
        if device_lost:
            self.e = None
            self.pulse = None
            return self._finalize(self._none_decision("b", "device-lost"), limits)
        if not switch_b:
            self.e = None
            self.pulse = None
            return self._finalize(self._none_decision("b", "switch-off"), limits)
        if self.e and s.tick >= self.e["end"]:
            self.e = None
            extra = {"dur": self.cfg.e_duration_ms}
            self.last_pulse_extra = dict(extra)
            return self._finalize(Decision("b", B_NONE, 0, 0, "pulse-end", extra=extra), limits)
        if self.e:                                 # E 在跑：不因无效读数中断
            cfg = self._tier(E_DEATH)
            return self._finalize(Decision("b", E_DEATH, cfg, cfg, self._b_keep("death-screen")),
                                  limits)
        self.pulse = None
        return self._finalize(self._none_decision("b", "invalid"), limits)

    # ------------------------------------------------------------ 受击脉冲

    def _register_hit(self, s: Sample, delta: int) -> bool:
        """返回是否新起一个受击脉冲。

        触发门槛（两条任一成立；两条都填 0 时退化为"任何掉血都算一次受击"的旧行为）：
        * 单次掉血 ≥ `b_hit_min_delta`；
        * 最近 `b_burst_window_ms` 内累计掉血 ≥ `b_burst_delta`（覆盖"小额高频"的连打）。

        脉冲在途或处于不应期时，掉血一律并入当前脉冲（n / dt），不新起——这是频率上限。
        """
        self._recent.append((s.tick, delta))
        cutoff = s.tick - self.cfg.b_burst_window_ms
        while self._recent and self._recent[0][0] <= cutoff:
            self._recent.pop(0)
        burst_total = sum(d for _t, d in self._recent)

        in_flight = self.pulse is not None
        in_refractory = (self.last_hit_tick is not None
                         and s.tick - self.last_hit_tick < self.cfg.refractory_ms)
        if in_flight or in_refractory:
            if self.pulse:
                self.pulse["n"] += 1
                self.pulse["dt"] += delta
            elif self.last_hit_tick is not None:
                # 不应期内但脉冲已结束：并入上一次的统计，不新起脉冲
                self.last_pulse_extra = {
                    "n": self.last_pulse_extra.get("n", 1) + 1,
                    "dt": self.last_pulse_extra.get("dt", 0) + delta,
                }
            return False

        min_delta = self.cfg.b_hit_min_delta
        burst = self.cfg.b_burst_delta
        if min_delta > 0 or burst > 0:
            big_hit = min_delta > 0 and delta >= min_delta
            in_burst = burst > 0 and burst_total >= burst
            if not (big_hit or in_burst):
                return False
        self.last_hit_tick = s.tick
        self.pulse = {"start": s.tick, "end": s.tick + self.cfg.b_duration_ms, "n": 1, "dt": delta}
        return True

    def _end_pulse(self) -> None:
        if self.pulse:
            self.last_pulse_extra = {"n": self.pulse["n"], "dt": self.pulse["dt"]}
        self.pulse = None

    # ------------------------------------------------------------ 限幅与收尾

    def _none_decision(self, ch: str, reason: str) -> Decision:
        slot = A_NONE if ch == "a" else B_NONE
        return Decision(ch, slot, 0, 0, reason)

    def _finalize(self, dec: Decision, limits: Limits) -> Decision:
        """限幅（最后一步）：eff = min(cfg, 插件上限, 输出上限, 绝对上限)。"""
        cands = [(self.cfg.plugin_max_intensity, "plugin")]
        for attr, kind in ((f"{dec.channel}_out", "out"), (f"{dec.channel}_abs", "abs")):
            v = getattr(limits, attr, None)
            if v is not None:
                cands.append((int(v), kind))
        limit, kind = min(cands, key=lambda t: t[0])
        if limit <= 0:
            dec.eff = 0
            dec.clamp = "none"          # 通道停用用 reason 表达，不谎报「被限幅到 0」
            dec.clamp_limit = 0
            dec.reason = "ch-disabled"
            return dec
        dec.eff = min(dec.cfg, limit)
        if dec.eff < dec.cfg:
            dec.clamp = kind
            dec.clamp_limit = limit
        return dec

    def _commit(self, s: Sample, a_dec: Decision, b_dec: Decision) -> None:
        """按日志键 / 下发键决定是否记录与下发；随后固化本帧状态。"""
        log_ch, disp_ch = [], []
        for label, dec in (("a", a_dec), ("b", b_dec)):
            prev = self.prev_decision[label]
            if prev is not None and dec.slot == prev.slot:
                if dec.cfg != prev.cfg:
                    dec.reason = "cfg-change"
                elif dec.eff > 0 and (dec.clamp, dec.clamp_limit) != (prev.clamp, prev.clamp_limit) \
                        and dec.clamp != "none":
                    dec.reason = "clamp"
            if self.first_frame:
                dec.reason = "clamp" if dec.clamp != "none" else "init"
            if dec.key_dispatch() != self.dispatch_key[label]:
                self.dispatch_key[label] = dec.key_dispatch()
                disp_ch.append(label.upper())
            if dec.key_log() != self.log_key[label]:
                self.log_key[label] = dec.key_log()
                log_ch.append(label.upper())
            self.prev_decision[label] = dec

        self.prev_alive = s.alive
        self.prev_doomed = s.doomed
        self.prev_m25 = s.m25
        self.prev_max_health = s.max_health
        self.prev_in_titan = s.in_titan
        self.epoch = s.epoch
        self.last_log_channels = "".join(log_ch)
        self.last_dispatch_channels = "".join(disp_ch)

    def _finish(self, a_dec: Decision, b_dec: Decision) -> Frame:
        frame = Frame(a=a_dec, b=b_dec,
                      log_channels=self.last_log_channels,
                      dispatch_channels=self.last_dispatch_channels,
                      flags={"needs_relocate": self.needs_relocate,
                             "invalid_reason": self.last_invalid_reason})
        self.first_frame = False
        return frame

    # ------------------------------------------------------------ 辅助

    def _tier(self, slot: str) -> int:
        return int(self.cfg.tier_intensity.get(slot, 0))

    def _b_keep(self, default: str) -> str:
        prev = self.prev_decision["b"]
        if prev is not None and prev.slot == B_NONE and prev.reason in _B_STICKY:
            return prev.reason
        return default

    def invalid_reason(self, s: Sample) -> str:
        """返回非空表示本帧读数不可信（fail-closed），字符串说明**是哪一条校验挂了**。

        日志只写"连续读到无效数据"会把排查方向带偏（用户会以为一定是指针链失效，其实
        也常见于"根本没在对局里"或"游戏进程已关闭"）。所以这里返回具体原因。
        """
        if not s.read_ok:
            return "读取失败"
        if s.max_health <= 0:
            return "最大血量<=0（字段没读到泰坦量级）"
        if s.health < 0 or s.health > s.max_health:
            return "血量超范围（%d 不在 0..%d）" % (s.health, s.max_health)
        # 注意：doomed ∧ !alive 是**合法的死亡态**（濒死泰坦被处决/打爆），不是矛盾，不得判为 Invalid
        if self.baseline_ready and self.epoch == s.epoch and self.baseline is not None:
            # 「同一纪元内血量单次变化不得超过最大血量」用来挡垃圾读数。但有两种情况
            # **测的对象本来就换了**，不是垃圾读数，必须放行：
            #   ① 最大血量变了（换机体 / 进濒死时最大值变成濒死池 2500 / 铁驭 100）
            #   ② 上下机切换了（铁驭 ↔ 泰坦）
            # 典型反例（实测）：下机 12500→100、进濒死 12500→2500、复活 100→25，
            # 幅度都超过新的最大值；早先一律判 invalid，导致 A 通道被 fail-closed 关掉，
            # 连濒死档 60 都发不出来。
            entity_changed = (
                (self.prev_max_health is not None and s.max_health != self.prev_max_health)
                or (self.prev_in_titan is not None and s.in_titan != self.prev_in_titan))
            if not entity_changed and abs(s.health - self.baseline) > s.max_health:
                return "同纪元内单次变化超过最大血量（%d → %d）" % (self.baseline, s.health)
        return ""

    def force_redispatch(self) -> None:
        """清空下发去重缓存：下一个采样帧会把两条通道的当前状态重新下发一次。

        用于「WebSocket 会话重建」之后：DGHub 在插件断开时会清理该插件的强度层，
        因此重连后必须重新把当前档位推一遍，否则设备会一直停在 0。
        """
        self.dispatch_key = {"a": None, "b": None}

    def _on_epoch_change(self, s: Sample) -> None:
        self.window = {"active": False, "epoch": None, "start": 0, "not_in_titan_since": None}
        self.e = None
        self.pulse = None
        self.last_hit_tick = None
        self._recent = []          # 换机/新一命：累计伤害窗口从零开始
        self.invalid_streak = 0
        self.epoch = s.epoch
        self.baseline = s.health
        self.baseline_ready = True

    def _open_window(self, s: Sample) -> None:
        self.window = {"active": True, "epoch": s.epoch, "start": s.tick,
                       "not_in_titan_since": None if s.in_titan else s.tick}

    def _window_close_reason(self, s: Sample) -> str:
        """返回非空表示本帧应关闭窗口。epoch 变化是第一优先。"""
        if not self.window["active"]:
            return ""
        if s.epoch != self.window["epoch"]:
            return "epoch"
        if not s.in_titan:
            if self.window["not_in_titan_since"] is None:
                self.window["not_in_titan_since"] = s.tick
            elif s.tick - self.window["not_in_titan_since"] >= self.cfg.in_titan_false_hold_ms:
                return "in-titan-false"
        else:
            self.window["not_in_titan_since"] = None
        if s.tick - self.window["start"] >= self.cfg.death_window_ceiling_ms:
            self.needs_relocate = True
            return "ceiling"
        return ""

    def _close_window(self, s: Sample) -> None:
        self.window["active"] = False
        self.baseline = s.health
        self.baseline_ready = True
        self.pulse = None
        self.last_hit_tick = None
