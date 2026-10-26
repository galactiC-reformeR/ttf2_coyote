"""读出给定地址当前的值（只读），用于确认哪个地址真的持有血量。

支持 ``16EF49FA110``（绝对地址，十六进制）与 ``engine.dll+111E1E68``（模块+偏移）两种写法。

用法::

    python tools/check_addrs.py 16E989F9A04 engine.dll+111E1E68 ...
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import source  # noqa: E402

CFG = {"mem_module": "auto"}


def cfg(key, default=None):
    return CFG.get(key, default)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ms = source.MemorySource(cfg)
    if not ms._ensure_open():
        print("打开进程失败：", ms._last_error)
        return 1

    print("进程 %s (PID %d)  主模块基址 0x%X"
          % (ms._proc_name, ms._pid, ms._module_base(ms._proc_name) or 0))
    print("%-22s %-20s %-10s %-10s %s" % ("输入", "绝对地址", "int32", "uint32", "float"))

    results = []
    for item in sys.argv[1:]:
        text = item.strip()
        module = None
        if "+" in text and not text.lower().startswith("0x"):
            module, _, off_text = text.partition("+")
            module = module.strip().strip('"')
            offset = int(off_text.strip(), 16)
        else:
            offset = int(text, 16)
        base = ms._module_base(module) if module else 0
        if module and base is None:
            print("%-22s %s" % (text, "模块未加载：%s" % module))
            continue
        address = (base or 0) + offset
        raw = ms._read_bytes(address, 4)
        if raw is None:
            print("%-22s 0x%-18X %s" % (text, address, "读取失败"))
            continue
        as_int = int.from_bytes(raw, "little", signed=True)
        as_uint = int.from_bytes(raw, "little", signed=False)
        as_float = __import__("struct").unpack("<f", raw)[0]
        print("%-22s 0x%-18X %-10d %-10d %.4f" % (text, address, as_int, as_uint, as_float))
        results.append((text, address, as_int))

    if results:
        from collections import Counter
        common = Counter(value for _t, _a, value in results).most_common(3)
        print("\n出现最多的值：", ", ".join("%d（%d 个地址）" % (v, c) for v, c in common))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
