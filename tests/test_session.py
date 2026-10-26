# -*- coding: utf-8 -*-
"""离线验证会话与输出重发（main.Runtime）。

背景（实测结论）：
* DGHub 在插件**自行**断开连接后仍认为该插件有活跃连接，会一直拒绝重新注册
  （宿主日志：远程插件已有活跃连接，拒绝重复注册）——所以插件绝不能靠"自己断线重连"自愈；
* 郊狼断开/重连后 DGHub 侧强度层的实际值会丢，而插件按 (档位, 强度) 去重，缓存值没变
  就再也不会下发 —— 表现为「重连后没输出、必须关掉再打开插件」。
  自愈手段是**幂等地定期重发**当前输出。

覆盖：不自动重建会话、定期重发按周期发生且值不变、间隔 0 时只发一次、手动重发、停止清理、
通道互斥（冲突让路 + 界面同步）与运行时切换通道（先归零再重发）。
不联网、不需要 DGHub：websockets 用最小桩替换，_connect 也被替换。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_ws_mod = types.ModuleType("websockets")


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration


async def _fake_connect(*_args, **_kwargs):
    return FakeWS()


_ws_mod.connect = _fake_connect
sys.modules.setdefault("websockets", _ws_mod)

import main as plugin_main  # noqa: E402


class FakeSource:
    """固定读数：泰坦 8000/12500 → A 通道 70% 档（默认 20）。"""

    def __init__(self, health: int = 8000, max_health: int = 12500) -> None:
        self.health = health
        self.max_health = max_health

    def read(self):
        return ({"health": self.health, "max_health": self.max_health, "doomed": False,
                 "in_titan": True, "alive": True, "epoch": 1}, "测试读数")

    def close(self) -> None:
        self.closed = True

    def identity(self) -> str:
        return "测试源"


async def _fake_connect(self) -> None:  # noqa: ANN001
    self.sessions += 1
    self.ws = FakeWS()
    self.ws_log.append(self.ws)


async def _noop(**_kwargs) -> None:
    return None


def _make_runtime(interval: float, source=None, config=None, real_apply: bool = False,
                  real_source: bool = False, real_status: bool = False):
    rt = plugin_main.Runtime({"id": "ttf2_coyote", "config_schema": []})
    base = {"source_mode": "off", "reassert_interval_s": interval,
            "change_log": False, "debug_log": False}
    if config:
        base.update(config)
    rt.cfg.update(base)
    rt.config_ready.set()
    rt.sessions = 0
    rt.source_calls = 0
    rt.ws_log = []                 # 会话结束时 self.ws 会被置空，因此保留每代会话的连接对象
    plugin_main.Runtime._connect = _fake_connect
    if not real_apply:
        rt._apply_config = lambda: None
    if not real_status:
        rt._report_status = _noop

    if real_source:
        real_make_source = plugin_main.Runtime._make_source

        def counting_make_source():
            rt.source_calls += 1
            return real_make_source(rt)

        rt._make_source = counting_make_source
    else:
        def fake_make_source():
            rt.source_calls += 1
            return source, "测试数据源"

        rt._make_source = fake_make_source
    return rt


def _strength_frames(rt) -> list[tuple]:
    """下发过的 (channel, pct) 序列（跨全部会话累计）。"""
    out = []
    for conn in getattr(rt, "ws_log", []):
        for raw in conn.sent:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("op") == "set_strength":
                out.append((msg.get("channel"), msg.get("pct")))
    return out


async def case_no_auto_rebuild() -> None:
    """关键回归：插件绝不会为了自愈而重建会话（那会让它永久失去输出能力）。"""
    rt = _make_runtime(interval=1.0, source=FakeSource())
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(4.5)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)
    assert rt.sessions == 1, "不应发生自动重建会话，实际 %d 次" % rt.sessions
    assert rt.source_calls == 1, "数据源只应创建一次，实际 %d 次" % rt.source_calls
    print("ok   不自动重建会话：会话数=%d，数据源=%d 次" % (rt.sessions, rt.source_calls))


async def case_reassert_cadence() -> None:
    """定期重发：节奏正确、值幂等不变。"""
    rt = _make_runtime(interval=1.0, source=FakeSource())
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.4)
    # 未收到 device_info 时按郊狼上限 200 换算：条目 20 → 10%
    expect_a = rt._pct("a", rt.engine.cfg.tier_intensity["A_70"])
    first = len(_strength_frames(rt))
    assert first >= 2, "首帧应下发两个通道，实际 %d 条" % first
    await asyncio.sleep(3.6)
    frames = _strength_frames(rt)          # 停止前的快照（停止时会追加两通道归零消息）
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)
    assert len(frames) >= first + 6, "定期重发未生效：%d → %d 条" % (first, len(frames))
    a_values = {pct for ch, pct in frames if ch == "a"}
    assert a_values == {expect_a}, "A 通道重发值应恒为 %d，实际 %s" % (expect_a, sorted(a_values))
    assert {pct for _, pct in frames} == {expect_a, 0}, "只应出现当前档位值与 0"
    print("ok   定期重发：4 秒内下发 %d 条，A 通道恒为 %d（幂等）" % (len(frames), expect_a))


async def case_disabled() -> None:
    """间隔 0：只在下发状态变化时发送。"""
    rt = _make_runtime(interval=0, source=FakeSource())
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.4)
    expect_a = rt._pct("a", rt.engine.cfg.tier_intensity["A_70"])
    count = lambda: sum(1 for ch, pct in _strength_frames(rt) if ch == "a" and pct == expect_a)
    before = count()
    assert before >= 1, "首帧应下发 A 通道"
    await asyncio.sleep(3.0)
    after = count()
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)
    assert after == before, "关闭定期重发后 A 通道不应重复下发：%d → %d" % (before, after)
    print("ok   间隔 0：不再重发（A 通道仍为 %d 条）" % after)


async def case_manual() -> None:
    """手动重发：立刻补发一次当前输出。"""
    rt = _make_runtime(interval=0, source=FakeSource())
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.4)
    before = len(_strength_frames(rt))
    await rt._reassert("用户手动触发")
    await asyncio.sleep(0.3)
    after = len(_strength_frames(rt))
    assert after == before + 2, "手动重发应补发两个通道，实际 %d → %d" % (before, after)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)
    print("ok   手动重发：%d → %d 条" % (before, after))


async def case_shutdown_zeroes() -> None:
    """停止时两通道归零，并显式关闭连接。"""
    rt = _make_runtime(interval=0, source=FakeSource())
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.4)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)
    tail = _strength_frames(rt)[-2:]
    assert tail == [("a", 0), ("b", 0)], "退出前应两通道归零，实际 %s" % (tail,)
    assert any(getattr(c, "closed", None) is not None for c in rt.ws_log), "退出时应显式 close 连接"
    print("ok   退出归零并关闭连接：%s" % (tail,))


async def case_doomed_derivation() -> None:
    """濒死判据：标志位不够用，必须叠加「最大血量塌缩」。

    实测：下机后再上机回到濒死泰坦时，标志位不恢复成 1（游戏本身仍判濒死、血量/最大值
    都是濒死池 2500），于是 A 通道被误判成 0 档。因此以「最大值塌缩到峰值 1/3 以下」兜底。
    """
    rt = _make_runtime(interval=0, source=FakeSource())
    base = {"epoch": 1, "alive": True}

    def doomed_of(**raw):
        merged = dict(base)
        merged.update(raw)
        return rt._to_sample(0, merged).doomed

    assert doomed_of(health=12500, max_health=12500, in_titan=True) is False, "健康泰坦不得判濒死"
    assert doomed_of(health=2500, max_health=2500, in_titan=True) is True, \
        "最大值塌缩成濒死池（12500→2500）必须判为濒死（标志位此时可能是 0）"
    assert doomed_of(health=1000, max_health=2500, in_titan=True) is True, "濒死池内继续被打仍属濒死"
    assert doomed_of(health=12500, max_health=12500, in_titan=True) is False, "恢复后不得再判濒死"
    assert doomed_of(health=100, max_health=100, in_titan=False) is False, "铁驭状态不得判濒死"
    assert doomed_of(health=2500, max_health=2500, in_titan=True, doomed=True) is True, "标志位为真时同样判濒死"
    # 插件启动时人已经在濒死泰坦里：峰值还没建立，靠"泰坦内最大值 <= 3500"兜底
    fresh = _make_runtime(interval=0, source=FakeSource())
    fresh.cfg.update({"off_doomed": "engine.dll+111E20C4"})
    assert fresh._to_sample(0, dict(base, health=2500, max_health=2500, in_titan=True)).doomed is True, \
        "峰值未建立时（插件刚启动且已在濒死泰坦内）必须仍能判出濒死"
    assert fresh._to_sample(1, dict(base, health=12500, max_health=12500, in_titan=True)).doomed is False, \
        "正常泰坦（最大值 12500）不得判濒死"
    # 泰坦被毁（alive=False）后峰值复位：下一条命从自己的最大值重新起算
    doomed_of(health=0, max_health=2500, in_titan=True, alive=False)
    assert doomed_of(health=7500, max_health=7500, in_titan=True, alive=True) is False, \
        "换机体后（峰值已复位）不得误判濒死"
    print("ok   濒死判据：标志位 + 最大值塌缩（含恢复与换机体）")


async def case_module_cache() -> None:
    """模块基址缓存：**失败结果不得缓存**。

    实测：Northstar 启动器先起进程、几秒后才载入 engine.dll。若此时把"找不到模块"缓存下来，
    因为 PID 一直活着、_ensure_open 会一直提前返回成功，这个负面缓存就永不刷新 ——
    实测后果：切到官服（-vanilla 启动）后插件永久失效、日志刷"找不到模块 engine.dll"。
    """
    from source import MemorySource

    ms = MemorySource(lambda k, d=None: {"mem_module": "auto"}.get(k, d))
    ms._pid = 4242
    answers = [None, 0x7FFA92620000]
    calls = []

    def fake_find(pid, name):
        calls.append(name)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    ms._find_module_base = fake_find
    assert ms._module_base("engine.dll") is None, "第一次找不到时应返回 None"
    assert "engine.dll" not in ms._module_bases, "失败结果不得写入缓存"
    base = ms._module_base("engine.dll")
    assert base == 0x7FFA92620000, "模块载入后应立刻能取到基址，实际 %r" % base
    assert ms._module_bases["engine.dll"] == base, "成功结果应缓存"
    ms._find_module_base = lambda pid, name: None
    assert ms._module_base("engine.dll") == base, "已有成功缓存时不再重复查询"
    print("ok   模块基址缓存：失败不缓存、成功可复用（官服切服失效的根因）")


def _log_messages(rt) -> list[str]:
    """插件发过的日志文本（跨全部会话累计）。"""
    out = []
    for conn in getattr(rt, "ws_log", []):
        for raw in conn.sent:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("op") == "log":
                out.append(str(msg.get("message", "")))
    return out


def _status_frames(rt) -> list[dict]:
    """插件上报过的状态字段（跨全部会话累计）。"""
    out = []
    for conn in getattr(rt, "ws_log", []):
        for raw in conn.sent:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("op") == "status":
                out.append(msg.get("fields") or {})
    return out


def _config_frames(rt) -> dict:
    """下发过的 set_config（跨全部会话累计，后写覆盖先写）。"""
    out = {}
    for conn in getattr(rt, "ws_log", []):
        for raw in conn.sent:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("op") == "set_config":
                out[msg.get("key")] = msg.get("value")
    return out


async def case_channel_exclusive() -> None:
    """通道互斥：受击/死亡脉冲不得占用持续输出那条通道。

    一条通道同时只能有一种输出：两路都压在同一条上时，后发的 set_strength 会覆盖先发的，
    表现为"档位强度和受击脉冲互相打断"。规则是**持续输出优先**，脉冲让路，并把界面值改回来。
    """
    rt = _make_runtime(interval=0, source=FakeSource(), real_apply=True,
                       config={"a_channel": "b", "b_channel": "b", "e_channel": "b"})
    plan, fixes = rt._resolve_channels()
    assert plan == {"level": "b", "pulse": "a", "e": "a"}, \
        "受击与死亡脉冲必须让到另一条通道，实际 %s" % (plan,)
    assert fixes == {"b_channel": "a", "e_channel": "a"}, "冲突项应登记为待纠正，实际 %s" % (fixes,)

    # 设备两通道上限不同：百分比必须按「实际下发到的通道」换算
    rt.device_info_received = True
    rt.max_strength = {"a": 79.0, "b": 89.0}

    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.5)
    applied = rt.channels
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    assert applied == plan, "运行时映射应与解析结果一致，实际 %s" % (applied,)
    writes = _config_frames(rt)
    assert writes.get("b_channel") == "a", "界面应被改成实际通道，实际 %s" % (writes,)
    assert writes.get("e_channel") == "a", "死亡脉冲同样要同步，实际 %s" % (writes,)

    expect_b = rt._pct("b", rt.engine.cfg.tier_intensity["A_70"])
    expect_a = rt._pct("a", rt.engine.cfg.tier_intensity["A_70"])
    assert expect_a != expect_b, "用例前提：两通道上限不同，换算结果应不同"
    levels = [(ch, pct) for ch, pct in _strength_frames(rt) if pct == expect_b]
    assert levels and {ch for ch, _ in levels} == {"b"}, \
        "持续输出只应落在 B 通道且按 B 的上限换算，实际 %s" % (levels,)
    assert not [pct for ch, pct in _strength_frames(rt) if ch == "b" and pct == expect_a], \
        "不得用 A 通道的上限去换算下发给 B 的强度"
    print("ok   通道互斥：持续=B 受击=A，界面同步纠正 %s（B 通道按 89 换算）" % (writes,))


async def case_channel_switch_zeroes() -> None:
    """运行时切换通道：先两通道归零，再按新映射重新下发。

    不先归零的话，被释放通道上会留着上一次的档位强度 —— 那正是"两条输出互相覆盖"的来源。
    """
    rt = _make_runtime(interval=0, source=FakeSource(), real_apply=True)
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.5)
    expect = rt._pct("a", rt.engine.cfg.tier_intensity["A_70"])
    before = _strength_frames(rt)
    assert ("a", expect) in before, "默认配置下持续输出应在 A 通道，实际 %s" % (before,)

    rt.cfg["a_channel"] = "b"          # 用户把持续输出改到 B 通道
    rt._apply_config()
    await asyncio.sleep(0.5)
    new = _strength_frames(rt)[len(before):]
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    assert new[:2] == [("a", 0), ("b", 0)], "切换映射前应先两通道归零，实际 %s" % (new[:2],)
    expect_b = rt._pct("b", rt.engine.cfg.tier_intensity["A_70"])
    assert ("b", expect_b) in new, "持续输出应改发 B 通道，实际 %s" % (new,)
    assert {pct for ch, pct in new if ch == "a"} <= {0}, \
        "A 通道释放后不得再有非零输出，实际 %s" % (new,)
    assert rt.channels == {"level": "b", "pulse": "a", "e": "a"}, \
        "切到 B 后受击应让到 A，实际 %s" % (rt.channels,)
    print("ok   切换通道：先归零 %s，再改发 B 通道（受击让到 A）" % (new[:2],))


async def case_source_rebuild_midrun() -> None:
    """运行中改指针链：必须立刻重建数据源。

    旧实现只在轮询任务**启动时**检查一次 dirty 标记，等于「填好偏移必须重启插件」——
    这正是标定时最折磨人的毛病。
    """
    src = FakeSource()
    rt = _make_runtime(interval=0, source=src, real_apply=True)
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.4)
    assert rt.source_calls == 1, "启动时应只建一次数据源，实际 %d 次" % rt.source_calls

    rt.cfg["off_health"] = "engine.dll+111E1E68"      # 用户标定/修改指针链
    rt._apply_config()
    await asyncio.sleep(0.5)
    calls = rt.source_calls
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    assert calls == 2, "运行中改指针链应立即重建数据源，实际 %d 次" % calls
    assert getattr(src, "closed", False) is True, "旧数据源应被关闭"
    print("ok   运行中改指针链：数据源重建 %d 次（旧源已关闭）" % calls)


async def case_dry_run_logs_instead_of_sending() -> None:
    """演练模式：不向设备发任何命令，但把「待下发」打进日志，并明确提示"不发包"。

    波形名（preset）与换算后的百分比本来只出现在设备命令里、从不进变更日志，
    所以演练模式必须自己把待发命令记下来，否则没法在不下发的情况下核对波形与强度。
    同时要**只记一次**：定期重发在演练模式下没有意义（没有设备侧强度层可丢），
    否则每 3 秒重复一遍，看起来像在反复输出。
    """
    rt = _make_runtime(interval=0, source=FakeSource(), real_apply=True,
                       config={"dry_run": True, "reassert_interval_s": 0.2})
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(1.2)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    assert _strength_frames(rt) == [], "演练模式不得下发 set_strength，实际 %s" % (_strength_frames(rt),)
    logs = _log_messages(rt)
    assert any("演练模式已开启" in m and "不向设备发送任何命令" in m for m in logs), \
        "必须明确提示「开了演练模式就不会发包」，实际日志 %s" % (logs[:6],)
    strength_lines = [m for m in logs if "待下发" in m and "强度" in m]
    assert len(strength_lines) == 1, \
        "演练模式下同一档位只应记一次待下发（定期重发应跳过），实际 %d 条：%s" % (len(strength_lines), strength_lines)
    assert any("两通道归零（退出）" in m for m in logs), \
        "退出时也应记录待下发的归零命令，实际 %s" % (logs[-3:],)
    print("ok   演练模式：不发命令、明确提示、只记一次「待下发」（%d 条日志）" % len(logs))


async def case_log_console_encoding() -> None:
    """日志回显要扛住 GBK 控制台：emoji 编不进去时降级，不能连整条日志一起吞掉。

    中文 Windows 的控制台和 DGHub 捕获的管道默认都是 GBK，`print` 写 emoji 会抛
    UnicodeEncodeError。「演练已完成：…🔧 维护…」这条就是这样被吞的 —— 演练播完看起来像卡住了。
    """
    rt = _make_runtime(interval=0)
    text = "演练已完成：可切换剧本/数据源重新开始，或点「🔧 维护 → 重播当前演练 → 立即重播」"
    sent = []

    async def capture(message):
        sent.append(message)

    rt.send = capture

    class GbkStdout:
        """只接受 GBK 的标准输出替身：装不下的字符立刻抛错（与真实控制台一致）。"""

        encoding = "gbk"

        def __init__(self) -> None:
            self.buf = []

        def write(self, s: str) -> int:
            s.encode("gbk")
            self.buf.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    real_stdout = sys.stdout
    fake = GbkStdout()
    sys.stdout = fake
    try:
        await rt.log("info", text)
    finally:
        sys.stdout = real_stdout

    echoed = "".join(fake.buf)
    assert "演练已完成" in echoed, "中文部分必须照样回显，实际 %r" % (echoed,)
    assert "🔧" not in echoed, "GBK 装不下的 emoji 要降级掉，实际 %r" % (echoed,)
    assert [m.get("message") for m in sent] == [text], \
        "日志面板收到的必须是完整原文（含 emoji），实际 %r" % (sent,)
    print("ok   日志回显：GBK 控制台下 emoji 降级、中文保留、面板仍收完整原文")


async def case_relocate_hint() -> None:
    """「连续读到无效数据」提示：要说明是哪条校验挂了，并在读数恢复后自动撤销。

    旧行为只有一句"指针链可能已失效"且**永不撤销**：无论原因是不在对局、进程已关闭还是
    地址错位，日志都每 30 秒刷同一句话，排查方向被带偏。
    """
    engine = plugin_main.Engine(plugin_main.Config())
    base = dict(read_ok=True, epoch=1, in_titan=True, alive=True,
                health=999999, max_health=5000, doomed=False, m25=False)

    def feed(tick, **over):
        sample = dict(base)
        sample.update(over)
        sample["tick"] = tick
        return engine.feed(plugin_main.Sample(**sample), limits=plugin_main.Limits())

    frame = None
    for i in range(5):
        frame = feed(i * 8)
    assert frame.flags["needs_relocate"] is True, "连续 5 帧无效应提示重新定位"
    assert "超范围" in frame.flags["invalid_reason"], \
        "提示要带具体原因，实际 %r" % frame.flags["invalid_reason"]

    frame = feed(3100, health=5000)          # 读数恢复并稳定 3 秒
    assert frame.flags["needs_relocate"] is False, "读数稳定后应撤销提示，否则会无限重播"
    assert frame.flags["invalid_reason"] == "", "撤销后不应再留着旧原因"

    other = plugin_main.Engine(plugin_main.Config())
    f = other.feed(plugin_main.Sample(
        tick=0, read_ok=True, epoch=1, in_titan=False, alive=True, health=100,
        max_health=0, doomed=False, m25=False), limits=plugin_main.Limits())
    assert "最大血量" in f.flags["invalid_reason"], \
        "最大值没读到泰坦量级要单独说明（常见于不在对局），实际 %r" % f.flags["invalid_reason"]
    print("ok   无效数据提示：带具体原因、读数恢复后自动撤销")


async def case_sim_run_framing() -> None:
    """循环剧本（S12，不在面板下拉里）：每轮都有配对的开场白与结束语。

    面板上的五个剧本都是演练式（只跑一次，见 case_rehearsal_run_once），所以循环行为用
    S12 覆盖：它 200ms 走完 → 一轮 1700ms，跑 2 秒能看到两轮。
    """
    rt = _make_runtime(interval=0, real_apply=True, real_source=True,
                       config={"source_mode": "sim", "sim_scenario": "S12", "change_log": True})
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(2.0)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    logs = _log_messages(rt)
    starts = [i for i, m in enumerate(logs) if m.startswith("现在处于模拟状态")]
    ends = [i for i, m in enumerate(logs) if m == "此次模拟场景输出已结束"]
    body = [i for i, m in enumerate(logs) if " slot=" in m]

    assert len(starts) >= 2, "循环剧本应每轮重新开场，实际 %d 次" % len(starts)
    assert starts[0] == 0 or starts[0] < body[0], "开场白必须排在运行日志之前"
    assert ends, "缺少结束语，实际日志 %s" % (logs[-6:],)
    assert body, "开场白与结束语之间应有运行日志"
    assert starts[0] < body[0] < ends[0], \
        "顺序应为 开场白 → 运行日志 → 结束语，实际 %s / %s / %s" % (starts[:2], body[:2], ends[:2])
    print("ok   循环剧本：开场白 %d 次、结束语 %d 次、运行日志 %d 行，顺序正确"
          % (len(starts), len(ends), len(body)))


async def case_scenario_format() -> None:
    """面板上五个剧本都必须是「演练格式」：once + 中文解说行。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "testvectors", "scenarios.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    missing = []
    for sid in ("R1", "S1", "S2", "S4", "S10"):
        sc = next((s for s in data["scenarios"] if s["id"].startswith(sid)), None)
        assert sc is not None, "剧本 %s 不存在" % sid
        if not sc.get("once"):
            missing.append("%s 缺 once" % sid)
        if not any(step.get("note_log") for step in sc["steps"]):
            missing.append("%s 缺解说行" % sid)
    assert not missing, "演练格式不完整：%s" % missing

    looping = [s["id"] for s in data["scenarios"] if not s.get("once")]
    assert len(looping) >= 10, "未在面板上的剧本应保持循环，实际 %s" % looping
    print("ok   剧本格式：面板五个剧本均为 once + 解说行（其余 %d 个保持循环）" % len(looping))


