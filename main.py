# -*- coding: utf-8 -*-
"""泰坦陨落 2 × DGHub/郊狼 —— 插件主程序（SDK v1）。

协议依据：DGHub 《插件开发指南》(SDK v1)。三条硬性约束：

1. **首条消息必须是 ``{"op": "hello", ...}``**，否则主程序以 1008 policy violation 断开
   （上一版就是因为先发了 ``{"type": "log"}`` 而从未真正接入）；
2. 所有消息按 ``op`` 字段分发，且 **未知字段会被拒绝**（extra=forbid），因此只发文档字段；
3. 连接期间必须持续保活（ping/pong），插件断开后主程序会清理其强度层。

输出模型（与 main 的设计一致，见 spec/engine-contract.md）
--------------------------------------------------------
* **通道 A = 电平** → 用 ``set_strength`` 直接设该插件的强度层，档位变了才下发；
* **通道 B = 脉冲** → 同样用 ``set_strength`` 精确控制起止（0 → 强度 → 0），
  配合 ``pulse`` 播一次该条目的波形。
  这里刻意**不用** ``trigger`` 的 rollback 租约：租约到期前无法立刻切断，
  而需求要求「关闭受击开关 / 急停 / 处决静音」必须**立即**切断通道 B
  （官方文档也明确：自持强度状态机时应使用底层命令）。
* 单位：条目配置一律是**郊狼强度值（0–200）**，下发前按
  ``pct = value * 100 / 设备最大强度`` 换算成 DGHub 的百分点（设备最大强度来自
  ``device_info``）。用户自设的设备上限插件读不到，由设备侧兜底。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from engine import (A_NONE, B_HIT, B_NONE, E_DEATH, Config, Engine, Limits, Sample)
from selfcheck import SELFCHECK_INTERVAL_S, diagnose as selfcheck_diagnose
from source import LOCATE_DEFAULTS, MemorySource, SimSource, make_vectors_path

try:
    import websockets
except ImportError:  # pragma: no cover
    print("缺少 websockets（应由 DGHub 主程序提供）", file=sys.stderr)
    raise

# ---- 环境注入（官方文档字段；兼容旧变量名以便手动调试） ----
DGHUB_HOST = os.environ.get("DGHUB_HOST", "127.0.0.1")
DGHUB_PORT = os.environ.get("DGHUB_PORT", "8000")
DGHUB_TOKEN = os.environ.get("DGHUB_TOKEN", "")
PLUGIN_ID = os.environ.get("DGHUB_PLUGIN_ID") or os.environ.get("PLUGIN_ID") or "ttf2_coyote"
PLUGIN_ROOT = os.environ.get("DGHUB_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT") \
    or os.path.dirname(os.path.abspath(__file__))

VK_BY_NAME = {"F12": 0x7B, "F11": 0x7A, "PAUSE": 0x13, "END": 0x23, "off": 0}
SLOT_PRESET_KEY = {B_HIT: "b_preset", E_DEATH: "e_preset"}

#: 通道互斥：持续输出（血量档位电平）与脉冲输出（单次受击、死亡脉冲）各占一条通道。
#: 一条通道同时只能有一种输出 —— 两者压在同一条上会互相覆盖强度层。
CHANNEL_DEFAULT = {"level": "a", "pulse": "b", "e": "b"}
CHANNEL_NAME = {"a": "A", "b": "B"}
#: 通道字段 → 面板上的条目名（警告里说人话）
CHANNEL_LABEL = {"a_channel": "血量档位持续输出", "b_channel": "单次受击", "e_channel": "死亡脉冲"}
#: 模拟运行的边界提示（用户要求：一轮剧本 = 一次模拟，前后各一句）
SIM_START_FMT = "现在处于模拟状态，%s，以下为模拟运行产生的日志。"
SIM_END_TEXT = "此次模拟场景输出已结束"

#: 死亡脉冲时长（毫秒）默认值；面板「☠️ [E] → 死亡脉冲时长 (ms)」可调。
E_DURATION_MS = 5000
#: 死亡界面标志的保持时间门槛（去抖，毫秒）。实测被毁那一帧该标志会抖动约 0.3 秒，
#: 不去抖会误发一次死亡脉冲；真实死亡界面持续 8 秒以上，因此 500ms 只挡抖动、不会漏报。
M25_MIN_HOLD_MS = 500


def _safe_console_text(text: str) -> str:
    """把一行日志降级成标准输出装得下的写法。

    中文 Windows 的控制台与管道默认用 GBK 编码，日志里的 emoji（如「🔧 维护」）编不进去，
    `print` 会抛 UnicodeEncodeError 并把整条日志连同后面的逻辑一起打断。这里只在编码装不下时
    才把编不了的字符替换成「?」，其余中文原样保留；日志面板收到的仍是完整原文。
    """
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return text
    try:
        text.encode(enc)
    except LookupError:      # 编码名不可用时交给 print 自己报错，不在这里吞掉
        return text
    except UnicodeEncodeError:
        return text.encode(enc, "replace").decode(enc, "replace")
    return text


def _norm_channel(value, fallback: str) -> str:
    """通道取值容错：只接受 a / b。

    旧版本允许 `both`（两通道同时输出），那与「一条通道一种输出」互斥，因此按 fallback 处理，
    并由 `_resolve_channels` 把纠正结果写回界面。
    """
    text = str(value if value is not None else "").strip().lower()
    return text if text in ("a", "b") else fallback


#: 这些配置键变化时必须重建数据源（指针链、字段类型、数据源选择都在建源时解析一次）
SOURCE_KEYS = ("source_mode", "sim_scenario", "mem_module", "off_health", "off_max_health",
               "off_doomed", "off_in_titan", "off_alive", "off_epoch", "off_m25",
               "off_int32_fields", "flag_true_value")


def _load_manifest() -> dict:
    path = os.path.join(PLUGIN_ROOT, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("无法读取 manifest.json：%s" % exc)
    data["id"] = PLUGIN_ID
    return data


class Config7(dict):
    """配置读取（DGHub 下发的 key/value），带类型容错。"""

    def num(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)

    def text(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return default if value is None else str(value)


class Runtime:
    """插件运行时：WebSocket 会话 + 采样/判定/下发循环。"""

    def __init__(self, manifest: dict) -> None:
        self.manifest = manifest
        self.cfg = Config7()
        self.engine = Engine()
        self.source = None
        self.ws = None
        self.stop = asyncio.Event()
        self.config_ready = asyncio.Event()
        self.tx: asyncio.Queue = asyncio.Queue()
        self.send_lock = asyncio.Lock()
        self.reconnect = asyncio.Event()      # 置位 → supervisor 重建会话（重新 hello 注册）
        self.session_reason = ""

        # 设备侧信息（device_info）
        self.device_connected = False
        self.max_strength = {"a": 100.0, "b": 100.0}   # 设备上报前的初值（与锁死的换算上限一致）

        # 下发去重（与引擎的 dispatch 键配合）
        self.sent = {"a": None, "b": None}
        # 通道映射（持续 / 脉冲各自独占一条通道，互斥由 _resolve_channels 强制）
        self.channels = None
        self.channel_fixes: dict = {}
        self.channel_dirty = False
        # 模拟运行（一轮剧本 = 一次模拟）：运行期间锁定剧本与数据源，结束后才允许切换
        self.sim_run = False
        self.sim_done = False          # 单次演练（once）播完：不再读、不再循环、不再重复报
        self.sim_finished_noted = False
        self.sim_last_pos = None
        self.sim_refused_at = 0.0
        self.sim_defer_warned = False
        self.source_note = "未启动"
        self.last_sample_ok = 0.0
        self.last_frame: dict = {}
        self.doomed_flag_suspect = None    # 濒死标志在非濒死血量上置位时的(血量,最大值)，用于诊断
        self.last_doomed_src = ""          # 濒死判定来源：flag / collapse / flag+collapse
        self.peak_bad_read = None          # 超过合理上限的最大血量读数（不参与峰值），用于诊断
        self.last_peak_warn = 0.0
        self.last_suspect_warn = 0.0
        self.last_relocate_warn = 0.0
        self.a_tier_since = 0.0
        self.a_hold_zeroed = False
        self.estop = False
        self._estop_key_down = False
        self._dry_noted = None         # 演练模式开关的提示只在状态变化时打一次
        self._debug_at = 0.0
        self._status_at = 0.0
        self._identity_logged = False
        self.in_titan_configured = False
        self.device_info_received = False
        self.device_logged = False
        self.max_hint_logged = False
        self.no_device_warned = False
        self._limit_warned = False        # "郊狼上限未上报、按固定 100 换算"只提示一次
        self.last_device_connected = None      # 跨会话保留：用于判定"设备刚变为可用"
        self.last_reassert = 0.0
        self.reassert_count = 0
        self.session_index = 0
        self.peak_max = 0            # 本命最大血量的峰值（识别"濒死池塌缩"用）
        self.source_ready = False
        self.source_dirty = False
        self._source_snapshot = None
        self.frames = 0
        self.last_error = ""
        #: 进程自检：上次检查的时间（None = 从没查过）与上次说过的结论（同样的结论不重复刷）
        self._selfcheck_at = None
        self._selfcheck_emit = 0.0
        self._selfcheck_text = ""

    # ------------------------------------------------------------ 发送

    async def send(self, message: dict) -> None:
        if not self.stop.is_set():
            await self.tx.put(message)

    async def log(self, level: str, text: str) -> None:
        print(_safe_console_text("[%s] %s" % (level, text)), flush=True)
        await self.send({"op": "log", "level": level, "message": text})

    async def _tx_loop(self) -> None:
        while not self.stop.is_set():
            message = await self.tx.get()
            if not self.ws:
                continue
            try:
                async with self.send_lock:
                    await self.ws.send(json.dumps(message, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                print("发送失败: %s" % exc, file=sys.stderr)
                self.session_reason = "发送失败"
                self.reconnect.set()
                return

    # ------------------------------------------------------------ 接收

    async def _rx_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                op = message.get("op")
                if op == "config":
                    data = message.get("data") or {}
                    if isinstance(data, dict):
                        self.cfg.update(data)
                    self.config_ready.set()
                    self._apply_config()
                elif op == "config_changed":
                    key = message.get("key")
                    if key is not None:
                        self.cfg[key] = message.get("value")
                        self._apply_config()
                        if key == "reassert_now" and str(message.get("value")).lower() in ("now", "1", "是"):
                            await self.send({"op": "set_config", "key": "reassert_now", "value": "off"})
                            await self._reassert("用户手动触发")
                        if key == "replay_sim" and str(message.get("value")).lower() in ("now", "1", "是"):
                            await self.send({"op": "set_config", "key": "replay_sim", "value": "off"})
                            self._request_replay()
                            await self.log("info", "重播当前演练：数据源将重建，演练从头再放一遍")
                elif op == "device_info":
                    first_report = not self.device_info_received
                    self.device_info_received = True
                    connected = bool(message.get("connected"))
                    for ch in ("a", "b"):
                        value = message.get("max_strength_%s" % ch)
                        if isinstance(value, (int, float)) and value > 0:
                            self.max_strength[ch] = float(value)
                    was = self.last_device_connected
                    self.last_device_connected = connected
                    self.device_connected = connected
                    if connected and was is not True:
                        # 设备（重新）可用：立刻重发当前输出，不等下一个定期重发周期
                        await self._reassert("设备已连接" if first_report else "设备重新连接")
                    elif not connected and was is True:
                        await self.log("info", "设备已断开：恢复连接后会自动重发当前输出")
                elif op == "ping":
                    await self.send({"op": "pong", "t": message.get("t", time.time())})
                elif op == "stop":
                    self.stop.set()
                    return
        except Exception as exc:  # noqa: BLE001
            if not self.stop.is_set():
                print("接收循环异常: %s" % exc, file=sys.stderr)
        finally:
            if not self.stop.is_set():
                self.session_reason = self.session_reason or "连接中断"
                self.reconnect.set()

    async def _send_now(self, message: dict) -> None:
        """直接经连接发送，不经发送队列。

        收尾路径（退出归零）上 tx 循环已被取消、且 ``stop`` 已置位会让 ``send`` 直接丢弃，
        因此这里必须绕开队列 —— 否则「退出归零」这条安全承诺是空的。
        """
        if self.ws is None:
            return
        async with self.send_lock:
            await asyncio.wait_for(
                self.ws.send(json.dumps(message, ensure_ascii=False)), timeout=1.0)

    async def _close_ws(self) -> None:
        """显式关闭 WebSocket。

        只把 ``self.ws`` 置 None 依赖 GC 不会及时发出关闭帧：实测 DGHub 会一直认为
        「远程插件 ttf2_coyote 已有活跃连接」并拒绝重新注册（连续 85 秒 35 次全部被拒）。
        """
        ws, self.ws = self.ws, None
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(code=1000, reason="session rebuild"), timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            print("关闭旧连接异常：%s" % exc, file=sys.stderr)

    # ------------------------------------------------------------ 配置 → 引擎

    def _apply_config(self) -> None:
        c = self.cfg
        # 指针链/类型/数据源这几个键改了必须重建数据源：它们的解析只在建源时做一次，
        # 否则用户在界面上填好偏移后必须重启插件才生效（标定时很折磨人）。
        snapshot = tuple(str(c.get(k)) for k in SOURCE_KEYS)
        if snapshot != self._source_snapshot:
            self._source_snapshot = snapshot
            self.source_dirty = True
        cfg = self.engine.cfg
        cfg.tier_intensity.update({
            "A_70": int(c.num("a_70_strength", 20)),
            "A_50": int(c.num("a_50_strength", 30)),
            "A_30": int(c.num("a_30_strength", 40)),
            "A_DOOM": int(c.num("a_doom_strength", 60)),
            "B_HIT": int(c.num("b_strength", 25)),
            "E_DEATH": int(c.num("e_strength", 45)),
        })
        cfg.b_enabled = c.flag("b_enable", True)
        cfg.b_duration_ms = int(c.num("b_duration_ms", 300))
        cfg.refractory_ms = int(c.num("b_refractory_ms", 200))
        # 受击触发门槛：单次掉血 ≥ 1500，或 1 秒内累计掉血 ≥ 2000（都填 0 = 恢复"任何掉血都算"）
        cfg.b_hit_min_delta = int(c.num("b_hit_min_delta", 1500))
        cfg.b_burst_delta = int(c.num("b_burst_delta", 2000))
        cfg.b_burst_window_ms = int(c.num("b_burst_window_ms", 1000))
        # 死亡脉冲时长：面板可调（默认 5 秒）；夹到 500–15000ms，防止手输 0 或极端值
        cfg.e_duration_ms = max(500, min(15000, int(c.num("e_duration_ms", E_DURATION_MS))))
        # 去抖门槛仍是定值（不做成面板项）
        cfg.m25_min_hold_ms = M25_MIN_HOLD_MS
        cfg.plugin_max_intensity = int(c.num("plugin_max_intensity", 80))
        self.in_titan_configured = bool(
            c.text("off_in_titan", "").strip() or LOCATE_DEFAULTS.get("in_titan"))
        # 通道切分只在轮询任务里改（那里才安全：不与下发并发），这里只登记"需要重新解析"
        channels, fixes = self._resolve_channels()
        if channels != self.channels or fixes != self.channel_fixes:
            self.channel_fixes = fixes
            self.channel_dirty = True

    # ------------------------------------------------------------ 数据源

    def _make_source(self):
        mode = self.cfg.text("source_mode", "memory")
        if mode == "off":
            return None, "数据源已关闭"
        if mode == "sim":
            path = make_vectors_path(PLUGIN_ROOT)
            if not os.path.exists(path):
                return None, "找不到测试向量 %s" % path
            src = SimSource(self.cfg.get, path)
            return src, "模拟数据源：%s" % src.scenario_id
        src = MemorySource(self.cfg.get)
        if not src.configured:
            return src, "内存字段尚未标定（需先定位血量指针链）"
        return src, "内存只读轮询已就绪"

    async def _open_source(self) -> None:
        """按当前配置创建数据源（已就绪时不动）。

        只在轮询任务里调用：数据源一旦建立就被同一任务读取，换源必须与读取串行。
        """
        if self.source_ready:
            return
        self.source_ready = True
        self.source, self.source_note = self._make_source()
        # 换源（含换剧本、重播）就是新的一轮：运行标记清零，由 _begin_sim_run 重新开场
        self.sim_run = False
        self.sim_done = False
        self.sim_finished_noted = False
        self.sim_last_pos = None
        self.sim_defer_warned = False
        await self.log("info", "数据源：%s" % self.source_note)
        if not self.in_titan_configured:
            await self.log("info", "「是否在泰坦内」未标定：改用最大血量推断（>%.0f 视为在泰坦内）"
                           % self.cfg.num("in_titan_min_max_health", 1000))
        if self.source is not None and self.source.__class__.__name__ == "MemorySource":
            if not self.source.configured:
                await self.log("warning",
                                "内存字段未标定：插件不会输出任何刺激。"
                                "请先用 Cheat Engine 定位泰坦血量指针链并填入「内存定位」区")

    async def _rebuild_source_now(self) -> None:
        """指针链/数据源配置变了 → 丢掉旧数据源，下一次 `_open_source` 重新建。

        这段必须每帧检查（而不仅在启动时）：用户是在插件运行中填偏移的，只在启动时检查
        会让「填好偏移后必须重启插件」这个旧毛病复活。
        """
        self.source_dirty = False
        had_source = self.source is not None
        # 配置变了（可能就是用户刚改了「进程 / 默认模块名」）：清掉自检节流，下次失败立刻重新判断
        self._selfcheck_at = None
        self._selfcheck_text = ""
        if had_source:
            self.source.close()
        self.source = None
        self.source_ready = False
        if had_source:
            # 首次启动时本来就没有数据源，别打"正在重建"这句（尤其模拟模式下会误导）
            await self.log("info", "配置已更新：正在重建数据源")

    async def _apply_channels_if_dirty(self) -> None:
        """通道配置变了 → 切换映射（先两通道归零，再按新映射重发）。"""
        if not self.channel_dirty:
            return
        self.channel_dirty = False
        await self._apply_channels()

    # ------------------------------------------------------------ 模拟运行

    def _sim_name(self) -> str:
        """当前模拟剧本的短名（S2 / S4 …）。"""
        scenario = getattr(self.source, "scenario_id", "") or ""
        return scenario.split("-")[0] or self.cfg.text("sim_scenario", "?").strip().upper()

    def _sim_mode(self) -> bool:
        """配置上是不是模拟模式。

        通道重叠策略要用它，而那段逻辑可能在数据源建立**之前**执行（启动时的首次通道解析），
        所以这里看配置而不是看 `self.source` 的类型。
        """
        return self.cfg.text("source_mode", "memory").strip().lower() == "sim"

    def _channel_overlap(self) -> list:
        """用户选的通道是否重叠：返回被让路的配置键（空列表 = 没重叠）。"""
        _plan, fixes = self._resolve_channels()
        return list(fixes)

    async def _begin_sim_run(self) -> None:
        """开始一轮模拟：先复位引擎，再打开场白。

        复位放在这里，是为了让开场白一定排在所有运行日志之前，且上一轮遗留的脉冲 / 死亡窗口 /
        基线不会污染新一轮的判定。
        """
        self.engine.reset()
        self.sent = {"a": None, "b": None}
        self.sim_run = True
        self.sim_last_pos = None
        self.sim_defer_warned = False
        await self.log("info", SIM_START_FMT % self._sim_name())

    async def _end_sim_run(self) -> None:
        """一轮剧本播完：结束语。"""
        await self.log("info", SIM_END_TEXT)
        self.sim_run = False
        self.sim_last_pos = None

    async def _sim_refuse(self) -> None:
        """通道重叠：警告 + 拒绝本次模拟输出（不读、不喂引擎、不下发、不出运行日志）。"""
        self.sim_run = False
        self.sim_last_pos = None
        now = time.monotonic()
        if now - self.sim_refused_at < 5.0:
            return
        self.sim_refused_at = now
        await self.log("warning", "通道重叠：%s 与「%s」指向同一条通道 —— 本次模拟输出已拒绝，"
                                  "请把其中一个改到另一条通道"
                       % ("、".join(CHANNEL_LABEL.get(k, k) for k in self._channel_overlap()),
                          CHANNEL_LABEL["a_channel"]))

    async def _sim_defer_warn(self) -> None:
        """模拟运行中改了剧本 / 数据源：本轮不受影响，改动推迟到本轮结束后生效。"""
        if self.sim_defer_warned:
            return
        self.sim_defer_warned = True
        await self.log("warning", "模拟运行中：模拟剧本 / 数据源的改动将在本次模拟结束后生效")

    def _request_replay(self) -> None:
        """重播当前剧本：置脏标记，由轮询任务重建数据源（演练随之从头再放一遍）。

        `once` 剧本播完就停，而"把剧本重新选成同一个值"不会触发 config_changed，
        所以需要一个显式入口 —— 见配置页「🔧 维护 → 重播当前演练」。
        """
        self.source_dirty = True
        self.sim_finished_noted = False

    async def _note_dry(self) -> None:
        """演练模式开关变化时明确说一句。

        这一句非常重要：开着演练模式时插件**一个包都不发**，只把命令写进日志 ——
        用户看到满屏「[演练] 待下发」却"没有任何波形"，很容易以为是插件坏了。
        """
        dry = self.cfg.flag("dry_run", False)
        if dry == self._dry_noted:
            return
        self._dry_noted = dry
        if dry:
            await self.log("info", "演练模式已开启：只把「待下发」写进日志，不向设备发送任何命令"
                                  "（要真出波形请在配置里关掉「演练模式」）")
        else:
            await self.log("info", "演练模式已关闭：输出将真实下发给设备")

    async def _tick_sleep(self, started: float, interval: float) -> None:
        """把采样间隔睡满（也用于"拒绝模拟"这种提前跳过本帧的路径，避免空转）。"""
        spent = time.monotonic() - started
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.002, interval - spent))
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------ 下发

    def _relocate_text(self, frame) -> str:
        """「连续读到无效数据」的可读诊断：哪条校验挂了、读的是哪个进程、最近一帧长什么样。

        旧文案只有一句"指针链可能已失效"，把排查方向带偏了：同样的现象也可能只是
        "不在对局里"（泰坦字段本来就是 0）或"游戏进程已关闭"。
        """
        reason = frame.flags.get("invalid_reason") or "原因未知"
        raw = dict(self.last_frame or {})
        if "最大血量" in reason:
            advice = "不在对局或没坐进泰坦时属正常；一直在泰坦里仍为 0 才需重新标定"
        elif "读取失败" in reason:
            advice = "游戏进程可能已关闭，或句柄/模块基址失效（换启动方式、重启游戏后需重新解析）"
        elif "超范围" in reason or "单次变化" in reason:
            advice = "地址可能已错位（引擎构建变了或指到了别的字段），需要重新标定"
        else:
            advice = "指针链可能已失效，需要重新定位字段"
        where = self.source_note or "无数据源"
        if self.source is not None and hasattr(self.source, "identity"):
            try:
                where = self.source.identity()
            except Exception:  # noqa: BLE001 身份信息只用于日志，取不到不影响判定
                pass
        return ("连续读到无效数据：%s ｜ 最近读数 hp=%s/%s titan=%s alive=%s ｜ 数据源 %s ｜ %s"
                % (reason, raw.get("health"), raw.get("max_health"),
                   int(bool(raw.get("in_titan"))), int(bool(raw.get("alive"))), where, advice))

    def _device_text(self) -> str:
        """设备状态的可读描述（区分「未上报」「未连接」「已连接」三种情况）。"""
        if not self.device_info_received:
            return "DGHub 未上报设备信息"
        if not self.device_connected:
            return "未连接"
        return "已连接 max=%d/%d" % (self.max_strength["a"], self.max_strength["b"])

    def _pct_limit(self, channel: str) -> tuple:
        """返回 (用于换算的上限, 来源)。

        上限只有两个来源，用户不可配置（避免"上限填错导致整体强度减半/翻倍"这类麻烦）：

        1. **设备上报**（`device_info.max_strength_*`）—— 最准，DGHub 只在插件注册那一刻上报一次；
        2. 上报缺失时**按固定 100 换算** —— 与郊狼 V3 的实际上限一致；若你的设备上限是 200，
           这一档会让强度翻倍，此时唯一的办法是让插件在设备已连接的状态下启动。
        """
        if self.device_info_received:
            return (self.max_strength.get(channel, 100.0) or 100.0), "设备上报"
        return 100.0, "固定100"

    def _pct(self, channel: str, value: int) -> int:
        """郊狼值 → 下发百分比。

        `set_strength` 的 pct 是「**目标通道**上限的百分比」，所以这里必须按实际下发到的
        物理通道取上限（A/B 两通道上限可以不同，用户可把持续输出指派到任意一条通道）。
        """
        maximum, _source = self._pct_limit(channel)
        pct = int(round(max(0.0, min(float(maximum), float(value))) * 100.0 / maximum))
        return max(0, min(100, pct))

    def _resolve_channels(self):
        """解析三处通道配置，强制「一条通道同时只有一种输出」。

        持续输出（血量档位电平）独占一条通道；单次受击与死亡脉冲属于脉冲输出，必须走另一条。
        用户把脉冲选到持续通道上时**持续输出优先**：脉冲让路到另一条通道，并把纠正结果放进
        返回值的第一项（`fixes`），由 `_apply_channels` 写回界面 —— 否则界面显示 A、实际走 B，
        用户会以为是插件坏了。
        """
        raw_level = str(self.cfg.get("a_channel") or "").strip().lower()
        level = _norm_channel(raw_level, CHANNEL_DEFAULT["level"])
        other = "b" if level == "a" else "a"
        fixes = {}
        if raw_level and raw_level not in ("a", "b"):
            fixes["a_channel"] = level
        pulse = _norm_channel(self.cfg.get("b_channel"), CHANNEL_DEFAULT["pulse"])
        if pulse == level:
            fixes["b_channel"] = other
            pulse = other
        e_ch = _norm_channel(self.cfg.get("e_channel"), CHANNEL_DEFAULT["e"])
        if e_ch == level:
            fixes["e_channel"] = pulse
            e_ch = pulse
        return {"level": level, "pulse": pulse, "e": e_ch}, fixes

    async def _apply_channels(self) -> None:
        """通道映射变化时切换输出路径：先两通道归零，再按新映射重新下发，并纠正界面值。

        先归零是必须的：旧映射可能把持续档位的强度留在被释放的通道上，两路输出压在同一条
        通道上时后发的命令会覆盖先发的 —— 这正是「覆盖出错」的来源。
        """
        plan, fixes = self._resolve_channels()
        sim = self._sim_mode()
        changed = plan != self.channels
        if changed:
            if self.channels is not None and not self.cfg.flag("dry_run", False):
                for target in ("a", "b"):
                    await self.send({"op": "set_strength", "channel": target, "pct": 0})
            self.channels = plan
        self.sent = {"a": None, "b": None}
        self.engine.force_redispatch()
        # 模拟模式下**不**悄悄把界面值改回来：让用户看到自己选了重叠的通道，并直接拒绝这次模拟
        overlap_refused = sim and bool(fixes)
        for key, value in ({} if overlap_refused else fixes).items():
            await self.send({"op": "set_config", "key": key, "value": value})
        if not overlap_refused and (changed or fixes):
            await self.log("warning" if fixes else "info",
                           "通道分配：持续输出=%s｜单次受击与死亡脉冲=%s%s"
                           % (CHANNEL_NAME[plan["level"]], CHANNEL_NAME[plan["pulse"]],
                              "（你选的通道与持续输出冲突，已按「持续优先」让路，界面同步纠正）"
                              if fixes else ""))

    def _physical_channel(self, decision) -> str:
        """逻辑输出 → 物理通道：持续电平走 level 通道，受击/死亡脉冲走脉冲通道。"""
        plan = self.channels or CHANNEL_DEFAULT
        if decision.channel == "a":
            return plan["level"]
        if decision.slot == E_DEATH:
            return plan["e"]
        return plan["pulse"]

    async def _dispatch(self, frame) -> None:
        # 配置到齐之前不下发：否则会拿默认值发出真实命令，而且此时设备 target 可能还没就绪
        if not self.config_ready.is_set():
            return
        dry = self.cfg.flag("dry_run", False)
        if not dry:
            if not self.device_logged:
                self.device_logged = True
                await self.log("info", "输出前设备状态：%s（若 DGHub 报「设备目标不可用」，"
                                      "请重新连接郊狼并在插件上重新选择输出目标）" % self._device_text())
            if not self.device_info_received and not self.max_hint_logged:
                self.max_hint_logged = True
                await self.log("info", "DGHub 未上报设备上限（启用插件时郊狼未连接，之后不再补报）："
                                      "按固定 100 换算强度（郊狼上限已锁死，不可配置）。"
                                      "若你的设备上限其实是 200，实际强度会翻倍")
            if self.device_info_received and not self.device_connected:
                if not self.no_device_warned:
                    self.no_device_warned = True
                    await self.log("warning", "设备未连接：DGHub 会丢弃所有输出命令")
            elif self.device_connected and self.no_device_warned:
                self.no_device_warned = False
                await self.log("info", "设备已连接，输出命令恢复")
        for decision in (frame.a, frame.b):
            channel = self._physical_channel(decision)
            key = "a" if decision.channel == "a" else "b"
            if frame.dispatch_channels and key.upper() not in frame.dispatch_channels:
                continue
            pct = self._pct(channel, decision.eff)
            if self.sent[key] == pct:
                continue
            self.sent[key] = pct
            limit, limit_src = self._pct_limit(channel)
            if limit_src != "设备上报" and not self._limit_warned:
                self._limit_warned = True
                await self.log("warning",
                               "郊狼上限未上报（DGHub 只在启用插件那一刻上报一次）：按固定 100 换算。"
                               "若你的设备上限是 200，这里的强度会翻倍 —— 重新在郊狼已连接的状态下启用插件即可")
            # 真实模式一律照发（含归零：否则通道会停在旧强度）；演练模式只在有输出时记一行，
            # 免得"归零"把日志刷满。
            if pct > 0 or not dry:
                await self._emit({"op": "set_strength", "channel": channel, "pct": pct}, dry,
                                 "强度 %s 通道 = 郊狼值 %d（按上限 %d·%s 换算为 %d%%）"
                                 % (CHANNEL_NAME[channel], decision.eff, int(limit), limit_src, pct))
            # 脉冲起振时播一次该条目的波形（pulse 只播波形、不改强度）
            if decision.slot in SLOT_PRESET_KEY:
                preset_key = SLOT_PRESET_KEY[decision.slot]
                preset = self.cfg.text(preset_key, "")
                # 留空 = 不发 pulse，只有 set_strength 持续 → 恒定强度（死亡脉冲默认就是这个）
                if preset:
                    await self._emit({"op": "pulse", "preset": preset, "channel": channel}, dry,
                                     "波形「%s」→ %s 通道" % (preset, CHANNEL_NAME[channel]))

    async def _emit(self, message: dict, dry: bool, text: str) -> None:
        """下发一条设备命令；演练模式（dry_run）下只记日志。

        波形名（preset）与换算后的百分比本来只出现在设备命令里，从不进变更日志 ——
        演练模式下把「待下发」原样打出来，才能不接设备就核对波形与强度。
        """
        if dry:
            await self.log("info", "[演练] 待下发：%s" % text)
        else:
            await self.send(message)

    async def _force_zero(self, reason: str) -> None:
        """立即切断两通道（急停 / 失联 / 退出）。"""
        dry = self.cfg.flag("dry_run", False)
        if dry:
            await self.log("info", "[演练] 待下发：两通道归零")
        for target in ("a", "b"):
            self.sent[target] = 0
            if not dry:
                await self.send({"op": "set_strength", "channel": target, "pct": 0})
        await self.log("warning", "已切断两通道输出：%s" % reason)

    # ------------------------------------------------------------ 日志

    @staticmethod
    def _fmt(decision) -> str:
        clamp = "-" if decision.clamp == "none" else "%s(%d)" % (decision.clamp, decision.clamp_limit)
        extra = ""
        if decision.extra:
            extra = " " + " ".join("%s=%s" % (k, v) for k, v in decision.extra.items())
        return ("ch=%s slot=%s cfg=%d eff=%d clamp=%s reason=%s%s"
                % (decision.channel, decision.slot, decision.cfg, decision.eff,
                   clamp, decision.reason, extra))

    async def _log_changes(self, frame, snapshot: dict) -> None:
        if not self.cfg.flag("change_log", True) or not frame.log_channels:
            return
        # doomed 后面标出判定来源、并带上 peak（峰值最大血量）：
        # 排查"这个血量为什么判濒死"时，一眼能分清是标志位还是"最大值塌缩"，
        # 以及峰值是否被垃圾读数污染过（collapse 的判据是 peak >= 3 × 当前最大值）。
        src = self.last_doomed_src
        head = "[%.1f] hp=%s/%s doomed=%d%s peak=%d titan=%d alive=%d" % (
            snapshot.get("t_ms", 0) / 1000.0, snapshot.get("health"), snapshot.get("max_health"),
            int(snapshot.get("doomed", 0)), ("(%s)" % src) if src else "",
            self.peak_max, int(snapshot.get("in_titan", 0)),
            int(snapshot.get("alive", 0)))
        for label, decision in (("A", frame.a), ("B", frame.b)):
            if label in frame.log_channels:
                await self.log("info", "%s | %s" % (head, self._fmt(decision)))

    # ------------------------------------------------------------ 急停热键

    def _poll_estop(self) -> None:
        try:
            import ctypes
            name = self.cfg.text("estop_key", "F12")
            vk = VK_BY_NAME.get(name.upper(), 0x7B)
            if not vk:
                return
            down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not self._estop_key_down:
                self.estop = not self.estop
                print("急停热键：%s" % ("已触发（输出已归零，再按恢复）" if self.estop else "已恢复"))
            self._estop_key_down = down
        except Exception:  # noqa: BLE001
            return

    # ------------------------------------------------------------ 主循环

    async def _selfcheck(self, started: float, force: bool = False) -> None:
        """读取失败时做进程自检：把「该把 进程 / 默认模块名 改成什么」写进日志。

        只在内存数据源上用（模拟/关闭数据源没有进程可查）。节流：每
        ``SELFCHECK_INTERVAL_S`` 秒最多查一次；结论没变就只在 5 分钟后再念一遍，
        免得 8ms 的轮询把日志刷爆。

        :param started: 当前 monotonic 时间（与轮询同一时钟）
        :param force: 忽略节流（配置刚改过时用）
        """
        if self.cfg.text("source_mode", "memory") != "memory":
            return
        if (not force and self._selfcheck_at is not None
                and started - self._selfcheck_at < SELFCHECK_INTERVAL_S):
            return
        self._selfcheck_at = started
        try:
            report = selfcheck_diagnose(self.cfg.text("mem_module", "auto"))
        except Exception as exc:  # noqa: BLE001 - 自检本身不能把轮询拖垮
            await self.log("info", "进程自检失败：%s" % exc)
            return
        if report is None:
            return
        level, text = report
        if text == self._selfcheck_text and not force and started - self._selfcheck_emit < 300:
            return
        self._selfcheck_text = text
        self._selfcheck_emit = started
        await self.log(level, text)

    async def _poll_loop(self) -> None:
        try:
            await asyncio.wait_for(self.config_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            await self.log("warning", "未收到 config，按默认配置启动")
        self._apply_config()

        if self.source_dirty:
            await self._rebuild_source_now()

        if self.channel_dirty:
            await self._apply_channels_if_dirty()

        await self._open_source()
        if self.session_index > 1:
            await self.log("info", "连接已恢复（第 %d 次）：重新下发当前输出" % self.session_index)
        await self._report_status(force=True)

        interval = max(0.004, min(0.5, self.cfg.num("mem_poll_ms", 8) / 1000.0))
        t0 = time.monotonic()
        no_signal = 0.0
        zeroed_for_signal = False

        while not self.stop.is_set():
            await self._note_dry()
            # 运行中改配置：指针链/通道映射只能在轮询任务里改（避免与读取/下发并发）。
            # 模拟运行期间不改剧本与数据源 —— 改动推迟到本轮模拟结束后生效。
            if self.source_dirty:
                if self.sim_run:
                    await self._sim_defer_warn()
                else:
                    await self._rebuild_source_now()
                    await self._open_source()
            if self.channel_dirty:
                await self._apply_channels_if_dirty()

            started = time.monotonic()
            tick = int((started - t0) * 1000)
            self._poll_estop()

            if isinstance(self.source, SimSource) and self._channel_overlap():
                # 通道重叠：警告 + 拒绝本次模拟输出（不读、不喂引擎、不下发、不出运行日志）
                await self._sim_refuse()
                await self._tick_sleep(started, interval)
                continue

            raw, note = (None, "数据源已关闭")
            if self.source is not None:
                try:
                    if isinstance(self.source, SimSource):
                        raw, note = self.source.read(tick)
                    else:
                        raw, note = self.source.read()
                except Exception as exc:  # noqa: BLE001
                    raw, note = None, "读取异常：%s" % exc
            self.source_note = note

            if (raw is None and isinstance(self.source, MemorySource)
                    and "未配置" not in note):
                # 读不到就先替用户判断是哪种情况（没启动 / 名字填错 / 附错进程 / 权限 / 授权未完成）。
                # 「字段未配置」是排版问题而不是进程问题，交给 _open_source 的提示，不在这里重复。
                await self._selfcheck(started)
            elif raw is not None and self._selfcheck_text:
                self._selfcheck_text = ""
                await self.log("info", "进程自检：读取已恢复")

            if isinstance(self.source, SimSource):
                if self.sim_done:
                    # 单次演练播完：不再读、不再循环、不再重复报。但状态仍要按时上报 ——
                    # 否则日志面板与状态卡彻底没动静，看起来像插件掉了。
                    await self._report_status()
                    await self._tick_sleep(started, interval)
                    continue
                pos = raw.get("_sim_pos") if raw else None
                wrapped = (pos is not None and self.sim_last_pos is not None
                           and pos < self.sim_last_pos)
                if not self.sim_run:
                    # 开场白必须排在解说行与运行日志之前
                    await self._begin_sim_run()
                elif wrapped:
                    # 剧本播完一轮 = 本次模拟结束；期间改过的剧本/数据源此刻才换
                    await self._end_sim_run()
                    switched = False
                    if self.source_dirty:
                        await self._rebuild_source_now()
                        await self._open_source()
                        switched = True
                    if not switched and getattr(self.source, "once", False):
                        # 演练剧本只跑一次：归零两通道后彻底停下（换剧本/换数据源/重播才会重新开始）
                        # 注意 switched：用户刚换的剧本要跑，不能因为旧剧本是 once 就把它也停掉
                        self.sim_done = True
                        await self._force_zero("单次演练结束")
                        if not self.sim_finished_noted:
                            self.sim_finished_noted = True
                            await self.log("info", "演练已完成：可切换剧本/数据源重新开始，"
                                                  "或点「🔧 维护 → 重播当前演练 → 立即重播」")
                        await self._tick_sleep(started, interval)
                        continue
                    await self._begin_sim_run()
                if raw and raw.get("_sim_log"):
                    await self.log("info", raw["_sim_log"])     # 剧本里夹的提示行（如"插入电池"）
                self.sim_last_pos = pos

            if raw:
                self.last_frame = raw
                self.frames += 1
                self.last_sample_ok = started
                no_signal = 0.0
                if not self._identity_logged and hasattr(self.source, "identity"):
                    self._identity_logged = True
                    await self.log("info", "数据源定位：%s" % self.source.identity())
                if zeroed_for_signal:
                    zeroed_for_signal = False
                    await self.log("info", "数据源恢复")
                sample = self._to_sample(tick, raw)
            else:
                no_signal = started - (self.last_sample_ok or started)
                self.last_error = note
                sample = self._lost_sample(tick)

            frame = self.engine.feed(sample, limits=Limits(), estop=self.estop)

            # 先写变更日志、再下发：这样「档位变了」的行排在（演练模式下的）「待下发」明细之前，
            # 读起来是「发生了什么 → 会发什么」。
            snapshot = dict(raw or {})
            snapshot.update({"t_ms": tick, "health": sample.health,
                             "max_health": sample.max_health, "doomed": sample.doomed,
                             "in_titan": sample.in_titan, "alive": sample.alive})
            await self._log_changes(frame, snapshot)

            await self._dispatch(frame)

            # 定期重发当前输出：设备断开/重连后 DGHub 侧强度层的实际值会丢，而插件按
            # (档位, 强度) 去重，缓存没变就不再发 —— 重发是幂等的，且脉冲在途时自动跳过。
            # 演练模式不下发任何命令，"设备侧强度层丢失"也就不存在：直接跳过，免得每 3 秒
            # 重复一遍「待下发」，看着像在反复输出。
            reassert_s = self.cfg.num("reassert_interval_s", 3)
            if (reassert_s > 0 and not self.cfg.flag("dry_run", False)
                    and started - self.last_reassert >= reassert_s
                    and self._reassert_idle()):
                self.last_reassert = started
                await self._reassert("定期重发")

            limit = self.cfg.num("no_signal_zero_s", 3)
            if self.source is not None and limit > 0 and no_signal > limit and not zeroed_for_signal:
                zeroed_for_signal = True
                await self._force_zero("数据源失联超过 %.0f 秒" % limit)

            if frame.flags.get("needs_relocate") and started - self.last_relocate_warn > 30:
                self.last_relocate_warn = started
                await self.log("error" if isinstance(self.source, MemorySource) else "warning",
                               self._relocate_text(frame))

            if self.doomed_flag_suspect is not None and started - self.last_suspect_warn > 30:
                self.last_suspect_warn = started
                hp, mx = self.doomed_flag_suspect
                self.doomed_flag_suspect = None
                await self.log("warning",
                               "濒死标志可疑：血量 %d/%d（%d%%）时标志为真，已按门槛 %.0f%% 忽略"
                               "（该地址在当前环境下不是布尔濒死位，建议重新标定，"
                               "或把「濒死标志」清空只用最大值塌缩判定）"
                               % (hp, mx, (hp * 100 // mx) if mx else 0,
                                  self.cfg.num("doomed_flag_max_hp_pct", 25)))

            if self.peak_bad_read is not None and started - self.last_peak_warn > 30:
                self.last_peak_warn = started
                bad = self.peak_bad_read
                self.peak_bad_read = None
                await self.log("warning",
                               "最大血量读数异常：%d（超过合理上限 %.0f）；已忽略、不参与峰值计算"
                               " —— 疑似切换瞬间的垃圾读数或地址错位"
                               % (bad, self.cfg.num("max_health_sane_cap", 20000)))

            await self._debug(note, raw)
            await self._report_status()

            await self._tick_sleep(started, interval)

    def _to_sample(self, tick: int, raw: dict) -> Sample:
        # 模拟剧本自带权威身份字段（在泰坦内/存活/濒死）；「未标定时推导」的兜底只属于内存模式
        trusted = bool(raw.get("_trusted"))
        # 内存模式下的读取失败由 _lost_sample 表达；剧本则用 readOk 字段直接表达
        read_ok = bool(raw.get("read_ok", True)) if trusted else True
        m25 = bool(raw.get("m25")) if self.cfg.flag("e_enable", True) else False
        health = int(raw.get("health", 0) or 0)
        max_health = int(raw.get("max_health", 0) or 0)
        if trusted or self.in_titan_configured:
            in_titan = bool(raw.get("in_titan", False))
        else:
            # 未标定「是否在泰坦内」时用最大血量推断：铁驭约 100、泰坦数千，分界清楚
            in_titan = max_health > self.cfg.num("in_titan_min_max_health", 1000)
        if trusted or self.cfg.text("off_alive", "").strip():
            alive = bool(raw.get("alive", True))
        else:
            # 未标定「是否存活」时用血量推导：泰坦被毁、铁驭阵亡时该地址都会归 0
            alive = health > 0
        # 濒死判据二：泰坦进濒死时「最大血量」会塌缩成濒死池（实测 12500 → 2500 = 1/5）。
        # 只靠标志位不够 —— 实测「下机后再上机」时标志位不会恢复成 1，而游戏本身仍判濒死。
        # 判据二有两个分支：
        #   ① 与本次驾驶的峰值比较（<= 1/3 视为塌缩）—— 覆盖正常玩法路径；
        #   ② 在泰坦内且最大值 <= 3500 —— 五种机体真实最大血量最低 7500（濒死池 = 1/5 最大 3500），
        #      所以这个量级只可能是濒死池；用来覆盖「插件启动时人已经在濒死泰坦里」。
        if not alive:
            self.peak_max = 0
        elif max_health > self.cfg.num("max_health_sane_cap", 20000) > 0:
            # 垃圾读数（加载/换机瞬间可能读到很大值）：绝不写进峰值 —— 峰值一旦被抬高，
            # collapse 判据（peak >= 3 × 当前最大值）就会永久成立，表现为"血量还有 60% 却输出濒死档"
            self.peak_bad_read = max_health
        elif max_health > self.peak_max:
            self.peak_max = max_health
        collapse = (not trusted) and in_titan and max_health > 0 and (
            self.peak_max >= 3 * max_health or max_health <= 3500)
        # 濒死判据一（标志位）要加可信度门槛：实测该字节不是"只在濒死时为真"的布尔位
        # （曾在 62% 血量、正在挨打时读到 bit0 置位），一旦采信就会直接跳到濒死档，
        # 70/50/30 三档全部没机会出现。所以要求"标志为真 **且** 血量比例 ≤ 门槛"。
        #
        # 比例必须拿**峰值**当分母：真濒死时血量与最大值会一起塌缩成濒死池（12500 与 2500 同时变成
        # 2500），此时 血量/当前最大值 = 100%，拿当前最大值当分母会把真濒死也拒掉。
        # 门槛填 100 = 恢复旧行为（永远采信）；填 0 = 只用"最大值塌缩"判定。
        flag_doomed = bool(raw.get("doomed", False))
        if flag_doomed and not trusted and max_health > 0:
            gate = self.cfg.num("doomed_flag_max_hp_pct", 25)
            reference = max(self.peak_max, max_health)
            if reference > 0 and health * 100 > reference * gate:
                flag_doomed = False
                self.doomed_flag_suspect = (health, reference)
        # 记录判定来源：排查"这个血量为什么判濒死"时，光看 doomed=1 分不清是标志还是塌缩
        if flag_doomed and collapse:
            self.last_doomed_src = "flag+collapse"
        elif flag_doomed:
            self.last_doomed_src = "flag"
        elif collapse:
            self.last_doomed_src = "collapse"
        else:
            self.last_doomed_src = ""
        return Sample(
            tick=tick,
            read_ok=read_ok,
            epoch=int(raw.get("epoch", 1) or 1),
            in_titan=in_titan,
            alive=alive,
            health=health,
            max_health=int(raw.get("max_health", 0) or 0),
            doomed=flag_doomed or collapse,
            m25=m25,
        )

    def _lost_sample(self, tick: int) -> Sample:
        """读取失败：沿用上一次的已知状态，但标记 read_ok=False（绝不当作血量 0）。"""
        last = self.last_frame or {}
        return Sample(
            tick=tick, read_ok=False,
            epoch=int(last.get("epoch", 1) or 1),
            in_titan=bool(last.get("in_titan", False)),
            alive=bool(last.get("alive", True)),
            health=int(last.get("health", 0) or 0),
            max_health=int(last.get("max_health", 0) or 0),
            doomed=bool(last.get("doomed", False)),
            m25=False,
        )

    async def _debug(self, note: str, raw) -> None:
        if not self.cfg.flag("debug_log", False):
            return
        now = time.monotonic()
        if now - self._debug_at < 1.0:
            return
        self._debug_at = now
        await self.log("debug", "读数：%s | %s" % (json.dumps(raw, ensure_ascii=False) if raw else "-", note))

    # ------------------------------------------------------------ 状态上报

    async def _report_status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._status_at < 1.0:
            return
        self._status_at = now

        src_ok = self.source is not None and bool(self.last_frame)
        steps = [{
            "key": "plugin", "title": "插件进程", "state": "ok",
            "detail": "已连接 DGHub（%s v%s）" % (self.manifest.get("name"), self.manifest.get("version")),
        }, {
            "key": "source", "title": "数据源", "state": "ok" if src_ok else "pending",
            "detail": self.source_note or "未启动",
            "hint": "" if src_ok else "内存定位区为空时不会输出任何刺激，请先标定血量指针链",
        }, {
            "key": "fields", "title": "内存字段", "state": self._fields_state(),
            "detail": self._fields_detail(),
        }, {
            "key": "engine", "title": "判定引擎", "state": "ok",
            "detail": "A=%s B=%s%s" % (self.engine.prev_decision["a"].slot if self.engine.prev_decision["a"] else "-",
                                       self.engine.prev_decision["b"].slot if self.engine.prev_decision["b"] else "-",
                                       "（急停中）" if self.estop else ""),
        }]

        a = self.engine.prev_decision["a"]
        if self.sim_done:
            # 演练播完后引擎已复位，别让状态卡显示"泰坦 待机"（那看着像没在跑）
            display = "演练已完成（%s）" % self._sim_name()
        elif self.sim_run:
            display = "演练中（%s）" % self._sim_name()
        else:
            display = "泰坦 %s" % (a.slot if a else "待机")
        limit_a, limit_src_a = self._pct_limit("a")
        await self.send({"op": "status", "fields": {
            "display_status": display,
            "source": self.source_note,
            "frames": self.frames,
            "device": "%s max=%d/%d（换算上限 %d·%s）" % (
                "已连接" if self.device_connected else "未连接",
                self.max_strength["a"], self.max_strength["b"], int(limit_a), limit_src_a),
            "estop": self.estop,
            "startup_check": {"title": "泰坦陨落2 联动 启动检查", "steps": steps},
        }})

    def _fields_state(self) -> str:
        if self.source is None:
            return "idle"
        return "ok" if getattr(self.source, "configured", False) else "warn"

    def _fields_detail(self) -> str:
        if self.source is None:
            return "未使用内存数据源"
        if getattr(self.source, "configured", False):
            return "血量/最大血量指针链已配置"
        return "未配置：插件保持静默（fail-closed），不会产生任何刺激"

    # ------------------------------------------------------------ 连接与退出

    async def _connect(self) -> None:
        url = "ws://%s:%s/ws/plugin?token=%s" % (DGHUB_HOST, DGHUB_PORT, DGHUB_TOKEN)
        while not self.stop.is_set():
            try:
                # 首条消息必须是 hello：文档规定非 HelloMsg 会以 1008 断开
                self.ws = await websockets.connect(url, ping_interval=20, ping_timeout=20)
                await self.ws.send(json.dumps({
                    "op": "hello", "token": DGHUB_TOKEN, "manifest": self.manifest,
                }, ensure_ascii=False))
                ack = json.loads(await self.ws.recv())
                if not ack.get("accepted"):
                    raise RuntimeError(ack.get("reason") or "hello 被拒绝")
                print("已连接 DGHub：%s" % url.replace(DGHUB_TOKEN, "***"))
                return
            except Exception as exc:  # noqa: BLE001
                print("连接失败：%s，2 秒后重试" % exc, file=sys.stderr)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

    def _reassert_idle(self) -> bool:
        """脉冲在途时不重发：重发会把脉冲的波形重新播一次。"""
        return self.engine.pulse is None and self.engine.e is None

    async def _reassert(self, reason: str) -> None:
        """重发当前输出状态（幂等）：清掉下发缓存并让引擎强制重新下发一次。

        解决的真实缺陷：设备断开后强度层归零，而插件按 (档位, 强度) 去重，缓存值没变
        就再也不会下发 —— 表现为「郊狼重连后没输出，必须关掉再打开插件」。
        """
        if not self._reassert_idle():
            return
        self.sent = {"a": None, "b": None}
        self.engine.force_redispatch()
        self.reassert_count += 1
        # 每 20 次（默认约 1 分钟）才记一行，够用作"插件仍在正常重发"的心跳，又不刷屏
        if self.cfg.flag("debug_log", False) and (self.reassert_count == 1
                                                  or self.reassert_count % 20 == 0):
            await self.log("debug", "重发当前输出（%s，累计 %d 次）" % (reason, self.reassert_count))

    def _reset_session(self) -> None:
        """一次新会话开始前：清掉上一会话的待发队列与下发缓存，并强制重推当前状态。

        DGHub 在插件断开时会清理该插件的强度层，所以重连后必须重新下发一次，
        否则设备会一直停在 0。
        """
        while not self.tx.empty():
            try:
                self.tx.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.sent = {"a": None, "b": None}
        self.engine.force_redispatch()
        self.device_logged = False
        self.max_hint_logged = False
        self.no_device_warned = False
        self._status_at = 0.0
        self.session_index += 1

    async def _shutdown(self) -> None:
        dry = self.cfg.flag("dry_run", False)
        if self.cfg.flag("zero_on_exit", True) and self.ws is not None:
            if dry:
                # 演练模式全程没向设备发过输出，收尾也不发包（保证「零命令」可核对）
                try:
                    await self._send_now({"op": "log", "level": "info",
                                          "message": "[演练] 待下发：两通道归零（退出）"})
                except Exception:  # noqa: BLE001 收尾日志失败不影响退出
                    pass
            else:
                try:
                    for target in ("a", "b"):
                        await self._send_now({"op": "set_strength", "channel": target, "pct": 0})
                    await asyncio.sleep(0.1)
                except Exception:  # noqa: BLE001
                    pass
        if self.source is not None:
            self.source.close()
        await self._close_ws()

    async def run(self) -> None:
        """会话管理：只在连接真的断了（宿主关闭、收发异常）时重新注册一次。

        注意：**不要**用「自己断开再重连」来实现任何自愈 —— 实测 DGHub 在插件自行断开后
        仍认为该插件有活跃连接，会一直拒绝重新注册（日志：远程插件 ttf2_coyote 已有活跃连接，
        拒绝重复注册），插件就此永久失去输出能力。输出层的自愈由 `_reassert` 负责。
        """
        try:
            while not self.stop.is_set():
                self.reconnect.clear()
                self.session_reason = ""
                await self._connect()
                if self.stop.is_set():
                    return
                self._reset_session()
                tasks = [asyncio.create_task(self._tx_loop(), name="tx"),
                         asyncio.create_task(self._rx_loop(), name="rx"),
                         asyncio.create_task(self._poll_loop(), name="poll")]
                reconnect_task = asyncio.create_task(self.reconnect.wait(), name="reconnect")
                stop_task = asyncio.create_task(self.stop.wait(), name="stop")
                group = tasks + [reconnect_task, stop_task]
                await asyncio.wait({reconnect_task, stop_task},
                                   return_when=asyncio.FIRST_COMPLETED)
                for task in group:
                    task.cancel()
                await asyncio.gather(*group, return_exceptions=True)
                if self.stop.is_set():
                    return          # 停止：交给 finally 归零后再关连接（顺序不能颠倒）
                await self._close_ws()
                print("连接中断（%s）—— 重新注册插件" % (self.session_reason or "未知原因"), flush=True)
                # 关闭帧已经发出，等宿主完成注销后再注册（否则会被判为重复注册）
                await asyncio.sleep(1.0)
        finally:
            await self._shutdown()


def main() -> int:
    if not DGHUB_TOKEN:
        print("缺少 DGHUB_TOKEN：请由 DGHub 启动，或手动设置环境变量后运行", file=sys.stderr)
        return 2
    try:
        manifest = _load_manifest()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    runtime = Runtime(manifest)
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
