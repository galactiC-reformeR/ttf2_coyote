# -*- coding: utf-8 -*-
"""进程自检：附加失败时，把「该把 进程 / 默认模块名 改成什么」写进日志。

什么时候跑
----------
只在**读取失败**时跑（外加插件刚起来那一次），且两次之间至少隔 ``SELFCHECK_INTERVAL_S`` 秒；
枚举进程 + 查模块表都是毫秒级，绝不参与 8ms 采样热路径。

为什么需要
----------
泰坦陨落 2 的引擎宿主**会换人**（实测：Northstar 启动时 ``engine.dll`` 在
``NorthstarLauncher.exe`` 里；官服直启时在 ``Titanfall2.exe`` 里），而且进程存在 ≠ 引擎在里面
（直启被 EA 授权挡住时进程在、引擎没加载）。用户看到的现象都只是「读不到数据」，
光看状态行分不清是没启动、名字填错、附错进程、还是权限问题 —— 这里替他把区别讲清楚。
"""
from __future__ import annotations

import ctypes
import unicodedata
from typing import List, NamedTuple, Optional, Tuple

from source import (
    PROCESS_CANDIDATES, TH32CS_SNAPPROCESS, MemorySource, _k32, _PROCESSENTRY32W,
)

#: 两次自检之间至少间隔多少秒（同样的结论不重复刷屏）
SELFCHECK_INTERVAL_S = 30.0

#: 判定「引擎在这个进程里」用的模块名 —— 指针链里的偏移全部相对它
ENGINE_MODULE = "engine.dll"

#: 进程名里出现这些词就当作候选：这样 exe 被改名成「Titanfall2 - 副本.exe」也能认出来
_NAME_HINTS = ("titanfall", "northstar")


class ProcInfo(NamedTuple):
    """一个候选进程的探测结果。"""

    name: str
    pid: int
    window: bool
    engine: Optional[int] = None      # engine.dll 基址；None = 没加载 → 引擎不在这个进程里