async def case_sim_refuses_on_channel_overlap() -> None:
    """通道重叠：警告 + 拒绝本次模拟输出（不读、不喂引擎、不下发、不出运行日志）。"""
    rt = _make_runtime(interval=0, real_apply=True, real_source=True,
                       config={"source_mode": "sim", "sim_scenario": "S10", "change_log": True,
                               "a_channel": "b", "b_channel": "b"})   # 两者都指向 B 通道
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(1.0)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    logs = _log_messages(rt)
    assert any("通道重叠" in m and "已拒绝" in m for m in logs), \
        "应警告通道重叠并说明已拒绝，实际 %s" % (logs[:6],)
    assert not any(m.startswith("现在处于模拟状态") for m in logs), "被拒绝时不得输出开场白"
    assert not any(" slot=" in m for m in logs), "被拒绝时不得产生模拟运行日志"
    assert not any(m == "此次模拟场景输出已结束" for m in logs), "被拒绝时不得输出结束语"
    # 允许退出时的两通道归零（那是安全收尾，不是模拟输出）
    nonzero = [f for f in _strength_frames(rt) if f[1] != 0]
    assert nonzero == [], "被拒绝时不得下发任何模拟输出，实际 %s" % (nonzero,)
    assert _config_frames(rt) == {}, \
        "模拟模式下不悄悄改回界面值（用户要看到自己选了重叠的通道），实际 %s" % (_config_frames(rt),)
    print("ok   通道重叠：警告 + 拒绝本次模拟（无开场白、无运行日志、无下发）")


