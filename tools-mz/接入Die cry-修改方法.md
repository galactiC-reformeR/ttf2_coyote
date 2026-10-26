# 针对《Die cry》(RPG Maker MZ) 的修改方法

目标游戏：`<游戏目录>\Die_cry`（本文按本机路径记录，替换成你自己的目录）
对照插件：`ttf2_coyote` v2.19.0（本文的代码改动已应用并部署）
实测时间：2026-10-17 18:11–18:25

## 一、先把这个游戏的技术底子测清楚

| 项目 | 实测结果 | 怎么得到的 |
|---|---|---|
| 引擎 | **RPG Maker MZ**（`js\rmmz_core.js`、`rmmz_managers.js`…） | `js\` 目录文件清单 |
| 运行时 | **nw.js 0.48.4**（Chromium 85.0.4183.121 / V8 8.5.210.26） | `Die cry.exe` 版本信息 + CDP `/json/version` |
| 进程名 | **`Die cry.exe`**（**名字里有空格**） | `Get-Process` |
| 位数 | PE32+ / x64 64 位 | PE 头 `Machine=0x8664` |
| 同名进程 | **8 个** `Die cry.exe`，只有 pid 3368 有可见窗口 | 枚举进程 + `EnumWindows` |
| 模块 | `Die cry.exe` = 0x7FF61BDF0000、`nw.dll` = 0x7FF81A9C0000、`nw_elf.dll` = 0x7FF85A5D0000；**没有 `engine.dll`**，`node.dll` 未加载 | `Module32First/Next` |
| 游戏数据 | `data\*.json`（Map/Actors/…），血量是 JS 里的 `$gameActors.actor(n)._hp` | CDP 求值 |
| 启动参数 | `package.json` 的 `chromium-args` 带 `--disable-devtools` | `package.json` |

关键结论：**这个游戏里没有「引擎 dll + 静态偏移」这种东西**。血量活在 V8 堆里的 JS 对象上，每次启动地址都不同，所以泰坦陨落 2 那套 `engine.dll+111E1E68` 的指针链对它完全无效。

## 二、第一步：进程 / 默认模块名 该怎么填

面板位置：`⚙️ 基础与数据源 → 进程 / 默认模块名`

| 想做的事 | 该填的值 |
|---|---|
| 自动模式（只对泰坦陨落 2 有意义） | `auto` |
| 附加这个 RPG Maker 游戏 | **`Die cry.exe`**（连空格照抄，不带路径、不带引号） |
| 附加新版 Northstar | `NorthstarLauncher.exe` |

这个字段的语义（`source.py` 的 `_ensure_open`）：

- 填 `auto` → 依次尝试 `Titanfall2.exe` → `NorthstarLauncher.exe` → `Titanfall2_trial.exe`；
- 填具体名字 → **先按这个名字找进程**；若这个名字属于泰坦陨落 2 家族，才再补上另外两个同族候选（保留旧行为的互补回退）；填别的游戏的 exe 名就只认这个名字。
- 找到进程后，「默认模块名」= 该进程的主模块（也就是填的那个 exe 本身）；指针链里若自带模块名（`"某某.exe"+偏移`、`engine.dll+偏移`），**以链里写的为准**。

### 光填进程名不够：两个必须一起修的坑

**坑 1：模块名里的空格会被解析器切断。**
旧正则 `([A-Za-z_][A-Za-z0-9_\-]*\.(?:exe|dll))` 不允许空格，`"Die cry.exe"+02A1B3C0` 会被拆成模块名 `cry.exe`、剩余文本 `Die  +…`，链直接废掉。修复后（v2.19.0）：

| 输入 | 修复前 | 修复后 |
|---|---|---|
| `"Die cry.exe"+02A1B3C0 -> 18 -> 1C` | 模块=`cry.exe`，偏移解析错乱 | 模块=`Die cry.exe`，偏移 `[0x2A1B3C0, 0x18, 0x1C]` |
| `Die cry.exe+0x1234`（不带引号） | 同上 | 模块=`Die cry.exe`，偏移 `[0x1234]` |
| `"Titanfall2.exe"+02A1B3C0 -> 18` | 模块=`Titanfall2.exe` | 不变 |
| `engine.dll+111E1E68` | 模块=`engine.dll` | 不变 |

实现：引号形式用 `_QUOTED_MODULE_RE` 整段取；不带引号时 `_MODULE_RE` 允许名字里含空格。

**坑 2：八个同名进程，按名字取第一个会拿错。**
nw.js 会开出 8 个同名子进程（主进程 / 渲染 / GPU / 网络…），只有主进程持有游戏窗口和完整模块表。旧代码返回枚举顺序里的第一个；修复后优先选**有可见顶层窗口**的那个，都没有窗口时退回 pid 最小的（通常是父进程）。实测选中 pid 3368（唯一有窗口的）。

## 三、第二步：只改进程名的实际效果（实测）

改完进程名，插件确实**能附加**，但读不到有意义的数据：

```
附加成功: Die cry.exe (pid 3368)
模块基址: Die cry.exe=0x7FF61BDF0000   nw.dll=0x7FF81A9C0000   nw_elf.dll=0x7FF85A5D0000

