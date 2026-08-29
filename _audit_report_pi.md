# ac-advisor 全面审计报告（pi 线，2026-08-29）

任务书：`_audit_task_pi.md`（v8.35 后复审）。基线：本审计开工时 HEAD=c67f3db；审计中途 omp 并行线推进到 42e5c2d（见 §10）。全程遵守红线：只控上海空调伴侣 DID 2056557176（本地 miio 0.5.12）、未触本溪/爸爸家设备、未改 cron/gateway、执行位 wrapper 与受管副本哈希一致（399e399b…）。

## 结论先行

**系统当前健康度：良好。** 状态账本一致、学习偏移干净（0）、v8.35 三项声称修复经效果层重放全部兑现、12 条停止出口前置完整、无严重级新发现。遗留 1 条非阻断微短循环新路径（建议级）+ 测试隔离已在本轮修完。

## 发现清单

### [建议-1] 三个测试文件启动类用例被真实 decision_log 拦成假红（已修）

- **证据**：`test_day_short_cycle.py` 3F / `test_sustain_gate.py` 3F / `test_night_temp_gate.py` 2F（2026-08-29 00:4x 实跑）。decide() 的启停上限读 `A.load_learned().decision_log` 近 1h 真启动（ac_watch.py:602-615），夜间 DP 蓄冷实跑在 00:00/00:12 留下 2 条真实 cooling 记录 → `_starts_1h>=2 且 temp<29` 拦下所有 27°C 启动用例（实测单点复现：同参数 mock 空账本即绿）。
- **归因**：环境依赖假红，非产品 bug。与 omp 线 42e5c2d 修的 selftest 缺陷（其 `_audit_report_omp.md` audit9）同源；本次按其遗留建议把隔离下沉到三个测试文件。

### [建议-2] 夜间微短循环存在 v8.35 未覆盖的新路径

- **证据**：ac_watch.log 2026-08-29 `00:00:13 执行 cooling target=25 → action 设定25°C`（运行中缓除上调 24→25）→ `00:02:07 执行 off → action 关机`。重放（`A.load_learned` 空账本 + 谷电 mock）命中 `夜间室内湿度已达标（AH=10.6），关机省电`——缓除上调 target 后，T=25 ≤ 新target(25)+0.5 使夜间 AH 停机的温度前置立即满足 → 2 分钟微循环。
- **与 v8.35 修复的关系**：v8.35 堵的是 DP 缓存 override 复用路径（重放 8/8 验证已堵死，见 §3b）；本条是 decide() 内部「缓除上调→立即达标」交互，属另一条出口，未在本轮红线要求内，**未修产品**，建议下轮：缓除上调后 N 分钟内禁停（或上调当轮跳过温度前置复核）。

### [建议-3] 关机日志无 reason 字符串（既有，v8.33 已识别未落地）

- **证据**：ac_watch.log 全部关机行 `执行 off target=None → action 关机 · …` 无 decide() 理由；apply_and_commit 成功路径 reason 空串（ac_advisor.py:568）。本轮仍需靠决策重放反推分支（§5）。长期改进：reason 穿透 log_decision。

### [观察] 状态/账本/日志健康面

| 项 | 结果 | 证据 |
| --- | --- | --- |
| `_daily_kwh_date`=今天 | ✓ | ac_state.json = 2026-08-28（审计时点） |
| 峰谷账本交叉 | ✓ 差 0.0353 < 0.05 | peak 6.4863 + valley 2.3635 = 8.8498 vs `_daily_kwh` 8.8145 |
| temp/rh_history ≤200 | ✓ 各 12 条 | |
| manual_on_at 残留 | ✓ 合法锚点 | 20:36:54 用户手动开机（reconcile_state 记录），TTL 内；manual_off_at 与之配对 |
| 学习偏移 | ✓ temp_cooling=0 | ac_learned.json，decision_log 50 条 |
| 48h 段配对 | 41 开 / 48 关 | 8 条 8-10min ZERO_KWH 段（08-27，v8.32 假运行闸门前历史数据）；2 条 ≤3min 短循环（08-27 02:48、08-28 04:50，均在 v8.35 提交 19:38 之前=修复前行为）；1 条 154min 长跑（08-27 20:26→23:00，晚间巡航豁免 WATCH_MAX_RUN，by design）；6 条 OFF_WITHOUT_ON（用户手动开机，ac_watch 只记自身动作，正常） |

