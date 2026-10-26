# -*- coding: utf-8 -*-
"""进程自检（selfcheck.diagnose）的用例：每种「进程不匹配」都要给出对得上的提示。

不需要真游戏：进程探测结果由 ProcInfo 列表注入，纯逻辑比对。
    python tests\\test_selfcheck.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selfcheck import ProcInfo, diagnose, _describe  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


TS = 0x7FFF92E50000     # 引擎基址（实测值，见 IGO 日志）

# 实测两种宿主：Northstar 起时引擎在 NorthstarLauncher.exe；官服直启在 Titanfall2.exe
NORTHSTAR_HOST = ProcInfo("NorthstarLauncher.exe", 192, True, TS)
VANILLA_HOST = ProcInfo("Titanfall2.exe", 30668, True, TS)


@case("一个候选进程都没有")
def _():
    level, text = diagnose("auto", [])
    assert level == "warning", level
    assert "没找到游戏进程" in text
    assert "改名" in text and "别用 auto" in text


@case("进程在但引擎没加载（有窗口 = 卡在 EA 授权）")
def _():
    procs = [ProcInfo("Titanfall2.exe", 22388, True, None)]
    level, text = diagnose("auto", procs)
    assert level == "warning"
    assert "引擎不在" in text and "ActivationUI" in text
    assert "engine.dll 未加载" in text        # 探测明细要带上


@case("进程在但引擎没加载（无窗口 = 专用服务器）")
def _():
    procs = [ProcInfo("NorthstarLauncher.exe", 999, False, None)]
    level, text = diagnose("auto", procs)
    assert level == "warning"
    assert "专用服务器" in text and "r2ds.bat" in text


@case("auto + 引擎宿主在候选里：判为权限/偏移问题，不冤枉进程名")
def _():
    level, text = diagnose("auto", [VANILLA_HOST])
    assert level == "error"
    assert "auto 候选里就有它" in text
    assert "管理员" in text and "偏移" in text


@case("auto + 引擎宿主不在候选里（整合包改名）：必须手填")
def _():
    host = ProcInfo("TF2_CN.exe", 500, True, TS)
    level, text = diagnose("auto", [host])
    assert level == "warning"
    assert "不在 auto 候选" in text and "TF2_CN.exe" in text


@case("手填的名字有进程，但引擎在另一个进程里（附错进程）")
def _():
    procs = [ProcInfo("Titanfall2.exe", 22388, True, None), NORTHSTAR_HOST]
    level, text = diagnose("Titanfall2.exe", procs)
    assert level == "warning"
    assert "进程名填错了" in text
    assert "改成 NorthstarLauncher.exe" in text
    assert "引擎不在它里面" in text


@case("手填的名字完全没有进程，引擎在 auto 候选里的那个")
def _():
    procs = [VANILLA_HOST]
    level, text = diagnose("NorthstarLauncher.exe", procs)
    assert level == "warning"
    assert "进程名不匹配" in text
    assert "改成 Titanfall2.exe" in text
    assert "在 auto 候选里" in text


@case("手填的名字没有进程，引擎在候选之外的进程：提示必须手填")
def _():
    procs = [ProcInfo("Weird_Wrapper.exe", 77, True, TS)]
    level, text = diagnose("MyGame.exe", procs)
    assert level == "warning"
    assert "改成 Weird_Wrapper.exe" in text
    assert "必须手填" in text


@case("填对了宿主名却仍读不到：判为权限/偏移")
def _():
    level, text = diagnose("Titanfall2.exe", [VANILLA_HOST])
    assert level == "error"
    assert "就是引擎宿主" in text and "管理员" in text


@case("填的名字是全角/带空白：要指出「只差一个字符，照抄这个」")
def _():
    level, text = diagnose("Ｔｉｔａｎｆａｌｌ２.exe", [VANILLA_HOST])
    assert level == "warning"
    assert "只差全角/空格/大小写" in text, text
    assert "Titanfall2.exe" in text


@case("引擎宿主没窗口（r2ds 专用服务器）：要点破服务端没有客户端血量")
def _():
    dedi = ProcInfo("NorthstarLauncher.exe", 404, False, TS)
    level, text = diagnose("auto", [dedi])
    assert level == "error"
    assert "专用服务器" in text and "开客户端" in text


@case("多个进程都加载了引擎：提示多开只认一个")
def _():
    twin = ProcInfo("NorthstarLauncher.exe", 300, False, 0x7FFF00000000)
    level, text = diagnose("Titanfall2.exe", [VANILLA_HOST, twin])
    assert "多开" in text
    assert "只认有窗口的那一个" in text


@case("探测明细的格式")
def _():
    s = _describe([ProcInfo("A.exe", 1, True, 0x1000), ProcInfo("B.exe", 2, False, None)])
    assert s == "A.exe(pid 1,有窗口,engine.dll=0x1000) ｜ B.exe(pid 2,无窗口,engine.dll 未加载)", s
    assert _describe([]) == "无"


def main() -> int:
    passed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            print("FAIL  %s\n      %s" % (name, exc))
        else:
            passed += 1
            print("ok    %s" % name)
    print("\n自检用例 %d/%d 通过" % (passed, len(CASES)))
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
