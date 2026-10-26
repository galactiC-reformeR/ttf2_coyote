# -*- coding: utf-8 -*-
"""诊断「进程 / 默认模块名」该填什么。

做法：枚举所有名字里带 titanfall / northstar 的进程，对每个进程看
  * 有没有可见窗口（同名多进程时插件按这个认主进程）
  * 有没有加载 engine.dll（真正的引擎宿主才有）
然后给出该填的值。

用法（游戏正在运行时跑，插件目录下执行）：
    python tools\\probe_process.py
"""
from __future__ import annotations

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from source import (  # noqa: E402
    _k32, _PROCESSENTRY32W, TH32CS_SNAPPROCESS, MemorySource,
)

#: 引擎 dll：指针链里的偏移全部相对它，所以「哪个进程加载了它」= 该附加哪个进程
ENGINE_MODULES = ("engine.dll", "client.dll", "server.dll")


def list_candidates():
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    out = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _k32.Process32FirstW(snap, ctypes.byref(entry)):
            return {}
        while True:
            name = entry.szExeFile
            low = name.lower()
            if "titanfall" in low or "northstar" in low:
                out.setdefault(name, []).append(int(entry.th32ProcessID))
            if not _k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        _k32.CloseHandle(snap)
    return out


def _name_note(name: str) -> str:
    """进程名的「抄写提示」：这类问题多半错在不可见的细节上。"""
    notes = ["%d 字符" % len(name)]
    if any(ord(ch) > 127 for ch in name):
        notes.append("含非 ASCII（中文/全角），必须原样照抄")
    if " " in name:
        notes.append("名字中间有空格，前后别多打")
    if name != name.strip():
        notes.append("⚠ 名字里有首尾空白")
    return "，".join(notes)


def main() -> int:
    found = list_candidates()
    print("=" * 84)
    print("枚举：名字里含 titanfall / northstar 的进程")
    print("=" * 84)
    if not found:
        print("  一个都没有 —— 游戏没在跑。启动游戏后再跑本脚本。")
        return 0

    rows = []
    for name, pids in found.items():
        for pid in pids:
            base = MemorySource._find_module_base(pid, "engine.dll")
            mods = {m: MemorySource._find_module_base(pid, m) for m in ENGINE_MODULES}
            has_engine = any(v for v in mods.values())
            rows.append(dict(name=name, pid=pid, window=MemorySource._has_window(pid),
                             engine=base, mods=mods, has_engine=has_engine))

    for r in rows:
        print("  %-26s pid=%-6d 窗口=%-5s engine.dll=%s"
              % (r["name"], r["pid"], r["window"],
                 ("0x%X" % r["engine"]) if r["engine"] else "未加载"))
        print("  %-26s %s" % ("", _name_note(r["name"])))
        detail = ", ".join("%s=%s" % (m, ("0x%X" % b) if b else "-")
                           for m, b in r["mods"].items())
        print("  %-26s %s" % ("", detail))

    print()
    print("=" * 84)
    print("结论 / 建议填法")
    print("=" * 84)
    hosts = [r for r in rows if r["has_engine"]]
    if hosts:
        with_window = [r for r in hosts if r["window"]]
        pick = (with_window or hosts)[0]
        print("  引擎宿主进程：%s（pid %d）" % (pick["name"], pick["pid"]))
        print("  → 「进程 / 默认模块名」填：%s" % pick["name"])
        print("    （原样 %d 个字符，不带路径、不带引号；改完即时生效、不用重启插件）"
              % len(pick["name"]))
        if len(hosts) > 1:
            print("  ⚠ 有 %d 个进程都加载了引擎（多开？）。插件按「有窗口优先」只认一个，"
                  "想指定另一个需要关掉多余的那份。" % len(hosts))
        if pick["name"].lower() in {n.lower() for n in
                                   ("Titanfall2.exe", "NorthstarLauncher.exe",
                                    "Titanfall2_trial.exe")}:
            print("  → 这个名字在 auto 候选里，直接填 auto 也能连上；")
            print("    把名字写死的好处是：同时开了别的候选进程时不会被抢走。")
        else:
            print("  ⚠ 这个名字不在 auto 候选里，必须手填，auto 永远连不上。")
    else:
        print("  没有进程加载 engine.dll：")
        print("   * 若只有 NorthstarLauncher.exe 且无窗口 → 可能是专用服务器（-dedicated），"
              "读不到客户端血量；")
        print("   * 若进程刚启动 → 引擎 dll 是稍后才载入的，等几秒重跑本脚本；")
        print("   * 否则游戏还没进到引擎初始化阶段。")

    print()
    print("提示：引擎 dll 位于 bin\\x64_retail\\，链里的 engine.dll+偏移 就是相对它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