async def case_sim_defers_scenario_change() -> None:
    """模拟运行期间不能改剧本：改动被推迟到本轮结束后生效。"""
    rt = _make_runtime(interval=0, real_apply=True, real_source=True,
                       config={"source_mode": "sim", "sim_scenario": "S10", "change_log": True})
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.6)                    # 第一轮跑到一半
    rt.cfg["sim_scenario"] = "S4"
    rt._apply_config()
    await asyncio.sleep(0.3)
    mid = _log_messages(rt)
    await asyncio.sleep(2.6)                    # 等本轮结束、下一轮开场
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)
    logs = _log_messages(rt)

    assert any("本次模拟结束后生效" in m for m in mid), \
        "运行中改剧本应提示推迟，实际 %s" % (mid[-4:],)
    assert any(m.startswith("现在处于模拟状态，S4") for m in logs), \
        "本轮结束后应出现新剧本的开场白，实际 %s" % ([m for m in logs if m.startswith("现在")][:4],)
    print("ok   运行中改剧本：推迟到本次模拟结束后生效（新剧本开场白已出现）")


async def case_rehearsal_run_once() -> None:
    """演练剧本（R1）：开场白 → 30% 档两通道 → 电池提示 → 50% 档两通道 → 结束语，只跑一次。

    要求（用户）：单次演练报一次就行，不要重复报；两个档位都要把两个通道的波形与强度报出来。
    """
    rt = _make_runtime(interval=0, real_apply=True, real_source=True, real_status=True,
                       config={"source_mode": "sim", "sim_scenario": "R1", "change_log": True,
                               "dry_run": True, "b_preset": "CS2-受伤", "e_preset": "CS2-受伤",
                               # 演练剧本的伤害是小额（<1500），这里按旧触发规则验证输出与日志格式
                               "b_hit_min_delta": 0, "b_burst_delta": 0})
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(5.0)          # 剧本 2100ms + 1500ms 尾巴 ≈ 3.6 秒播完
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    logs = _log_messages(rt)
    head = "现在处于模拟状态，R1，以下为模拟运行产生的日志。"
    assert logs.count(head) == 1, "演练只应开场一次，实际 %s" % [m for m in logs if m.startswith("现在处于")]
    assert logs.count("此次模拟场景输出已结束") == 1, "演练只应结束一次"
    assert "插入电池，泰坦生命值恢复" in logs, "缺少电池提示行"

    i_start = logs.index(head)
    i_bat = logs.index("插入电池，泰坦生命值恢复")
    i_end = logs.index("此次模拟场景输出已结束")
    a30 = next(i for i, m in enumerate(logs) if "slot=A_30" in m)
    a50 = next(i for i, m in enumerate(logs) if "slot=A_50" in m)
    assert i_start < a30 < i_bat < a50 < i_end, \
        "顺序应为 开场白→30% 档→电池→50% 档→结束语，实际 %s" % ([i_start, a30, i_bat, a50, i_end],)

    assert len([m for m in logs if "slot=B_HIT" in m]) == 2, "30% 与 50% 两档应各出一次受击脉冲"
    assert any("郊狼值 40" in m for m in logs), "缺少 30% 档的 A 通道强度（40）"
    assert any("郊狼值 30" in m for m in logs), "缺少 50% 档的 A 通道强度（30）"
    assert len([m for m in logs if "波形「CS2-受伤」" in m]) == 2, "两档应各报一次 B 通道波形"
    assert rt.sim_done is True, "once 剧本播完应停下（不重复报）"
    assert any("演练已完成" in m for m in logs), \
        "播完要明确提示已完成，并给出重播入口，否则看起来像插件掉了"
    displays = [f.get("display_status", "") for f in _status_frames(rt)]
    assert any("演练已完成" in d for d in displays), \
        "状态卡应显示「演练已完成」，实际最近几次 %s" % (displays[-3:],)
    print("ok   演练剧本：30%% 档 → 电池 → 50%% 档 → 结束，只跑一次（%d 条日志）" % len(logs))