"Die cry.exe"+0x1000    -> 0x7FF61BDF1000   读到 int32 = 1447122753   ← PE 头/常量区
"Die cry.exe"+0x400000  -> 0x7FF61C1F0000   读取失败
nw.dll+0x1000           -> 0x7FF81A9C1000   读到 int32 = 786499014
nw.dll+0x8000000        -> 0x7FF8229C0000   读到 int32 = -1
```

套用插件内置的泰坦陨落 2 指针链，会直接在这一步失败（这是好事，说明守卫在工作）：

```
读取结果：frame=None
状态说明：找不到模块 engine.dll —— 指针链里写的模块名必须真的加载在当前进程里
          （当前附加的是 Die cry.exe；链里若写了别的游戏的模块名请改掉）
```

即使跳过这一步、硬编一组「能解析成功」的链，引擎层的守卫也会把这些值判成垃圾读数并 fail-closed（不输出任何刺激）：负血量、超范围、最大血量 ≤ 0、单帧跳变过大都会拦。

**所以：进程名只解决「能不能连上」，解决不了「能不能读到血量」。**

## 四、第三步：这个游戏真正能用的数据源（已实测可行）

血量在 JS 里，就走 JS 的读取通道：**nw.js 的远程调试端口（CDP）**。这条路**不需要改游戏文件**，只要启动参数。

### 4.1 启动游戏时带端口

```pwsh
& '<游戏目录>\Die_cry\Die cry.exe' --remote-debugging-port=9222
```

（也可以改 `package.json` 的 `chromium-args` 追加该参数，但那属于改游戏文件；命令行更干净。注意该游戏原本带 `--disable-devtools`，只影响 F12 面板，不影响调试端口。）

### 4.2 实测可用（本机真实返回）

```
GET http://127.0.0.1:9222/json/list
  → page 目标: id=EBEA12E65073D01B87179BA7EDA45F92  title=rmmz-game

CDP Runtime.evaluate:
  (function(){var a=$gameActors.actor(1);
              return {scene:SceneManager._scene.constructor.name,
                      name:a._name, hp:a._hp, mhp:a.mhp,
                      dead:a.isDead(), allDead:$gameParty.isAllDead()}})()
  → {"scene":"Scene_Title","name":"デスコ","hp":650,"mhp":650,
     "dead":false,"allDead":false}
```

单次求值往返延迟：**平均 0.82 ms**（最小 0.59 / 最大 4.55，采样 20 次）——比插件默认的 8ms 采样间隔还快，完全够用。

### 4.3 插件侧要加的东西

1. `manifest.json` → `source_mode` 增加一个选项 `cdp`，并加一个端口字段：

```json
{ "key": "cdp_port", "type": "number", "label": "nw.js 调试端口", "default": 9222,
  "min": 1024, "max": 65535, "step": 1,
  "description": "游戏需以 --remote-debugging-port=9222 启动；适用于 RPG Maker MV/MZ 这类 nw.js 游戏" }
```

2. `source.py` 增加 `CdpSource`（和现有 `MemorySource` / `SimSource` 同一层，`_make_source` 里按 `source_mode` 选择）：

```python
class CdpSource:
    """通过 nw.js 远程调试端口读 JS 层的游戏状态（RPG Maker MV/MZ）。"""

    #: 返回 [hp, mhp, dead]；取不到角色时返回哨兵值，由调用方判为无效帧
    EXPR = ("(function(){var a=$gameActors.actor(%d);"
            "return a?[a._hp,a.mhp,a.isDead()?1:0]:[-1,0,1]})()")

    def __init__(self, port=9222, actor=1):
        self.port, self.actor = port, actor
        self._ws_url = None

    async def _ensure_ws(self):
        # websockets 由 DGHub 提供，与 main.py 用的是同一个包
        import json, urllib.request, websockets
        if self._ws_url:
            return
        data = json.load(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/list" % self.port, timeout=1.0))
        page = next(t for t in data if t.get("type") == "page")
        self._ws_url = page["webSocketDebuggerUrl"]

    async def read(self) -> dict:
        import json, websockets
        await self._ensure_ws()
        async with websockets.connect(self._ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                "expression": self.EXPR % self.actor, "returnByValue": True}}))
            reply = json.loads(await ws.recv())
        hp, mhp, dead = reply["result"]["result"]["value"]
        if hp < 0 or mhp <= 0:
            return {}                                  # 无效帧，交给引擎 fail-closed
        return {"health": int(hp), "max_health": int(mhp),
                "alive": not dead,
                "doomed": hp <= mhp * 0.2,             # 引擎的濒死判据
                "m25": bool(dead),                     # 死亡界面（引擎按上升沿触发死亡脉冲）
                "in_titan": False}