### [观察] 死代码复核（清单 4）

独立 grep（`state["_x"]=` 写模式）结论与 omp 线 42e5c2d 一致：`_night_comp_starts`/`last_outdoor_temp`/`prev_outdoor_temp`/`src`/`note` 为真死键，已由 42e5c2d 从 ac_state.json 移除；`last_on_at`、`last_dehumid_adjust_at` 有完整读写路径（活键保留）。本轮 ac_state.json 已无死键残留。

### [观察] 数值卫生（清单 5）

decide() 全部 6 条 target 产出路径硬夹取在位：夜间 `max(24, min(26, T-2))`、白天 `max(24/25, min(28, T-drop))`、假运行重启 `max(16, cur-2)`、缓除 `min(26, cur+1)`、DP 蓄冷 `int(min(26, max(24, ...)))`（v8.29 fix）、缓除重启 `max(16, cur-2)`。历史 55°C 案发点（DP）已双重防护。注意：`ac_apply`（ac_advisor.py:542）无二次夹取，最后防线完全依赖 decide 侧——当前所有路径已夹取，可接受；加固建议（低优先）：ac_apply 入口统一 clamp [16,30]。

## v8.35 效果级验证（清单 6，重放脚本 `_audit_replay_v835.py` 8/8 PASS）

| 项 | 方法 | 结果 |
| --- | --- | --- |
| 6a selftest 峰电 mock | 读用例 + 实跑 | ✓ v8.23 持续判据组与启停上限组均 `mock current_price=ELECTRIC_VALLEY`（ac_watch.py:1213-1224, 1204-1210）；峰电反向用例 27 blocked/30 放行（1226-1231）；selftest ALL PASS |
| 6b DP 蓄冷缓存失效 | main() 全 mock 重放（不触真机不写日志） | ✓ temp≤蓄冷目标 → override 失效不开机（24/23°C 两例）；temp>目标 → 覆盖仍生效开 24°C（对照）；无缓存对照不开机。凌晨 04:50 抖振路径已堵死（今夜 00:12 起蓄冷运行 40min+ 无复发） |
| 6c manual_on_at 过期清除移出 mode 前置 | 重放 | ✓ off 态 + 过期锚点 → 被清；未过期锚点保留且不拦 decide；manual_off 冷却期拦截与过期清除双向正确（ac_watch.py:744-797） |
| 6d DAY_TEMP_REACHED_SLACK | 值 + git 历史 | ✓ 当前 0.5（ac_watch.py:119）；历史：v8.21 引入 0.5 → af5be25(v8.24) 放宽 1.0（bak-20260826-valley 实证）→ 906f6c3(v8.32) 收回 0.5 → 至今无再回退 |

## 测试套件（清单 7）

| 文件 | 结果 |
| --- | --- |
| test_day_short_cycle / test_sustain_gate / test_night_temp_gate | 修复后 **38/23/18 全绿 0 fail** |
| test_off / test_thermal_events / test_vent_quiet_hours / test_sensor_fallback | 全绿（14-30 用例） |
| test_learn_ratchet | ✓ 全过（omp 线 42e5c2d 重写对齐 v8.35，EXIT=0；原豁免项已复活为纯函数测试） |
| test_humidity | 豁免（依赖实时读数）未跑 |
| test_cloud | 预期 SKIP（环境缺 miio.miotcloud，0.5.12 --no-deps 装法所致，非产品 bug） |
| test_airpurifier / test_purifier | 环境依赖：缺 miio.airpurifier 模块 / 净化器设备响应超时 |

