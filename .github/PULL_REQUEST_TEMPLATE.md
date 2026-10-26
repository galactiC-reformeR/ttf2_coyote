## 改了什么

<!-- 一句话说清。对照项目历史：「放弃 trigger 的租约，改 set_strength 精确控制脉冲起止」 -->

## 为什么

<!-- 哪次真实故障、哪次体感偏差，或哪份文档与实测不符。没有场景支撑的改动请先开 issue 讨论 -->

## 怎么验证的

- [ ] `python tests\run_vectors.py`（17 个黄金场景）
- [ ] `python tests\test_sim_pipeline.py`（17 个模拟链路场景）
- [ ] `python tests\test_session.py`（26 个会话用例）
- [ ] `python tests\test_selfcheck.py`（13 个进程自检用例）
- [ ] 动了标定工具：`python tools\calibrate.py --selftest`

如果改了行为，请说明新增/修改了哪个场景或用例；如果没法离线验证（只能实机确认），写清实测条件
（游戏版本、是否私服、设备型号）。

## 文档同步

- [ ] `manifest.json` 的字段/默认值变了 → README「通道分工」与 `docs/`（介绍站）一起改
- [ ] 版本号变了 → `docs/index.html` 的导航与页脚、`docs/README.md` 的事实来源表
- [ ] 内存定位或验证记录变了 → README「内置定位的验证记录」补日期与读数样本
- [ ] `CHANGELOG.md` 补一条

## 不要提交

`__pycache__`、`tools/calibration*.json`、日志、本机绝对路径（`C:\Users\...` 这类只应出现在文档示例里）。
