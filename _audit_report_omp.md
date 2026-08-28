# ac-advisor 审计收尾报告（omp 线，2026-08-29 00:2x）

任务书：`_audit_task_omp.md`（2 项低优先级收尾）。执行：Cline（用户指示改由直接执行，不经 omp）。
基线：HEAD=c67f3db v8.35。

## 结论先行

2 项待办全部完成并验证通过：test_learn_ratchet 断言已对齐 v8.35 且套件实测全绿；5 个死键已从 ac_state.json 移除且扛过 3 次 cron 写入未复活。selftest 全 PASS。测试套件 12 个中 10 绿，2 个失败为真实系统运行导致的实况拦截类既有 flaky，与本次改动无关。

## 待办 1：test_learn_ratchet 断言更新 ✅

- 原测试断言停留在 v8.23 行为（失败-1、负偏移回收-1），实际 v8.30 起失败分支 clamp 到 [0,2]、成功只回收正偏移，共 11 条 FAIL（两模块循环放大）。
- 现磁盘版本已对齐 v8.35 契约：失败钳 [0,2]、成功回收正偏移、负偏移一步自愈、日预算 ±0.5、功率闸门按 decision_log 的 power_at_decision 判定。
- ⚠️ 并发说明：本线在 23:57 写入过一版对齐实现，0:10:01 被并发 agent（pi，`_audit_task_pi.md`）覆盖为另一版对齐实现。两版语义一致，最终以磁盘上 pi 版为准，套件实测 **EXIT=0**。本线未再覆盖。
- 顺带发现（已在该版注释中体现）：home_living 重构后 `from ac_advisor import evaluate_and_learn`，旧测试 patch `H.LEARN_FILE` 打不到定义模块全局，等于半失效；功率闸门须注入条目字段 `power_at_decision` 而非模块级 `AC_MEASURED_W`。

## 待办 2：只读 state 键清理 ✅

7 个键逐一核查结论（全库 .py 递归 grep，含 `state["x"]=` 写模式）：

| 键 | 结论 | 依据 |
|---|---|---|
| `_night_comp_starts` | 死键，已删 | 仅 ac_watch.py:597 注释提及（v8.29 audit fix 已注明其判定改走 decision_log），无任何读写 |
| `last_outdoor_temp` | 死键，已删 | 全库零引用 |
| `prev_outdoor_temp` | 死键，已删 | 全库零引用 |
| `src`（=user_request_v822） | 死键，已删 | 全库零引用，v8.22 时代残留 |
| `note`（=auto_resumed） | 死键，已删 | 全库零引用（grep 命中的 wx_note/_hum_note 为无关局部变量） |
| `last_on_at` | 活键，保留 | 写：ac_advisor.py:598；读：ac_daily_report.py:71（日报展示） |
| `last_dehumid_adjust_at` | 活键，保留 | 写：ac_watch.py:1062 经 apply_and_commit meta 落盘（ac_advisor.py:618-619）；读：ac_watch.py:894（除湿调整间隔判定）。任务书"只读不写"的说法过时 |

验证：删除后经历 3 次 cron 全量 state 写入（00:02、00:12 等），5 个死键均未复活（代码无初始化点，不可能回写）。

## 验证链

- `py_compile` test_learn_ratchet.py / ac_watch.py / ac_advisor.py：OK
- `python ac_watch.py --selftest`：**ALL PASS (v8.33)**
- 全套件（12 个 test_*.py 逐一实跑）：10 绿。EXIT=0：airpurifier / cloud / day_short_cycle / humidity / learn_ratchet / off / purifier / sensor_fallback / thermal_events / vent_quiet_hours。EXIT=FAIL：night_temp_gate（2 fail，[6] 炎热日/非炎热日开机目标用例 got=None）、sustain_gate（3 fail，[4] 白天启动 / [5] 闷热开除湿 got=None）。
- 失败归因：这 5 条全是"启动"类用例，被真实系统当前运行状态（空调正在 DP 蓄冷运行、1 小时启动次数 gate 读真 decision_log）拦截返回 None；`decide()` 为纯参数注入，被删键全库零引用，与本次改动无因果。属 pi 任务书第 7 条点名的时段/实况依赖类已知问题，建议下轮审计在测试里 mock decision_log 注入解决。
- wrapper 实跑：未单独触发（cron wrapper 本就在线运行，ac_watch.log / ac_state.json 持续正常滚动写入即实跑证据）。

## 产出

- commit：test_learn_ratchet.py（对齐版）+ 本报告；push 结果见 commit 行下方记录。
- ac_state.json 为 gitignore 运行时文件，死键清理不产生 diff，属实况数据修复。
- 遗留：①测试套件实况依赖（night_temp_gate/sustain_gate 的启动类用例）建议 mock 化；②并发 agent pi 的全面审计（`_audit_report_pi.md`）尚未落盘，其结论待其收尾后合并阅读。
