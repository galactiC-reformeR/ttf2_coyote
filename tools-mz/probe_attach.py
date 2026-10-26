# -*- coding: utf-8 -*-
"""在真实 RPG Maker MZ 游戏（Die cry）上演示「进程 / 默认模块名」的附加过程。

跑之前先带远程调试端口启动游戏（仅用于对照 CDP 路线，内存路线不需要）：
    "<游戏目录>\\Die_cry\\Die cry.exe"

本脚本直接复用插件 source.py 的实现，所以它跑出来的结果就是插件会得到的结果。

用法：
    python tools-mz\\probe_attach.py "Die cry.exe"
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from source import (  # noqa: E402
    LOCATE_DEFAULTS, MemorySource, PROCESS_CANDIDATES, parse_chain_full,
)

TARGET = sys.argv[1] if len(sys.argv) > 1 else "Die cry.exe"


def main() -> int:
    print("=" * 78)
    print("1) 进程名解析：同名多进程时插件会挑哪一个")
    print("=" * 78)
    import ctypes
    from source import _k32, _PROCESSENTRY32W, TH32CS_SNAPPROCESS  # noqa: E402

    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    same_name = []
    if _k32.Process32FirstW(snap, ctypes.byref(entry)):
        while True:
            if entry.szExeFile.lower() == TARGET.lower():
                pid = int(entry.th32ProcessID)
                same_name.append(pid)
                print("   同名进程 pid=%-6d 有可见窗口=%s" % (pid, MemorySource._has_window(pid)))
            if not _k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    _k32.CloseHandle(snap)
    chosen = MemorySource._find_pid(TARGET)
    print("   同名进程共 %d 个；_find_pid() 选中 pid=%s" % (len(same_name), chosen))
    if not chosen:
        print("   !! 没找到进程，游戏没在跑？")
        return 1

    print()
    print("=" * 78)
    print("2) 指针链解析：带空格的模块名（本次修复点）")
    print("=" * 78)
    for text in ('"Die cry.exe"+02A1B3C0 -> 18 -> 1C',
                 'Die cry.exe+0x1234',
                 '"Titanfall2.exe"+02A1B3C0 -> 18',
                 'engine.dll+111E1E68'):
        module, offsets = parse_chain_full(text)
        print("   %-42s -> 模块=%-16s 偏移=%s"
              % (text, module, [hex(x) for x in offsets]))

    print()
    print("=" * 78)
    print("3) 模块基址：这个游戏里到底有哪些模块可用")
    print("=" * 78)
    for name in (TARGET, "nw.dll", "node.dll", "engine.dll"):
        base = MemorySource._find_module_base(chosen, name)
        print("   %-16s 基址=%s" % (name, ("0x%X" % base) if base else "未找到"))

    print()
    print("=" * 78)
    print("4) 把泰坦陨落 2 的内置指针链套到这个游戏上：插件会读到什么")
    print("=" * 78)
    cfg = {
        "mem_module": TARGET,
        "off_health": LOCATE_DEFAULTS["health"],
        "off_max_health": LOCATE_DEFAULTS["max_health"],
        "off_doomed": LOCATE_DEFAULTS["doomed"],
        "off_m25": LOCATE_DEFAULTS["m25"],
        "flag_true_value": 1,
        "flag_mask": 255,
    }
    src = MemorySource(cfg.get)
    frame, reason = src.read()
    print("   附加状态：%s" % (src.describe() if hasattr(src, "describe") else src._proc_name))
    print("   读取结果：frame=%s" % frame)
    print("   状态说明：%s" % reason)

    print()
    print("=" * 78)
    print("5) 插件会怎么处理这种垃圾读数")
    print("=" * 78)
    print("   引擎层的守卫（见 engine.py）：负血量 / 超范围 / 最大血量<=0 / 单帧跳变过大")
    print("   都判为垃圾读数并 fail-closed（不输出刺激）。也就是说：")
    print("   进程名改对了 -> 能附加；但泰坦陨落 2 的 engine.dll 偏移在这个游戏里没有意义。")

    src.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
