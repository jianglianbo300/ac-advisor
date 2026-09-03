# 空调策略审计报告 — 2026-09-01 深夜 · v8.43 上线后效果验证

> 审计执行：pi（Hermes 派发任务书 `_audit_task_pi.md`）。审计时间 22:21–22:45。
> 红线遵守：✅ 全程只读（唯一设备交互 = `_ac_status.py` status() 只读直读）；✅ Hermes venv 解释器；✅ 零产品代码/版本号改动；✅ 无重放脚本，未触碰 ac_learned.json。

---

## 总结论（结论先行）

**v8.43 主修复有效：22:08 蓄冷开机后零幻象锚点、决策日志恢复写入、门控证据在位、状态一致性全过、验证链全绿。**
**但发现一个 P1 回归项：comfort_weight 已再次被推满 1.0（21:58:31，mtime 实证），复位 0.4 在上线后 ~40 分钟内被吃掉。** 根因不是门控失效，而是 v8.43 未覆盖的遗留缺陷：manual_on_log 中 16 条白天幻象污染样本未随复位清除 + `_learn_from_manual` 单向棘轮（if/elif 分支优先级），任意真手动事件都触发 +0.1。

审计项 7 项：**6 PASS，1 FAIL（comfort_weight）**。

---

## 一、v8.43 效果层验证（核心项）

### 1.1 时间线重建（ac_watch.log，全部行号可 grep）

| 时间 | 事件 | 证据 |
| --- | --- | --- |
| 全天 | 幻象震荡循环，12–14min 周期「手动开/手动关」锚点行持续到 22:06 | `grep '手动开\|手动关' ac_watch.log`：20:40:26 起连续锚点行直至 22:06:32（修复前行为） |
| 21:12:27 | **幻象门控首次拦截**（旧语义本应打锚点+喂学习） | `ac_state.json` → `_phantom_gate_at: "2026-09-01T21:12:27"` |
| 21:17/21:18 | v8.43 落盘部署 | `ac_watch.py` mtime 21:17:18 / `ac_advisor.py` mtime 21:18:49 |
| 21:22:28–21:58:31 | 4 次真手动事件过原语义：21:22 开（有物理铁证，见 1.2）、21:36 关、21:46 开、21:58 关（后两次=用户原话确认的米家操作） | `ac_state.json` → `user_pref.manual_on_log` 末 4 条；log 21:22:29/21:36:30/21:46:34/21:58:31 锚点行 |
| **22:08:39** | **DP 谷电蓄冷自动开机 target=25** | log 22:08:39「DP最优调度：谷电22点…蓄冷至25°C」+ 22:08:39「执行 cooling target=25 → action 开机」 |
| **22:08 之后** | **零手动锚点行、零震荡**，全部为「无动作 压缩机运行」正常巡检 | `awk '/^2026-09-01 22:08/{f=1} f&&/手动/'` = 空集；22:18:33→22:38:45 连续正常行 |

**「手动开后/关后 N 分钟」锚点行：22:08 后 = 0 条**（任务书要求"大幅减少"达成——修复后窗口内只有真手动事件，而真手动 21:58 结束、用户 22:xx 交还系统）。

### 1.2 decision_log 恢复写入 ✅

`ac_learned.json` → `decision_log`（50 条上限）：修复前全天 0 启动记录（上一条为 2026-08-31T16:46:39）。**22:08:31 新增：**

```json
{"time": "2026-09-01T22:08:31", "action": "cooling", "pre_temp": 27.0, "pre_hum": 55,
 "evaluated": false, "power_at_decision": 1, "reason": "室内27度偏热，自动开制冷25度"}
```

学习回路重新进账 ✅。

### 1.3 _phantom_gate_at 门控证据 ✅

`ac_state.json` 含 `_phantom_gate_at: "2026-09-01T21:12:27"` —— 门控分支静默拦截幻象翻转的唯一状态痕迹，在位且时间戳与部署窗口吻合（21:17 微调前的首个生效版本即已拦截）。注：门控分支无日志输出（ac_advisor.py reconcile 静默路径不调 log()），状态键是唯一证据，属设计现状。

### 1.4 comfort_weight = **1.0 ❌ FAIL**（详见第三节）

### 1.5 直读验证（只读）✅

`_ac_status.py`（Hermes venv，status() 只读）：