async def case_replay_after_finish() -> None:
    """演练播完可以重播：重播入口重建数据源，重新开场再放一遍。"""
    rt = _make_runtime(interval=0, real_apply=True, real_source=True,
                       config={"source_mode": "sim", "sim_scenario": "S4", "change_log": True})
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(2.2)                 # S4：200ms + 1500ms 尾巴 ≈ 1.7 秒播完
    assert rt.sim_done is True, "S4 播完应停下"
    before = [m for m in _log_messages(rt) if m.startswith("现在处于模拟状态")]
    rt._request_replay()
    await asyncio.sleep(1.0)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    after = [m for m in _log_messages(rt) if m.startswith("现在处于模拟状态")]
    assert len(after) == len(before) + 1, \
        "重播应重新开场一次：%d → %d 次" % (len(before), len(after))
    print("ok   重播：演练播完 → 重播入口 → 重新开场（%d → %d 次）" % (len(before), len(after)))


async def case_doomed_flag_gate() -> None:
    """濒死标志要加可信度门槛：它在非濒死血量上置位时不得跳到濒死档。

    实测（2026-10-16）：`engine.dll+111E20C4` 在同一局里 62% 血量、正在挨打时读到 1，
    安静时读到 2 —— 不是布尔濒死位。旧逻辑一旦采信就直接进 A_DOOM，70/50/30 三档全部消失。
    """
    rt = _make_runtime(interval=0, source=FakeSource())
    base = {"epoch": 1, "alive": True, "in_titan": True}

    def doomed_of(**raw):
        merged = dict(base)
        merged.update(raw)
        return rt._to_sample(0, merged).doomed

    # 门槛默认 25%：62% 血量上的标志不采信
    assert doomed_of(health=7800, max_health=12500, doomed=True) is False, \
        "62% 血量时标志为真不得判濒死（否则 50/30 档永远不出现）"
    assert rt.doomed_flag_suspect == (7800, 12500), "应记录可疑标志用于诊断，实际 %r" % (rt.doomed_flag_suspect,)
    # 真濒死（濒死池 2500/2500 —— 注意此时血量与最大值**一起塌缩**，比例是 100%）
    assert doomed_of(health=2500, max_health=2500, doomed=True) is True, "真濒死时标志仍须生效"
    assert "flag" in rt.last_doomed_src, \
        "真濒死时门槛必须采信标志（分母要用峰值，不能用已塌缩的当前最大值），实际来源 %r" % (rt.last_doomed_src,)
    assert doomed_of(health=2000, max_health=12500, doomed=True) is True, "血量 16% 时标志可信"
    # 门槛填 100 = 恢复旧行为
    rt.cfg["doomed_flag_max_hp_pct"] = 100
    assert doomed_of(health=7800, max_health=12500, doomed=True) is True, "门槛 100 应恢复「永远采信」"
    # 门槛填 0 = 完全不用标志（只靠最大值塌缩）
    rt.cfg["doomed_flag_max_hp_pct"] = 0
    assert doomed_of(health=5000, max_health=12500, doomed=True) is False, "门槛 0 时标志完全不参与"
    assert doomed_of(health=2000, max_health=12500, doomed=True) is False, \
        "门槛 0 且最大值没塌缩 → 不判濒死"
    assert doomed_of(health=2500, max_health=2500, doomed=True) is True, \
        "门槛 0 时「最大值塌缩」这条仍要判濒死"
    rt.cfg["doomed_flag_max_hp_pct"] = 25
    assert doomed_of(health=2500, max_health=2500) is True, "门槛不参与时最大值塌缩仍应判濒死"
    print("ok   濒死标志门槛：62% 血不采信、真濒死仍生效、可调 0/25/100")