```

3. 引擎侧不用改：`Sample` 的字段语义照样成立 —— `health/max_health` 驱动 A 通道四档（70/50/30/濒死），`doomed` 就是濒死档，`m25` 的上升沿触发 E 的死亡脉冲（内置 500ms 去抖正好吃掉读帧抖动）。

> 注意 MZ 的 `isDead()` 是**持续状态**而引擎要的是**上升沿**；引擎内部对 `m25` 已经是按上升沿 + 500ms 保持判定，所以直接映射即可，不需要在数据源里自己做边沿检测。

### 4.4 目标角色怎么定

`$gameActors.actor(1)` 是 1 号角色。这个游戏的 `data\System.json` 显示 `hasBattle=false`（没有战斗），主角在队伍里（`$gameParty._actors = [1]`）。要改读哪个人：

```js
$gameParty.leader()            // 队长
$gameParty.members()[0]        // 队首成员
$gameActors.actor(N)           // 指定 N 号
```

## 五、备选：改游戏文件（不依赖调试端口）

如果不想每次都带 `--remote-debugging-port`，可以往游戏里塞一个 MZ 插件，让游戏自己把血量吐出来：

1. 新建 `js\plugins\VitalsBridge.js`（nw.js 里 Node 可用）：

```js
/*:ja
 * @target MZ
 * @plugindesc 把当前 HP 写到临时文件，供外部程序读取
 */
(() => {
  const fs = require('fs');
  const path = require('path');
  const out = path.join(process.env.TEMP, 'diecry_vitals.json');
  const tick = () => {
    try {
      const a = $gameParty.leader();
      fs.writeFileSync(out, JSON.stringify({
        hp: a ? a._hp : -1, mhp: a ? a.mhp : 0,
        dead: a ? a.isDead() : true, t: Date.now()
      }));
    } catch (e) { /* 写文件失败就静默，别影响游戏 */ }
    setTimeout(tick, 50);
  };
  const _Scene_Boot_start = Scene_Boot.prototype.start;
  Scene_Boot.prototype.start = function() { _Scene_Boot_start.call(this); tick(); };
})();
```

2. 在 `js\plugins.js` 的插件列表里加一条启用记录（或在编辑器里装完导出）。
3. 插件侧再加一个 `FileSource`：读 `%TEMP%\diecry_vitals.json`，按 `t` 判新鲜度（超过 500ms 视为失联 → 归零），字段映射同上。

代价与收益：改游戏文件后每个游戏都要单独装；好处是不用管端口、不受 `--disable-devtools` 之类参数影响。

## 六、本次已经落到代码里的改动

| 文件 | 改动 | 目的 |
|---|---|---|
| `source.py` | `_QUOTED_MODULE_RE` + `_MODULE_RE` 允许带空格的模块名，`parse_chain_full` 引号整段取 | 让 `"Die cry.exe"+偏移` 能被正确解析 |
| `source.py` | 新增 `_has_window()`；`_find_pid()` 同名多进程时优先选有窗口的，否则取最小 pid | 八个 `Die cry.exe` 里认准主进程 |
| `source.py` | `_ensure_open()` 的候选列表改为「填什么先认什么」，仅泰坦陨落 2 家族保留互补回退 | 非泰坦陨落 2 的游戏不再被 `NorthstarLauncher.exe` 干扰 |
| `source.py` | 「找不到模块」的提示去掉 Northstar 专属措辞 | 换成通用说法，避免误导 |
| `manifest.json` | `进程 / 默认模块名` 的说明补上「填其它游戏 exe 名也能附加」；版本 → 2.19.0 | 面板上能看懂 |

回归测试（改完 source.py 后重跑）：引擎黄金场景 **17/17**、模拟链路 **17/17**、会话与重发 **24/24** 全通过。

## 七、一句话总结

- **进程名**：填 `Die cry.exe` 就能连上（同名多进程靠「有窗口」认主进程；模块名带空格靠修好的解析器）。
- **模块名**：这个游戏没有 `engine.dll`，链里能写的只有 `Die cry.exe` / `nw.dll` 这些——写上去能解析，但读到的是 PE 头和常量区。
- **要真读到血量**：换成 CDP 数据源（`--remote-debugging-port=9222` + `Runtime.evaluate`，实测 0.82ms 往返，直接拿到 `_hp` / `mhp` / `isDead()`），或者改游戏文件塞一个桥接插件。