py_compile ac_watch.py + ac_advisor.py ✓。

## 停止出口枚举（清单 8）

`return ("off"` 共 12 处逐一核对（ac_watch.py:472-576）：假运行止损（≥FALSE_RUN_ABORT_MIN=10min 前置✓）、假运行上限、fan_only 湿度达标、升温缓除恢复、fan_only 接近目标、WATCH_MAX_RUN=90（`not evening` 豁免=晚间巡航设计行为）、夜间 AH 停机（温度前置✓）、夜间湿度完成（温度前置+夜最小压缩时长✓）、绝对下限逃生门×2、白天含水量双轴停（温度前置+DUAL_STOP_MIN_COMP✓）、湿度达标完成关（v8.33 温度前置✓）、热模型提前关（温度前置✓）、除湿停滞关（无温度前置但语义=无效空耗止损，正确）。**未发现 v8.32→v8.33 式「修一半」复发。**

## 日志证据链重放（清单 9）

3 个近期关机点 decide() 重放全部命中合法前置分支：

1. `2026-08-29 00:02:07`（T=25 AH=10.6 target=25 夜）→ `夜间室内湿度已达标（AH=10.6）`（同事件构成建议-2 的微循环，分支本身合法）
2. `2026-08-28 19:30:52`（T=26 AH=12.4 target=26）→ `含水量已达标，关机防过冷`
3. `2026-08-28 18:32:48`（T=26 AH=13.2 target=26）→ `含水量已达标，关机防过冷`

## 并行会话说明（§10）

审计中途 omp 线在同一仓库推进：eedf844（test_learn_ratchet 重写）→ c4d096a + 42e5c2d（selftest 隔离修复 + 死键清理）。其修复与本审计清单 4/6a 结论互证，无代码冲突；本轮工作树改动仅限三个测试文件的隔离补丁 + 本报告 + 重放脚本。

## 修复清单

| 改动 | 验证 | commit |
| --- | --- | --- |
| test_day_short_cycle.py / test_sustain_gate.py / test_night_temp_gate.py 各补 `A.load_learned` 空账本隔离（与既有 current_price 隔离并列，[5] 上限用例的局部注入/还原模式兼容） | 三文件 38/23/18 全绿；py_compile ✓ | 见 commit hash（本次提交） |
| `_audit_replay_v835.py` 新增（v8.35 效果层重放证据，8/8 PASS） | 实跑输出留档 | 同上 |

push：按备份惯例执行，被墙则记录失败原因下轮重试。

## 遗留（未修项及原因）

1. **夜间微短循环新路径**（建议-2）：修产品需改 decide() 缓除/停机交互，红线 4 要求分支实跑验证+决策代码改动，超出本轮测试隔离修复范畴；已给出根因与修法，建议下轮专项。
2. **reason 穿透日志**（建议-3）：v8.33 起挂账，改动面涉及 apply_and_commit 签名，留待统一做。
3. **untracked 清理**（任务书 0 明示本轮不强清）：建议下轮 .gitignore 补 `*.log`、`*.bak-*`、`_*.py`、`gh_*.json`、`review_*.py`、`tokenfaucet_models.json`、`ndx_T_signal_v2.zip`、`qr_session.json`（会话凭据，当前未被 git 追踪✓）；`ac_collect.py`/`ac_advice.py`（活跃 cron 脚本）与 `ctrlc_sentinel.py`/`spawn_monitor.py` 建议入库。`miio_config.json` 未被追踪 ✓（token 不外泄）。
4. **test_purifier / test_airpurifier**：设备超时 / 缺 miio 子模块，环境项。
5. **pi-lens 既有告警**：sys.stdout.reconfigure 类型存根误报（ac_watchdog/home_living）、测试文件故意传 current_target=None 与风格项（F541/UP031）——均既有非本轮引入，已标 false-positive 处置。