async def case_peak_garbage_guard() -> None:
    """峰值护栏：垃圾「最大血量」读数不得抬高峰值，否则塌缩判据会永久成立。

    实测线索（2026-10-16）：出现「血量 62% 却直接输出濒死档」。候选根因之一是峰值被加载/
    换机瞬间的垃圾最大血量抬高（`peak >= 3 × 当前最大值` → collapse 永久为真），而这条判据
    **不受**「濒死标志可信度门槛」保护，所以必须单独设护栏。
    """
    rt = _make_runtime(interval=0, source=FakeSource())
    base = {"epoch": 1, "alive": True, "in_titan": True}

    def sample(**raw):
        merged = dict(base)
        merged.update(raw)
        return rt._to_sample(0, merged)

    s = sample(health=12500, max_health=12500)      # 满血建立峰值
    assert rt.peak_max == 12500, "峰值应为 12500，实际 %r" % (rt.peak_max,)
    assert s.doomed is False and rt.last_doomed_src == "", "满血不得判濒死"

    sample(health=12500, max_health=999999)         # 换机瞬间的垃圾读数
    assert rt.peak_max == 12500, "垃圾读数不得抬高峰值，实际 %r" % (rt.peak_max,)
    assert rt.peak_bad_read == 999999, "应记录垃圾读数用于诊断，实际 %r" % (rt.peak_bad_read,)

    s = sample(health=7800, max_health=12500)       # 62% 血
    assert s.doomed is False, "62% 血不得判濒死（峰值未被污染）"
    assert rt.last_doomed_src == "", "未判濒死时来源应为空，实际 %r" % (rt.last_doomed_src,)

    s = sample(health=2400, max_health=2500)        # 真濒死（最大值塌缩成濒死池）
    assert s.doomed is True, "真濒死必须判濒死"
    assert rt.last_doomed_src == "collapse", \
        "判定来源应记为 collapse，实际 %r" % (rt.last_doomed_src,)
    print("ok   峰值护栏：垃圾最大血量不抬峰值、62%% 不误判、濒死来源可区分")


