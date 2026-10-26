# 关于共同开发

欢迎各位大佬对本插件进行更新与改进，在此感激不尽。
为方便大家更好的改进请阅读以下改动指南。

#接什么样的改动

| 类型 | 例子 | 额外要求 |
|---|---|---|
| 修行为 | 档位判错、脉冲该发没发、归零不及时 | 补/改 `testvectors/scenarios.json` 或 `tests/` 用例 |
| 换内存定位 | 游戏更新后偏移失效、换引擎版本 | 走下面的「标定流程」，并在 README 里更新验证记录与日期 |
| 修进程 / 附加问题 | 改名的副本、包装器启动、多开抢进程 | 补 `tests/test_selfcheck.py` 的对应措辞断言 |
| 文档与文案 | README、`tools/*.md`、介绍站 `docs/` | 数字必须回源（见文末） |
| 面板字段 | `manifest.json` 的 `config_schema` | 同步 README「通道分工」与介绍站的参数面板区 |

不接受：读屏方案、写游戏内存、注入、自动瞄准一类会影响对局的改动 —— 这个插件的定位就是只读轮询。

## 环境

自测只需要 Python（本项目在 3.12 上验证，3.9+ 应可用）与仓库本身，**不需要游戏、不需要设备**：

```pwsh
python tests\run_vectors.py        # 17 个黄金场景：直接构造 Sample 喂引擎
python tests\test_sim_pipeline.py  # 17 个场景走 剧本 → SimSource → 引擎，逐帧比对
python tests\test_session.py       # 26 个会话用例：重发、通道、演练模式、日志
python tests\test_selfcheck.py     # 13 个进程自检用例
```

四个都通过再提交。涉及标定工具时加一条：

```pwsh
python tools\calibrate.py --selftest
```

## 改判定逻辑

引擎是状态机（`engine.py`：`feed(sample, limits, estop) -> frame`），会话层只负责把读数喂进去。
改判定的正确做法：

1. 先在 `testvectors/scenarios.json` 里加一个场景，用**期望输出**描述你想要的行为；
2. 跑 `tests\run_vectors.py`，看它红；
3. 改 `engine.py`，让它绿；
4. 如果这个场景也会经过会话层（重发、通道分配、演练模式），在 `tests\test_session.py` 里补一个用例。

不要为了让测试过而改期望值 —— 期望值是这个项目的行为契约，改动它要在提交信息里说明理由。

## 标定流程（换引擎版本 / 换游戏）

内存定位是**推导 + 实测验证**出来的，不是猜的：

1. `python tools\probe_process.py` 确认附加到了哪个进程、主模块是什么；
2. `python tools\calibrate.py --minutes 4`，按 README「第二步」的顺序在游戏控制台敲标定命令（需要 `nscalib-mod` 模组或私服）；
3. 工具输出 `tools\calibration.json`，里有 `off_health` / `off_max_health` / `off_doomed` / `off_m25` / `flag_mask`；
4. 用 `python tools\check_addrs.py engine.dll+<偏移>` 现场核对读数，与游戏内 `ns_dmg_status` 对齐；
5. 把结论写进 README「内置定位的验证记录」，带上实测日期与读数样本 —— 下一个人靠这条记录判断偏移还能不能用。

`tools\calibration.json`、`calibration-trace.json` 已在 `.gitignore` 里，不要提交。

## 提交与 PR

- 提交信息用中文，一句话写清「改了什么 + 为什么」（对照这个项目的历史：「放弃 trigger 的租约，改 set_strength 精确控制脉冲起止」）；
- 一个 PR 只做一件事；
- 不要提交：`__pycache__`、标定产物、日志、本机绝对路径（`C:\Users\...`、`D:\dghub\...` 这类只能出现在文档的示例里）；
- 新增的运行期文件路径要么写进 `.gitignore`，要么在 README「仓库结构」里说明。

PR 描述里请写：动机（哪次真实故障/体感偏差）→ 改法 → 跑了哪几个自测 → 有没有动到文档数字。

## 文档数字必须回源

介绍站 `docs/index.html` 上每个数字都登记在 `docs/README.md` 的「文案的事实来源」表里。
改版本号、档位强度、门槛、脉冲时长时，三处要一起改：`manifest.json` → `README.md` → `docs/`。
