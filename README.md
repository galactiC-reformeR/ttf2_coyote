# Titanfall 2 x dghub插件

DGHub 外部插件（SDK v1）：Titanfall2多人模式中泰坦血量的**数值变化**映射为郊狼电刺激。


介绍页（GitHub Pages）：<https://galactic-reformer.github.io/ttf2_coyote/>　｜　
安装包：[Releases](https://github.com/galactiC-reformeR/ttf2_coyote/releases)　｜　
本仓库目录结构见文末「[仓库结构](#仓库结构)」。

## 通道分工

| 条目 | 默认通道 | 形态 | 默认强度（郊狼值） |
|---|---|---|---|
| **A** 血量档位 | A | 电平，档位变了才改 | 70%→20 / 50%→30 / 30%→40 / 濒死→60 |
| **B** 单次受击 | B | 单次脉冲（可开关） | 25 |
| **E** 死亡脉冲 | B | 死亡界面出现后持续（默认 5 秒，`☠️ [E]` 段可调 0.5–15 秒）；波形默认**留空 = 恒定**，也可自选 | 45 |

处决动画期间**不静音**：保持濒死档持续输出（`A_DOOM`），受击脉冲照常；死亡脉冲（E）不受影响。

### 受击脉冲的触发门槛（v2.13.0 起）

不是每次掉血都发脉冲 —— 满足**任一条**才触发：

| 规则 | 配置项 | 默认 |
|---|---|---|
| 单次掉血 ≥ N | `⚡ [B] → 单次受击触发阈值` | **1500** |
| 最近窗口内累计掉血 ≥ M | `⚡ [B] → 1 秒内累计伤害阈值` + `累计窗口 (ms)` | **2000 / 1000ms** |


## 通道分配：一条通道同时只有一种输出

上面三个条目按**输出形态**分成两组，各占一条物理通道：

- **持续输出**（条目 A，电平式）→ 配置 `🎯 [A] → 输出通道（持续）`，默认 **A 通道**
- **脉冲输出**（条目 B 受击、条目 E 死亡脉冲）→ 配置 `⚡ [B] → 输出通道（受击）`、`☠️ [E] → 输出通道（死亡脉冲）`，默认 **B 通道**

**规则**：两组不能压在同一条通道上 —— 它们都用 `set_strength` 操作同一条强度层，压在一起会互相覆盖（档位强度和受击脉冲互相打断）。所以：

- 受击/死亡脉冲**不得**占用持续输出那条通道；`E` 可以和 `B` 共用一条（两者不会同时发生）；
- 如果你把脉冲选到了持续通道上，插件按**持续输出优先**处理：脉冲自动改用另一条通道，并把界面上的值**改回实际值**（同时打一条 warning 日志说明原因）；
- 界面下拉只有「A 通道 / B 通道」两项：旧版的「两个通道（both）」与互斥规则冲突，已移除；
- **改通道即时生效**：插件先把两条通道归零，再按新映射重新下发，不会把上一次的档位强度留在被释放的通道上。

> DGHub 的 `config_schema` 不支持字段联动（没法在界面上直接禁用冲突选项），所以互斥由插件自己强制 + 写回界面值。

## 日志格式

变更日志按 `(slot, cfg, eff, clamp, reason)` 变化时输出一行，`cfg` 与 `eff` 并列：

- **cfg** = 你在条目里配的强度；**eff** = 实际下发的强度；两者不等时 `clamp` 会说明被谁削顶；
- `reason` 解释每条 0 输出（`anim-mute` / `switch-off` / `death-window` / `invalid` …）；
- 排查「体感与配置不符」时，第一眼看 `cfg` 与 `eff` 是否相等。

行首还带三个诊断字段，专门用来查「为什么这个血量会判濒死」：

```
[12.3] hp=7800/12500 doomed=0 peak=12500 titan=1 alive=1 | ch=a slot=A_70 cfg=20 eff=20 clamp=- reason=tier-enter
[13.1] hp=2400/12500 doomed=1(collapse) peak=12500 titan=1 alive=1 | ch=a slot=A_DOOM cfg=60 ...
[14.0] hp=2500/2500  doomed=1(flag)     peak=2500  titan=1 alive=1 | ch=a slot=A_DOOM cfg=60 ...
```

- `doomed=1(flag)` = 濒死由**标志位**判定；`doomed=1(collapse)` = 由**最大值塌缩**判定；`flag+collapse` = 两者都成立
- `peak` = 本次驾驶记录到的**最大血量峰值**。若它明显大于机体真实最大血量（如 >17500），说明被垃圾读数污染过 ——
  插件现在会用「最大血量合理上限」（默认 20000）挡住这种读数，并打一条 `最大血量读数异常…` 的 warning


## 仓库结构

```
ttf2_coyote/            ← 仓库根 = 插件本体（放进 D:\dghub\plugins\ 时目录名必须是这个）
├─ main.py              DGHub 插件入口：会话、配置、通道下发、日志与状态上报
├─ engine.py            状态机：血量档位 / 受击脉冲 / 死亡脉冲 / fail-closed
├─ source.py            数据源：内存只读轮询（内置定位 + 进程解析）与模拟数据源
├─ selfcheck.py         读不到数据时的进程自检（六种情况分别给建议）
├─ manifest.json        DGHub 清单与参数面板（config_schema）
├─ config/              默认值、内置内存偏移
├─ spec/                引擎行为契约
├─ tests/               离线自测脚本
├─ testvectors/         17 个剧本（黄金场景 + 演练剧本）
├─ tools/               只读探针与标定工具（calibrate.py / check_addrs.py / probe_process.py）
├─ tools-mz/            接入 nw.js（RPG Maker MV/MZ）游戏的实测记录与 CDP 探针
├─ nscalib-mod/         可选：游戏内标定模组（Northstar 服务端脚本）
├─ docs/                介绍站（GitHub Pages 发布根，维护说明见 docs/README.md）
├─ .github/             issue 模板、PR 模板
└─ *.md                 通道分配、标定清单，以及下面这几份项目文档
```

| 文档 | 内容 |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | 如何改判定、如何重新标定、自测要求、文档数字回源 |
| [CHANGELOG.md](CHANGELOG.md) | 每个版本对应修掉的那次真实故障 |
| [SUPPORT.md](SUPPORT.md) | 出问题先自查的三步，以及报问题要带的三样东西 |
| [SECURITY.md](SECURITY.md) | 强度越界 / 急停失效 / 误触发怎么私密报告 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 隐私与强度安全相关的几条约定 |

## 声明

- 强度只负责下发与归零；实际体感请从低档试起，并记住急停手段（急停热键、退出归零、失联 3 秒归零）。
- 面向成年用户自用，请遵守所在地法律法规与设备厂商说明。

## 许可

MIT，见 [LICENSE](LICENSE)。