async def case_flag_mask() -> None:
    """标志位按掩码比较：bit0 = 濒死（2026-10-17 私服实测「2→3」与「0→1」）。

    旧逻辑按整字节 == 1 比较，导致 bit1=1 的泰坦上濒死值 3 判不出来（A 档位永不输出濒死档），
    而 bit1 从 2 翻成 1 的瞬间整字节恰好 == 1，又会在高血量上误判濒死。
    """
    from source import flag_matches

    assert flag_matches(2, 1, 1) is False, "bit0 未置位 → 不濒死"
    assert flag_matches(3, 1, 1) is True, "bit0 置位 → 濒死（旧逻辑 3 == 1 会漏报）"
    assert flag_matches(0, 1, 1) is False, "另一台泰坦正常值 0"
    assert flag_matches(1, 1, 1) is True, "另一台泰坦濒死值 1"
    assert flag_matches(3, 1, 255) is False, "整字节比较下 3 ≠ 1 —— 这就是濒死档发不出来的原因"
    assert flag_matches(1, 1, 255) is True, "整字节比较下 1 == 1（9-19 那台泰坦恰好成立）"
    assert flag_matches(205, 205, 255) is True, "枚举式标志位仍可用真值 + 255 掩码"
    print("ok   标志位掩码：bit0=濒死，(2→3)/(0→1) 都判对，255 可退回旧行为")