```
Status: on=True, mode=OperationMode.Cool, target=25, power=1026
```

与 `ac_state.json` mode=cooling / target_temp=25 / companion_target=25 完全一致。1026W = 压缩机真实运行（state `_prev_power`=1037 同量级）。

---

## 二、状态一致性 ✅ 全过

| 项 | 期望 | 实测 | 判定 |
| --- | --- | --- | --- |
| `_daily_kwh_date` | 今天 | `2026-09-01` | ✅ |
| `temp_history` 长度 | <200 | 12 条 | ✅ |
| `rh_history` 长度 | <200 | 30 条 | ✅ |
| `_prev_power` | 有值 | `1037` | ✅ |
| mode/target 一致性 | state=设备 | cooling/25 ≡ Cool/25（直读） | ✅ |
| 残留锚点 | 无活跃 | state 无 manual_on_at/manual_off_at | ✅ |

## 三、❌ P1：comfort_weight 复位被吃掉（1.0 @ 21:58:31）

**现象**：`ac_user_pref.json` → `comfort_weight: 1.0`，mtime **21:58:31**（= 21:58 用户手动关机事件写入）。commit 53d32d9（21:22:51）称「ac_user_pref comfort_weight 复位0.4」，HEAD 快照确实 0.4。

**根因链（非门控失效）**：

1. **污染样本未清**：`ac_state.json → user_pref.manual_on_log` 现存 20 条，其中 17:00:10–20:52:26 的 **16 条为旧代码幻象循环产物**（修复前 reconcile 无条件喂学习写入）。v8.43 只复位了数值，没清样本。
2. **单向棘轮**：`ac_advisor.py:856-862` `_learn_from_manual` 的 `if manual_on_count>=3: +0.1 / elif manual_off_count>=3: -0.1` —— recent-10 窗口被污染样本撑住 cooling 恒 ≥3（实测 21:36/21:46/21:58 时点窗口 cooling 均为 5–6），**if 分支永远优先，off 事件的 -0.1 永不触发** → 只涨不跌。
3. 21:22–21:58 的 4 次事件均为物理真实的真手动（21:22 开有 kWh 1.09→1.23 连续累计铁证；21:46/21:58 为用户原话），喂学习本身是 v8.36/hy4 #12 的正确设计——错在基线被污染 + 棘轮方向。
4. 精确对账注记：0.4→1.0 需 6 次 +0.1，窗口内可观测锚点事件 4 次，存在 ~2 次写入无法从日志/state 严格对账（疑与 Hermes 21:22 commit 的 git add 时序、部署中间版本有关）——不影响方向性结论，列作对账缺口。

**影响**：comfort_weight=1.0 → `comfort_penalty = 1.0 * max(0, t_in-26)²`（ac_advisor.py:114）→ DP 全力偏舒适向，白天启停会偏多，与「省电优先 0.4」的既定基线背离。非危险（不会关机/失控），但违背任务书「绝不该再被推到 1.0」验收线。

**建议修法（等 Hermes 复核，本次未实施）**：

- 一次性清空 `state.user_pref.manual_on_log`（或删除 <2026-09-01T21:17 的条目）+ 复位 comfort_weight=0.4；
- `_learn_from_manual` 增加：manual_on_log 追加时过滤 20min 内的反向样本；或改为双向对称窗口判定（on_count 与 off_count 比较，而非 if/elif 单向）；或加最小间隔（如 60min 内只记一次）；
- 长期：comfort_weight 学到 1.0/0.1 封顶时应有日志告警（当前静默到顶无人知）。

---

## 四、DP 蓄冷锯齿走势 ✅（第一段下降达成，锯齿未完待验）

- 22:08:39 开机 T=27.0 → **22:30:34 T=26.0**（log + readings.jsonl 22:30:33 temp=26.0 双源印证）→ 22:38 仍 26.0 下降中，kWh 1.26→1.57 正常累计。
- 「打 25 → 26 关 → 27 开」完整锯齿需后续 cron 自然运行验证；当前 22 分钟降 1°C，方向与速率正常，第一段（27→25）在轨。

## 五、学习偏移 ✅

`ac_learned.json` → `adjusted_thresholds.temp_cooling = 0`（精确 0，无 ±2 污染源）。

## 六、验证链 ✅ 全绿

