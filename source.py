# -*- coding: utf-8 -*-
"""数据源：只读内存轮询 + 模拟剧本。

设计约束
--------
* **只读**：仅申请 ``PROCESS_VM_READ | PROCESS_QUERY_INFORMATION``，绝不写游戏内存。
* **不读屏**：不使用任何抓屏 / OCR 手段。
* **失败即未知**：任一必需字段读取失败时返回 ``ok=False``，由引擎走 fail-closed 路径；
  绝不把失败当成「血量 0」（那是最大号的假脉冲来源）。
* 泰坦陨落 2 为 64 位单模块进程，必须在 64 位 Python 进程内解析指针链
  （DGHub 由 ``DGLab-Console.exe --plugin-runner`` 启动插件，满足该条件）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
from typing import Dict, List, Optional, Tuple

#: 十六进制数字（可带 0x），用于指针链解析
_NUM_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]+)$")

#: 指针链里的模块名（CE 复制出来形如 "Titanfall2.exe"+02A1B3C0 或 engine.dll+0x1234）。
#: 模块名可能带空格（RPG Maker MZ / nw.js 打包出来的 exe 就是 ``Die cry.exe`` 这种），
#: 所以**引号形式按原样整段取**，不带引号时也允许名字里出现空格。
_QUOTED_MODULE_RE = re.compile(r'"\s*([^"]+?\.(?:exe|dll))\s*"', re.I)
_MODULE_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_\- .]*?\.(?:exe|dll))', re.I)

#: 游戏进程候选名。新版 Northstar 把引擎 DLL 加载进 NorthstarLauncher.exe 进程，
#: 因此**不存在**独立的 Titanfall2.exe；老版本/纯净启动才会有 Titanfall2.exe。
PROCESS_CANDIDATES: Tuple[str, ...] = ("Titanfall2.exe", "NorthstarLauncher.exe", "Titanfall2_trial.exe")

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: 需要读取的字段 → (配置键, 类型)；类型决定读几个字节与如何解释
FIELD_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("health", "off_health", "int"),
    ("max_health", "off_max_health", "int"),
    ("doomed", "off_doomed", "bool"),
    ("in_titan", "off_in_titan", "bool"),
    ("alive", "off_alive", "bool"),
    ("epoch", "off_epoch", "int"),
    ("m25", "off_m25", "bool"),
)

#: 缺省值：字段未配置时用什么代替（bool 缺失＝该功能不可用，而不是崩溃）
FIELD_DEFAULTS = {"epoch": 1, "in_titan": False, "alive": True, "doomed": False,
                  "m25": False}

#: 没有这些字段就完全无法工作
FIELD_REQUIRED = ("health", "max_health")

#: 内存字段的内置定位（2026-10-17 私服实测有效；对应 engine.dll 9,843,712 字节 / SHA256 58EB1A1B…）。
#: 这些条目**不再出现在配置页**（减少用户误操作），配置文件里若存在同名键仍优先采用，
#: 因此游戏更新后可以只改配置文件或更新这里的默认值，而不必改界面。
LOCATE_DEFAULTS = {
    "health": "engine.dll+111E1E68",
    "max_health": "engine.dll+111E1E6C",
    "in_titan": "engine.dll+111E1D0C",
    "doomed": "engine.dll+111E20C4",
    "m25": "engine.dll+111E0F88",
    "alive": "",          # 留空 = 用"血量 > 0"推导
    "epoch": "",          # 留空 = 用"最大值变化 / 上下机"替代
}


def flag_matches(value: int, true_value: int, mask: int) -> bool:
    """标志位比较：`(值 & 掩码) == (真值 & 掩码)`。

    为什么需要掩码（2026-10-17 实测）：`engine.dll+111E20C4` 是个**位域**而不是纯枚举 ——
    两台泰坦上分别实测到「正常 2 → 濒死 3」和「正常 0 → 濒死 1」，即 **bit0 = 濒死**，
    bit1 是另一个常驻标志。按整字节相等比较（旧行为）会导致：
    * bit1=1 的泰坦上濒死值变成 3，`3 == 1` 为假 → **濒死档永远发不出来**；
    * bit1 从 2 翻成 1 的瞬间整字节恰好等于 1 → **在高血量上误判濒死**。
    掩码默认 1（只取 bit0）；填 255 = 整字节比较（旧行为）。

    @param value: 从内存读到的原始字节值
    @param true_value: 「标志位真值」配置
    @param mask: 掩码配置
    @returns: 该标志位是否成立
    """
    return (int(value) & mask) == (int(true_value) & mask)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE), ("szModule", wt.WCHAR * 256), ("szExePath", wt.WCHAR * 260),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
_k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.restype = wt.BOOL

#: 只用来看「这个 pid 有没有可见顶层窗口」：同名多进程（nw.js / Electron 游戏常见）时，
#: 持有游戏窗口的那个才是该附加的进程。
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.IsWindowVisible.argtypes = [wt.HWND]
_u32.IsWindowVisible.restype = wt.BOOL
_u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_u32.GetWindowThreadProcessId.restype = wt.DWORD
_ENUM_WINDOW_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_u32.EnumWindows.argtypes = [_ENUM_WINDOW_PROC, wt.LPARAM]
_u32.EnumWindows.restype = wt.BOOL


# ---------------------------------------------------------------- 工具

def parse_chain_full(text: str) -> Tuple[Optional[str], List[int]]:
    """解析 Cheat Engine 风格的指针链文本，返回 ``(模块名 or None, 偏移列表)``。

    接受 CE 直接复制出来的形式 ``"Titanfall2.exe"+02A1B3C0 -> 18 -> 1C``、
    ``engine.dll+0x1234 -> 0x18``，也接受 ``0x02A1B3C0, 0x18, 0x1C``。

    模块名会被单独提取出来：这样指针链自己就声明了「相对哪个模块的基址」，
    不必依赖配置里的默认模块（附加 Northstar 时基址模块可能是 NorthstarLauncher.exe
    或 engine.dll，两者都要能写）。

    **所有数字按十六进制解释**（与 CE 的显示一致：CE 复制出的偏移不带 ``0x``，
    形如 ``18`` 表示 0x18 而非十进制 18）。空文本返回 ``(None, [])``。
    """
    if not text:
        return None, []

    module: Optional[str] = None
    cleaned = text.replace("＋", "+").replace("－", "-")
    # 引号形式优先：名字里可能带空格（"Die cry.exe"+0x1234），必须整段取、整段删
    quoted = _QUOTED_MODULE_RE.search(cleaned)
    if quoted:
        module = quoted.group(1)
        cleaned = _QUOTED_MODULE_RE.sub(" ", cleaned)
    else:
        plain = _MODULE_RE.search(cleaned)
        if plain:
            module = plain.group(1)
            cleaned = _MODULE_RE.sub(" ", cleaned)

    for token in ("->", ">", "+", "，", "；", ";", "{", "}", "[", "]", "(", ")"):
        cleaned = cleaned.replace(
            token, "," if token in ("->", ">", "+", "，", "；", ";") else " ")

    out: List[int] = []
    for piece in cleaned.replace("\t", " ").split(","):
        for token in piece.split():
            token = token.strip()
            if not token:
                continue
            found = _NUM_RE.match(token)
            if not found:
                raise ValueError("无法解析的指针链片段：%r" % token)
            out.append(int(found.group(1), 16))
    return module, out


def parse_chain(text: str) -> List[int]:
    """只取偏移列表（模块名被丢弃），供不需要区分模块基址的调用方使用。"""
    return parse_chain_full(text)[1]


# ---------------------------------------------------------------- 内存源

class MemorySource:
    """只读内存数据源。"""

    name = "memory"

    def __init__(self, get_config) -> None:
        self._cfg = get_config
        self._pid: Optional[int] = None
        self._handle = None
        self._proc_name = ""
        self._chains: Dict[str, List[int]] = {}
        self._chain_modules: Dict[str, Optional[str]] = {}
        self._kinds: Dict[str, str] = {}
        self._module_bases: Dict[str, Optional[int]] = {}
        self._last_error = ""
        self._reopen_next = False
        self._reload_chains()

    # ---- 生命周期
    def _reload_chains(self) -> None:
        self._chains.clear()
        self._chain_modules.clear()
        self._kinds.clear()
        for field, key, kind in FIELD_SPECS:
            self._kinds[field] = kind
            raw = str(self._cfg(key, "") or "").strip() or LOCATE_DEFAULTS.get(field, "")
            try:
                module, offsets = parse_chain_full(raw)
                self._chains[field] = offsets
                self._chain_modules[field] = module
            except ValueError as exc:
                self._chains[field] = []
                self._chain_modules[field] = None
                self._last_error = "%s 指针链解析失败：%s" % (field, exc)
        override = str(self._cfg("off_int32_fields", "") or "")
        for pair in override.replace("，", ",").split(","):
            if "=" in pair:
                name, kind = pair.split("=", 1)
                name, kind = name.strip(), kind.strip().lower()
                if name in self._kinds and kind in ("int", "bool", "float", "u8", "u16", "u32"):
                    self._kinds[name] = kind
        # 布尔字段的"真值"：标定发现这些标志位其实是**状态枚举**（例如泰坦状态 1=濒死、
        # 4/5=已摧毁），所以不能简单按"非 0 即真"读，必须与期望值比较。
        try:
            self.flag_true = int(float(str(self._cfg("flag_true_value", 1) or 1)))
        except (TypeError, ValueError):
            self.flag_true = 1
        # 掩码：实测「濒死标志」这个字节是**位域**而不是纯枚举 —— 2026-10-17 抓取证明
        # bit0=濒死，另一个常驻标志占 bit1（两台泰坦分别是 2→3 和 0→1）。
        # 所以比较改成 (值 & 掩码) == (真值 & 掩码)，默认只取 bit0。
        try:
            self.flag_mask = int(float(str(self._cfg("flag_mask", 1) or 0)))
        except (TypeError, ValueError):
            self.flag_mask = 1
        if self.flag_mask <= 0 or self.flag_mask > 255:
            self.flag_mask = 1      # 掩码 0 会让"任何值都算真"，按默认 bit0 处理

    @property
    def configured(self) -> bool:
        return all(self._chains.get(f) for f in FIELD_REQUIRED)

    def close(self) -> None:
        if self._handle:
            try:
                _k32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._handle = None
        self._pid = None
        self._module_bases.clear()

    def identity(self) -> str:
        """当前定位到的进程与指针链所用模块的基址（诊断用，便于一眼看出链错在哪）。"""
        if not self._pid:
            return "未连接进程"
        parts = ["%s (PID %d)" % (self._proc_name, self._pid)]
        for module in sorted({m for m in self._chain_modules.values() if m}):
            base = self._module_base(module)
            parts.append("%s=0x%X" % (module, base) if base else "%s=未找到" % module)
        return " / ".join(parts)

    def _ensure_open(self) -> bool:
        if self._handle and self._pid and self._alive_pid(self._pid):
            return True
        self.close()

        wanted = str(self._cfg("mem_module", "auto") or "auto").strip()
        if wanted.lower() in ("", "auto"):
            candidates = list(PROCESS_CANDIDATES)
        else:
            candidates = [wanted]
            # 泰坦陨落 2 家族内部互补：新版 Northstar 把引擎装进 NorthstarLauncher.exe，
            # 填 Titanfall2.exe 时要能回退到它；填其它游戏的 exe 名则只认这一个。
            if wanted.lower() in {name.lower() for name in PROCESS_CANDIDATES}:
                candidates += [n for n in PROCESS_CANDIDATES if n.lower() != wanted.lower()]

        pid, name = None, ""
        for candidate in candidates:
            pid = self._find_pid(candidate)
            if pid:
                name = candidate
                break
        if not pid:
            self._last_error = "未找到游戏进程（已尝试：%s）" % " / ".join(candidates)
            return False

        handle = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            self._last_error = "OpenProcess 失败（err=%d），请以管理员身份运行 DGHub" \
                               % ctypes.get_last_error()
            return False

        self._pid, self._handle, self._proc_name = pid, handle, name
        self._module_bases.clear()
        self._last_error = ""
        return True

    def _module_base(self, module: str) -> Optional[int]:
        """按模块名取基址（带缓存）。模块名为空时用当前进程主模块。

        指针链里写的模块名必须真的加载在当前进程里：附加 NorthstarLauncher 时
        游戏模块是 NorthstarLauncher.exe / engine.dll / client.dll，
        **不存在** Titanfall2.exe。

        **只缓存成功结果**：Northstar 启动器是"先起进程、几秒后才载入 engine.dll"，
        如果此时把"找不到模块"也缓存下来，因为 PID 一直是活的、`_ensure_open` 会一直提前
        返回成功，这个负面缓存就再也不会刷新 —— 实测会导致切到官服（-vanilla 启动）后
        插件永久失效。失败时保持不缓存，下次读取自动重试。
        """
        name = module or self._proc_name
        key = name.lower()
        cached = self._module_bases.get(key)
        if cached:
            return cached
        base = self._find_module_base(self._pid, name) if self._pid else None
        if base:
            self._module_bases[key] = base
        return base

    @staticmethod
    def _alive_pid(pid: int) -> bool:
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return False
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if not _k32.Process32FirstW(snap, ctypes.byref(entry)):
                return False
            while True:
                if entry.th32ProcessID == pid:
                    return True
                if not _k32.Process32NextW(snap, ctypes.byref(entry)):
                    return False
        finally:
            _k32.CloseHandle(snap)

    @staticmethod
    def _has_window(pid: int) -> bool:
        """pid 是否拥有可见的顶层窗口。

        nw.js / Electron 打包的游戏（RPG Maker MZ 就是）会开出 8 个**同名**子进程，
        只有主进程持有游戏窗口与完整模块表；按名字取到第一个往往会拿到渲染/GPU 子进程。
        """
        found: List[int] = []

        def _cb(hwnd, _lparam):
            if not _u32.IsWindowVisible(hwnd):
                return True
            owner = wt.DWORD(0)
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                found.append(hwnd)
                return False
            return True

        _u32.EnumWindows(_ENUM_WINDOW_PROC(_cb), 0)
        return bool(found)

    @staticmethod
    def _find_pid(name: str) -> Optional[int]:
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return None
        matches: List[int] = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if not _k32.Process32FirstW(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.szExeFile.lower() == name.lower():
                    matches.append(int(entry.th32ProcessID))
                if not _k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            _k32.CloseHandle(snap)
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        # 同名多进程：优先有游戏窗口的那个；都没有窗口时退回 pid 最小的（通常是父进程）
        for pid in matches:
            if MemorySource._has_window(pid):
                return pid
        return min(matches)

    @staticmethod
    def _find_module_base(pid: int, name: str) -> Optional[int]:
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snap == INVALID_HANDLE_VALUE:
            return None
        try:
            entry = _MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(_MODULEENTRY32W)
            if not _k32.Module32FirstW(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.szModule.lower() == name.lower():
                    return int(ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0)
                if not _k32.Module32NextW(snap, ctypes.byref(entry)):
                    return None
        finally:
            _k32.CloseHandle(snap)

    # ---- 读取
    def _read_bytes(self, address: int, size: int) -> Optional[bytes]:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = _k32.ReadProcessMemory(self._handle, ctypes.c_void_p(address), buf, size,
                                    ctypes.byref(got))
        if not ok or got.value != size:
            return None
        return buf.raw

    def _read_value(self, address: int, kind: str):
        size = {"bool": 1, "u8": 1, "u16": 2}.get(kind, 4)
        raw = self._read_bytes(address, size)
        if raw is None:
            return None
        if kind in ("bool", "u8"):
            return bool(raw[0])
        if kind == "u16":
            return int.from_bytes(raw, "little", signed=False)
        if kind == "float":
            return ctypes.c_float.from_buffer_copy(raw).value
        if kind == "u32":
            return int.from_bytes(raw, "little", signed=False)
        return int.from_bytes(raw, "little", signed=True)

    def _resolve(self, chain: List[int], module: str) -> Optional[int]:
        """CE 指针链：第一项加在模块基址上，中间项逐级解引用，最后一项只加偏移。"""
        base = self._module_base(module)
        if not chain or base is None:
            return None
        addr = base + chain[0]
        # CE 语义：链上每个箭头偏移都跟在一次解引用之后，最后一个偏移加完即为字段地址，
        # 即「解引用次数 = len(chain) - 1」。早先版本写成了 len(chain) - 2（少解引用一次），
        # 会让 2 项以上的指针链全部解析到错误地址。
        for offset in chain[1:]:
            raw = self._read_bytes(addr, 8)
            if raw is None:
                return None
            ptr = int.from_bytes(raw, "little", signed=False)
            if ptr < 0x10000:
                return None                      # 空指针/垃圾指针
            addr = ptr + offset
        return addr

    def read(self) -> Tuple[Optional[dict], str]:
        """返回 (采样字典, 状态说明)。字典为 None 表示本次读取失败。"""
        if not self.configured:
            return None, "内存字段未配置（缺少 %s）" % "/".join(
                f for f in FIELD_REQUIRED if not self._chains.get(f))
        if self._reopen_next:
            # 上一轮有字段读失败：句柄/模块基址可能已随游戏重启失效，强制重开一次
            self._reopen_next = False
            self.close()
        if not self._ensure_open():
            return None, self._last_error

        frame: Dict[str, object] = {}
        missing_optional: List[str] = []
        for field, _key, _kind in FIELD_SPECS:
            chain = self._chains.get(field) or []
            if not chain:
                if field in FIELD_REQUIRED:
                    return None, "字段 %s 未配置" % field
                missing_optional.append(field)
                continue
            module = self._chain_modules.get(field) or self._proc_name
            if self._module_base(module) is None:
                return None, ("找不到模块 %s —— 指针链里写的模块名必须真的加载在当前进程里"
                              "（当前附加的是 %s；链里若写了别的游戏的模块名请改掉）"
                              % (module, self._proc_name))
            addr = self._resolve(chain, module)
            if addr is None:
                return None, "字段 %s 指针链解析失败" % field
            value = self._read_value(addr, self._kinds.get(field, "int"))
            if value is None:
                self._reopen_next = True            # 下次读取前强制重开进程
                return None, "字段 %s 读取失败" % field
            if self._kinds.get(field) == "bool":
                # 位域式标志位：按掩码比较，而不是整字节相等
                value = flag_matches(int(value), self.flag_true, self.flag_mask)
            frame[field] = value

        for field in missing_optional:
            frame[field] = FIELD_DEFAULTS[field]
        note = "" if not missing_optional else "（未配置：%s）" % ",".join(missing_optional)
        return frame, note


# ---------------------------------------------------------------- 模拟源

#: 剧本（测试向量）里的键名是驼峰，而引擎与内存源统一用 `Sample` 的下划线字段名。
#: 漏掉这一步映射的后果（实测）：模拟模式下 `max_health` / `in_titan` 永远取默认值 0 / False，
#: 每一帧都被判无效，A 档位与 B 受击全部发不出来，日志只剩死亡脉冲与满屏 invalid。
SIM_KEY_MAP = {"readOk": "read_ok", "inTitan": "in_titan", "maxHealth": "max_health"}


class SimSource:
    """按黄金测试向量的剧本来回放，用于无游戏时验证全链路。"""

    name = "sim"

    def __init__(self, get_config, vector_path: str) -> None:
        self._cfg = get_config
        self._path = vector_path
        self._steps: List[Tuple[int, dict]] = []
        self._state: dict = {}
        self._t0: Optional[float] = None
        self._loop_at = 0
        self.scenario_id = ""
        #: 只播一次（演练用）：播完由 Runtime 收尾，不自动重播
        self.once = False
        #: 剧本里夹的提示行（如「插入电池，泰坦生命值恢复」）：按 tick 播一次
        self._notes: List[Tuple[int, str]] = []
        self._notes_fired: set = set()
        self._last_pos: Optional[int] = None
        self._load()

    def _load(self) -> None:
        with open(self._path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        wanted = str(self._cfg("sim_scenario", "S1") or "S1").upper()
        scenario = None
        for item in data["scenarios"]:
            if item["id"].upper().startswith(wanted):
                scenario = item
                break
        if scenario is None:
            scenario = data["scenarios"][0]
        self.scenario_id = scenario["id"]
        self.once = bool(scenario.get("once"))
        base = dict(data["defaults"]["sample"])
        steps: List[Tuple[int, dict]] = []
        for step in scenario["steps"]:
            base.update(step.get("sample") or {})
            steps.append((int(step["tick"]), dict(base)))
        self._steps = steps
        self._loop_at = max((tick for tick, _ in steps), default=0)
        self._state = dict(steps[0][1]) if steps else {}
        self._notes = [(int(step["tick"]), str(step["note_log"]))
                       for step in scenario["steps"] if step.get("note_log")]

    def close(self) -> None:
        return None

    def read(self, elapsed_ms: float) -> Tuple[Optional[dict], str]:
        if not self._steps:
            return None, "模拟剧本为空"
        cycle = max(1000, self._loop_at + 1500)
        pos = int(elapsed_ms) % cycle
        if self._last_pos is not None and pos < self._last_pos:
            self._notes_fired.clear()          # 回绕 = 新一轮，提示行可以再播
        self._last_pos = pos
        chosen = self._steps[0][1]
        for tick, sample in self._steps:
            if pos >= tick:
                chosen = sample
        frame = {SIM_KEY_MAP.get(key, key): value for key, value in chosen.items()}
        for tick, text in self._notes:
            if pos >= tick and tick not in self._notes_fired:
                self._notes_fired.add(tick)
                frame["_sim_log"] = text       # 每帧最多夹一条，其余下一帧补
                break
        # 剧本自带完整的身份字段（在泰坦内 / 存活 / 濒死），是权威值：
        # 内存模式那套「未标定时用血量推导」的兜底不能套到剧本上（会把剧情改掉）。
        frame["_trusted"] = True
        frame["_sim_pos"] = pos
        return frame, "模拟剧本 %s @%dms" % (self.scenario_id, pos)


def make_vectors_path(plugin_root: str) -> str:
    """定位随插件分发的测试向量文件。"""
    return os.path.join(plugin_root, "testvectors", "scenarios.json")