async def case_hit_trigger_thresholds() -> None:
    """受击触发门槛：单次 ≥1500，或 1 秒内累计 ≥2000（两条都填 0 = 旧行为，任何掉血都算）。"""
    def make():
        e = plugin_main.Engine(plugin_main.Config())
        e.cfg.b_hit_min_delta = 1500
        e.cfg.b_burst_delta = 2000
        e.cfg.b_burst_window_ms = 1000
        return e

    def feed(engine, tick, health):
        return engine.feed(plugin_main.Sample(
            tick=tick, read_ok=True, epoch=1, in_titan=True, alive=True,
            health=health, max_health=12500, doomed=False, m25=False),
            limits=plugin_main.Limits())

    # ① 小额单次掉血：不触发
    e = make()
    feed(e, 0, 12500)
    f = feed(e, 16, 12400)                        # 掉 100
    assert f.b.slot == plugin_main.B_NONE, "100 点掉血不得触发受击脉冲"

    # ② 单次 ≥1500：触发
    e = make()
    feed(e, 0, 12500)
    f = feed(e, 16, 11000)                        # 掉 1500（正好达到阈值）
    assert f.b.slot == plugin_main.B_HIT, "单次掉血 1500 应触发"
    assert f.b.extra.get("dt") in (None, 1500), "脉冲应记为一次掉血"

    # ③ 单次都 <1500，但 1 秒内累计 ≥2000：第 8 笔（250×8=2000）触发
    e = make()
    feed(e, 0, 12500)
    health, first_trigger = 12500, None
    for i in range(1, 13):
        health -= 250
        f = feed(e, i * 50, health)               # 每 50ms 掉 250
        if f.b.slot == plugin_main.B_HIT and first_trigger is None:
            first_trigger = i
    assert first_trigger == 8, "累计到 2000 时（第 8 笔）应触发，实际第 %r 笔" % (first_trigger,)

    # ④ 两条都填 0：恢复旧行为（任何掉血都算受击）
    e = plugin_main.Engine(plugin_main.Config())
    e.cfg.b_hit_min_delta = 0
    e.cfg.b_burst_delta = 0
    feed(e, 0, 12500)
    f = feed(e, 16, 12499)                        # 只掉 1 点
    assert f.b.slot == plugin_main.B_HIT, "门槛关闭时应恢复「任何掉血都算受击」"

    print("ok   受击门槛：<1500 不触发、=1500 触发、1 秒累计 2000 触发、门槛 0 退回旧行为")