def _iter_candidates() -> List[Tuple[str, int]]:
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    out: List[Tuple[str, int]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _k32.Process32FirstW(snap, ctypes.byref(entry)):
            return []
        while True:
            name = entry.szExeFile
            low = name.lower()
            if any(hint in low for hint in _NAME_HINTS):
                out.append((name, int(entry.th32ProcessID)))
            if not _k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        _k32.CloseHandle(snap)
    return out


def scan() -> List[ProcInfo]:
    """枚举所有名字像泰坦陨落 2 / Northstar 的进程，并查它们有没有加载 engine.dll。"""
    procs: List[ProcInfo] = []
    for name, pid in _iter_candidates():
        procs.append(ProcInfo(
            name=name,
            pid=pid,
            window=MemorySource._has_window(pid),
            engine=MemorySource._find_module_base(pid, ENGINE_MODULE),
        ))
    return procs


def _describe(procs: List[ProcInfo]) -> str:
    """把探测结果压成一行，方便贴进日志。"""
    if not procs:
        return "无"
    parts = []
    for p in procs:
        parts.append("%s(pid %d,%s,%s)" % (
            p.name, p.pid,
            "有窗口" if p.window else "无窗口",
            ("engine.dll=0x%X" % p.engine) if p.engine else "engine.dll 未加载",
        ))
    return " ｜ ".join(parts)


def _pick_host(hosts: List[ProcInfo]) -> Optional[ProcInfo]:
    """多个进程都加载了引擎时（多开），选插件实际会附加的那个：有窗口的优先。"""
    if not hosts:
        return None
    for p in hosts:
        if p.window:
            return p
    return hosts[0]


def _is_candidate_name(name: str) -> bool:
    return name.lower() in {n.lower() for n in PROCESS_CANDIDATES}


def _normalize(name: str) -> str:
    """把全角/半角、首尾空白、大小写归一，用来认出「看起来一模一样其实差一个字符」的名字。"""
    return unicodedata.normalize("NFKC", name).strip().casefold()


def near_note(wanted: str, procs: List[ProcInfo]) -> str:
    """找出「归一化之后相同」的实测进程名。

    典型的坑：用户从别处复制来全角字母（`Ｔｉｔａｎｆａｌｌ２.exe`）、或名字里带了看不见的
    首尾空白 —— 屏幕上看着一模一样，字符串却不相等。这里替他把两个名字摆出来。
    """
    key = _normalize(wanted)
    for p in procs:
        if p.name.lower() == wanted.lower():
            continue
        if _normalize(p.name) == key:
            return "（你填的名字与实测的 %s 只差全角/空格/大小写，照抄它即可） " % p.name
    return ""


def diagnose(configured: str, procs: Optional[List[ProcInfo]] = None
             ) -> Optional[Tuple[str, str]]:
    """给一次附加失败做归因。

    :param configured: 面板上「进程 / 默认模块名」的当前值（``auto`` 或具体 exe 名）
    :param procs: 探测结果；默认自己 ``scan()``（测试时可注入）
    :returns: ``(日志级别, 日志正文)``；``None`` 表示一切正常、不必说话
    """
    configured = (configured or "auto").strip()
    auto = configured.lower() in ("", "auto")
    procs = scan() if procs is None else procs

    hosts = [p for p in procs if p.engine]
    host = _pick_host(hosts)
    detail = "实测进程：%s" % _describe(procs)
    tail = "（自检 %.0f 秒内最多提醒一次）" % SELFCHECK_INTERVAL_S

    def multi_note() -> str:
        if len(hosts) <= 1:
            return ""
        return "；另有 %d 个进程也加载了引擎（多开？），插件只认有窗口的那一个" % (len(hosts) - 1)

    # ---- 情况 1：一个候选进程都没有
    if not procs:
        return ("warning",
                "自检：没找到游戏进程 —— 游戏没启动，或 exe 被改名了（当前配置：%s；探测范围："
                "进程名含 titanfall / northstar）。若你用的是改名副本、整合包或包装器启动，请把"
                "「⚙️ 基础与数据源 → 进程 / 默认模块名」填成真实进程名，别用 auto。%s"
                % (configured, tail))

    # ---- 情况 2：有进程，但没有一个加载了引擎
    if host is None:
        if any(p.window for p in procs):
            return ("warning",
                    "自检：进程在、引擎不在 —— 都没有加载 %s。实测直启 Titanfall2.exe 会先跑 "
                    "Core\\ActivationUI.exe 的 EA 授权，这期间进程存在但引擎没载入，要等授权完成才进"
                    "游戏；若一直停在这里，先看有没有 EA 登录窗口没点。%s %s"
                    % (ENGINE_MODULE, detail, tail))
        return ("warning",
                "自检：进程在、引擎不在，而且这些进程都没有窗口 —— %s 都没加载 %s。"
                "若你开的是专用服务器（r2ds.bat / NorthstarLauncher.exe -dedicated），服务端没有客户端"
                "血量，任何「进程 / 默认模块名」都读不到，请开客户端。%s"
                % (detail, ENGINE_MODULE, tail))

    # ---- 情况 3：引擎宿主找到了，看配置指得对不对
    engine_host = ("%s（pid %d）" % (host.name, host.pid))
    # 专用服务器（r2ds.bat → NorthstarLauncher.exe -dedicated）同样会加载 engine.dll，
    # 唯一的廉价区别是它**没有窗口** —— 服务端没有客户端血量，得先点破这层，别让人去查偏移。
    server_note = ("" if host.window else
                   "；注意这个进程没有窗口，可能是 r2ds.bat 起的专用服务器 —— "
                   "服务端没有客户端血量，请开客户端")
    if auto:
        if _is_candidate_name(host.name):
            return ("error",
                    "自检：引擎宿主是 %s，auto 候选里就有它，说明进程名没问题 —— 读不到是别的原因："
                    "① DGHub 权限不足（请以管理员身份运行）；② 引擎刚载入、模块基址还没出现；"
                    "③ 游戏更新过，指针链偏移失效（需要重新标定）。%s%s%s %s"
                    % (engine_host, multi_note(), server_note, detail, tail))
        return ("warning",
                "自检：引擎宿主是 %s，但它不在 auto 候选（%s）里，auto 永远附加不上 —— "
                "请把「进程 / 默认模块名」填成 %s。%s%s %s"
                % (engine_host, " / ".join(PROCESS_CANDIDATES), host.name,
                   server_note, detail, tail))

    wanted = configured
    same_name = [p for p in procs if p.name.lower() == wanted.lower()]
    if host.name.lower() == wanted.lower():
        return ("error",
                "自检：你填的 %s 就是引擎宿主（pid %d），进程名没问题 —— 读不到是别的原因："
                "① 权限不足（以管理员运行 DGHub）；② 偏移失效（游戏更新后需重新标定）。%s%s%s %s"
                % (wanted, host.pid, multi_note(), server_note, detail, tail))
    if same_name:
        return ("warning",
                "自检：进程名填错了 —— 你填的 %s 确实有进程（pid %s），但引擎不在它里面；"
                "引擎在 %s。请把「进程 / 默认模块名」改成 %s。%s %s%s"
                % (wanted, "/".join(str(p.pid) for p in same_name), engine_host, host.name,
                   detail, multi_note(), tail))
    return ("warning",
            "自检：进程名不匹配 —— 你填的 %s 没有对应进程；引擎在 %s。请改成 %s%s。%s%s %s%s"
            % (wanted, engine_host, host.name,
               "（这个名字在 auto 候选里，直接填 auto 也可以）"
               if _is_candidate_name(host.name) else
               "（这个名字不在 auto 候选里，必须手填）",
               near_note(wanted, procs), detail, multi_note(), tail))