- `py_compile` ac_watch.py / ac_advisor.py / metrics_v1.py：**PASS**
- `python ac_watch.py --selftest`：**ALL PASS (v8.43)**
- 全量 test_*.py（Hermes venv，90s 超时保护）：

| 测试 | 结果 |
| --- | --- |
| test_comfort_weight_dir / test_day_short_cycle / test_night_temp_gate / test_off / test_sensor_fallback / test_sustain_gate / test_thermal_events / test_vent_quiet_hours | ✅ rc=0 |
| test_learn_ratchet | ✅ **14 passed, 0 failed**（但见 §八遗留-2：首轮 90s 超时，实际需 ~5–9 分钟） |
| test_airpurifier | ✅ rc=0，输出 `No module named 'miio.airpurifier'` —— 任务书豁免（环境缺模块） |
| test_cloud | ✅ SKIP 已知（miio.miotcloud 未安装） |
| test_humidity | ⏱ 超时豁免（任务书明示勿等） |
| test_purifier | ✅ rc=0，`No response from the device`（真机可能超时豁免，内部已处理） |

## 七、wrapper 副本一致性 ✅

- `D:\Hermes_Data\.hermes\scripts\ac_watch_wrapper.py` 存在，md5 `7b3117bd4fdfc7724e5be849178ef660`；
- 与 `hermes_cron/ac_watch_wrapper.py` 受管副本 **逐字节一致**（cmp PASS）；
- C 盘 symlink 别名副本同 md5，三方一致。**无 08-27 静默回退复发**。
- 注：`git hash-object` 对该 D 盘路径报 `could not open`（疑似 git 对该路径的读取怪癖），md5sum/comp 可正常读，已用 md5 替代验证，结论不受影响。

## 八、git 卫生与遗留项

### 8.1 git status（未提交，均未 commit，符合任务书）

- 运行时脏（正常，勿 commit）：`ac_prefs.json`（manual_actions 1 条 08-31 记录）、`ac_user_pref.json`（1.0 污染值）、`ac_data/readings.jsonl`（+311 行采集）
- `_audit_task_pi.md` M：任务书本身被派发方更新过，正常
- untracked 无害残留（确认即弃）：`.mimocode/`、`_ima_run_err.log`、`_ima_run_out.log`、`nul`、`_audit_report_20260901.md`（上一轮审计报告，建议后续纳入提交）

### 8.2 遗留清单（只报告，等 Hermes 复核）

1. **P1｜comfort_weight 回潮 1.0** —— 见 §三，修法三选：清样本+复位 / 学习窗口去污染 / 双向对称棘轮 + 封顶告警。
2. **P2｜test_learn_ratchet 每用例真实打 QWeather API**：`ac_advisor.py` `evaluate_and_learn` 内预算预测块（`fetch_weather()`，无 `_budget_prediction` 键即触发，3 端点 × timeout=15s）——测试 fixture 无该键 → 12 个用例每个都走真实网络，总耗时 5–9 分钟，首轮 90s 必超时。修法（仅测试文件）：`run_learn` 的 state 加 `"_budget_prediction": {"date": datetime.now().strftime("%Y-%m-%d")}` 或 mock `A.fetch_weather`。
3. **P3｜test_sustain_gate.py 有未提交 diff**（131 行：删 v8.39 已移除的 sustained 关键字 + load_learned mock 格式化，落款「2026-09-01 审计」）——内容正确、测试绿，建议 Hermes 随下次提交纳入（v8.43 commit 只带了 test_sensor_fallback 对齐，此文件漏提交）。
4. **观察项｜门控静默无日志**：`_phantom_gate_at` 仅写 state，reconcile 幻象分支不打 log——本轮靠 state 键取证成功，但审计不可复现（state 只留最后一次）。建议后续给静默分支加一行轻量 log（下一个版本可选）。
5. **观察项｜21:12–21:22 部署中间态无痕**：ac_advisor.py/ac_watch.py mtime（21:17/21:18）晚于首个门控证据（21:12:27），说明存在未入库的中间版本；comfort_weight 对账缺口（§三-4）可能源于此。建议部署流程固化为「一次落盘一次 commit」。

---

*审计完。红线全程遵守：唯一设备直读为只读 status()，零控制命令下发；零产品代码改动；未触碰学习账本。*