async def case_strength_limit_fallback() -> None:
    """郊狼上限锁死为 100：设备上报优先，未上报则按 100 换算（用户不可配置）。

    实测过的坑：上限取 200 时所有输出只有配置值的一半（郊狼值 63 → pct 32）。
    上限已从配置项里移除，就是为了让用户改不了它、避免这类麻烦。
    """
    rt = _make_runtime(interval=0, source=FakeSource())
    assert rt.device_info_received is False
    assert rt._pct("a", 63) == 63, "上限未上报时应按固定 100 换算，实际 %r" % (rt._pct("a", 63),)
    assert rt._pct_limit("a") == (100.0, "固定100"), \
        "来源应为固定100，实际 %r" % (rt._pct_limit("a"),)

    # 设备上报了 200 的设备：按 200 换算才对（63 → 31.5% ≈ 32，落在设备量程内）
    rt.device_info_received = True
    rt.max_strength["a"] = 200.0
    assert rt._pct("a", 63) == 32, "设备上限 200 时应按 200 换算，实际 %r" % (rt._pct("a", 63),)
    assert rt._pct_limit("a") == (200.0, "设备上报")

    rt.max_strength["a"] = 100.0
    assert rt._pct("a", 63) == 63, "设备上限 100 时 63 → 63%%，实际 %r" % (rt._pct("a", 63),)
    print("ok   强度换算：未上报按固定 100（63→63%）→ 设备上报优先（200 设备按 200 换算）")


async def case_builtin_locate() -> None:
    """内存定位已内置：配置为空也要能建好必要的链，用户不再填指针链。"""
    from source import MemorySource

    src = MemorySource({}.get)
    assert src.configured, "内置定位应让 health/max_health 视为已配置"
    assert src._chain_modules.get("health") == "engine.dll", src._chain_modules.get("health")
    assert src._chains.get("health") == [0x111E1E68], src._chains.get("health")
    assert src._chains.get("max_health") == [0x111E1E6C]
    assert src._chains.get("doomed") == [0x111E20C4]
    assert src._chains.get("in_titan") == [0x111E1D0C]
    assert src._chains.get("m25") == [0x111E0F88]
    assert not src._chains.get("alive") and not src._chains.get("epoch"), "alive/epoch 靠推导"

    # 配置文件里写了同名键时仍以配置为准 —— 游戏更新后的覆盖通道
    over = MemorySource({"off_health": "engine.dll+1"}.get)
    assert over._chains.get("health") == [0x1], over._chains.get("health")

    # 主程序应认为「在泰坦内」已配置，从而直接采信字段而不是用最大血量推断
    rt = _make_runtime(interval=0, source=FakeSource(), real_apply=True)
    rt._apply_config()          # _make_runtime 不会启动 run()，这里手动应用一次配置
    assert rt.in_titan_configured is True, "内置 in_titan 时应视为已配置"
    print("ok   内置定位：空配置即建好 5 条链（alive/epoch 走推导），配置文件可覆盖")


def _log_lines(rt, needle: str) -> list:
    """跨会话收集含 needle 的日志 (level, message)。"""
    out = []
    for conn in getattr(rt, "ws_log", []):
        for raw in conn.sent:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("op") == "log" and needle in msg.get("message", ""):
                out.append((msg.get("level"), msg.get("message")))
    return out


async def case_selfcheck_warns_once() -> None:
    """读不到数据 → 打一条进程自检警示（含该填什么），8ms 轮询也不刷屏；恢复后报一句恢复。"""
    rt = _make_runtime(
        interval=1.0, real_source=True,
        config={"source_mode": "memory", "mem_module": "ghost.exe",
                "off_health": "engine.dll+111E1E68",
                "off_max_health": "engine.dll+111E1E6C"})
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(1.0)
    warned = _log_lines(rt, "自检：")
    assert len(warned) == 1, "自检提示应只出现一次，实际 %d 条：%s" % (len(warned), warned)
    level, text = warned[0]
    assert level == "warning", level
    assert "ghost.exe" in text, "提示里要点出当前配置的是什么：%s" % text

    # 换成能读到数据的源 → 报一句「读取已恢复」
    rt.source = FakeSource()
    await asyncio.sleep(0.4)
    rt.stop.set()
    await asyncio.wait_for(task, timeout=10.0)
    assert len(_log_lines(rt, "读取已恢复")) == 1, "恢复后应恰好报一次"
    print("ok   进程自检：读不到时打一条警示（点出该改成什么），恢复后报一句")


async def main_async() -> int:
    await case_no_auto_rebuild()
    await case_reassert_cadence()
    await case_disabled()
    await case_manual()
    await case_shutdown_zeroes()
    await case_doomed_derivation()
    await case_doomed_flag_gate()
    await case_peak_garbage_guard()
    await case_flag_mask()
    await case_hit_trigger_thresholds()
    await case_strength_limit_fallback()
    await case_builtin_locate()
    await case_module_cache()
    await case_channel_exclusive()
    await case_channel_switch_zeroes()
    await case_source_rebuild_midrun()
    await case_dry_run_logs_instead_of_sending()
    await case_log_console_encoding()
    await case_relocate_hint()
    await case_sim_run_framing()
    await case_scenario_format()
    await case_sim_refuses_on_channel_overlap()
    await case_sim_defers_scenario_change()
    await case_rehearsal_run_once()
    await case_replay_after_finish()
    await case_selfcheck_warns_once()
    print("\n会话与重发 26/26 用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))