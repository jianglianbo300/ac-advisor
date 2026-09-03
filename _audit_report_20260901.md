# 空调策略审计报告（2026-09-01 晚，pi 线执行）

> 任务书：`_audit_task_pi.md`（Hermes 派发）。解释器：Hermes venv。红线全遵守（未触碰本溪空调、未下发任何控制命令、未改产品代码、未动 cron/wrapper）。
> 结论版本：v8.42（fbe7f67），本轮修复 commit **cea9068**（仅测试文件，已 push 成功）。

---

## 一、疑点1 结论：今天 18:42 off 为何不在 decision_log

### 结论（一句话）

**不是漏账 bug，是设计内行为 + 一个更大的系统性问题**：今天全天 decide() 被手动锚点保护短路（545/591 条日志），18:42 的 off 走的是 `reconcile_state` 设备状态对账路径——该路径**本来就只落 `manual_on_log`，不进 decision_log**。

### 证据链（代码行号）

1. `log_decision` 唯一调用点 = `ac_watch.py:1174`（decide() 决策动作路径），写 `ac_learned.json` decision_log（`ac_advisor.py:343-352`）。
2. `reconcile_state`（`ac_advisor.py:629-656`）：
   - `AC_SOCKET=="off"` 且 mode∈running → 置 `manual_off_at` + `last_off_at` + `_learn_from_manual`（**无 log_decision**）；
   - `AC_SOCKET=="on"` 且 mode∉running → 置 `manual_on_at` + `run_start` + `_learn_from_manual`（**无 log_decision**）。
3. 实测证据：
   - `manual_on_log`（ac_state.json 内）有 `{ts:"2026-09-01T18:42:17", rh:62, mode:"off"}` 条目 = 18:42 事件走了 reconcile 路径的直接落账；
   - `ac_watch.log` 18:42:18 `手动关后0分钟，跳过自动启动（尊重用户意图）`（锚点保护分支，`ac_watch.py:849-884`）；
   - 今天 `已自动` 动作日志 = **0 条**，decision_log 最后一条仍是 08-31 16:46（50 条满额滚动，无新写入因为**没有任何 decide() 动作发生**）。

### 🔴 审计新发现：手动锚点震荡循环（比疑点1 本身严重）

- 今天全天模式：设备 socket is_on 以 ~12min 周期翻转（00:00–19:20 间歇持续），每次翻转触发 reconcile_state 翻 state.mode 并打手动锚点 → 锚点保护又短路 decide()（"手动开后N分钟暂不自动关"/"手动关后N分钟跳过自动启动"）→ **decide() 全天饥饿，自学习零进账**。
- 统计：今天 545 条手动锚点行、`手动开后0分钟`=41 次、`手动关后0分钟`=40 次（即 ≥81 次设备翻转）；昨天 258 行。`已自动` 动作今天/昨天均 0 条。
- **误判根因（实锤）**：19:24 只读采样伴侣状态（DID 2056557176，只读）：`is_on=True` 但 **`load_power=1W`**（压缩机+风扇全停，纯待机）。证明 companion 的 `is_on` **不是**可靠的"用户意图制冷"信号——`reconcile_state` 按 is_on 翻转 state 并打"手动"锚点属于误分类（空调自己的恒温器循环/待机被当成用户手动操作）。
- 连带污染：
  1. `_learn_from_manual` 今天被喂 ~20 条假手动样本 → `ac_user_pref.json` comfort_weight 被推到 **1.0**（纯舒适，git diff 0.4→1.0），方向性影响 DP 开机意愿；
  2. kWh 计量：manual 锚点分支在 `update_kwh`（`ac_watch.py:993`）之前 `return`，锚点窗口计量被跳过（`_prev_kwh_ts` 停在 14:48:41），今日 `_daily_kwh=1.048` 为**保守低估**（但震荡期压缩机实际负载≈0，粗核与日志一致，误差可接受）；
  3. 传感器离线期间 rh 卡 62（`_learn_from_manual` 用 `rh_history[-1]`，湿度偏好学习同样被污染风险）。
- **修法建议（等 Hermes 复核后实施，本轮零产品改动）**：`reconcile_state` 判 manual 前增加负载门控——`is_on=False` 但本次/上次 `load_power` 存在且 `_prev_power` 曾 >300W 的翻转，不应打 `manual_off_at` 锚点，只对账 mode；或复用 H2 的 `FAN_ONLY_POWER_MAX`/`COMPRESSOR_POWER_THRESHOLD` 判据（`ac_watch.py:282-313` 已有现成常量）。另注意 `reconcile_state`（:778 调用）先于 H2 接管判定（:789）执行，reconcile 先翻 mode 会吃掉接管事件，H2 可能永远不触发——需一并核对。

---

## 二、疑点2 定性

| 文件 | diff 内容 | 定性 |
| --- | --- | --- |
| `metrics_v1.py` | +2 行：`companion_target==0 → None`（防 miio 失败模式返回 0 伪设定温，带 v8.42 注释） | **合理微调，建议提交**（属 v8.42 回声字段的配套防御，等 Hermes 复核后 commit） |
| `ac_prefs.json` | `manual_actions` []→1 条（08-31 13:47 user_command on/target25）+ 末行 newline 丢失 | **运行时脏**，正常；键集无变化；newline 噪音是 v8.36 已知问题（ac_advisor.py:752 注释） |
| `ac_user_pref.json` | `comfort_weight` 0.4→1.0 | 键集无变化；**但结合疑点1：这是假手动样本推上去的被污染值，建议复核后复位 0.4**（本轮不动用户偏好数据） |

---

## 三、状态一致性（步骤1）

- `_daily_kwh_date`=2026-09-01 ✅；`temp_history` 12 / `rh_history` 30，均 <200 ✅
- 手动锚点残留：`manual_on_at=18:54:17` / `manual_off_at=18:42:17` 属**当前活跃锚点**（TTL 720min 设计内），非异常残留；mode=cooling 与 19:24 只读读数（is_on=True, target=25, mode=Cool）一致 ✅
- `.ac_watch.lock`：审计开始时已不存在（cron 正常释放），**无残留** ✅
- state 文件 7.5KB 健康（对比 08-29 血案 83B 空账本）✅

## 四、今日日志重建（步骤3）

- 自动动作：开 0 / 关 0（decide() 全天被锚点短路，异常但原因已明）
- 设备翻转（reconcile 对账）：≥81 次（开 41 / 关 40，平衡但模式异常）
- 压缩机窗口：14:18–14:48 真实运行 28min（0.55kWh）+ 18:54 起恢复；`_daily_kwh=1.048` ↔ 日志段粗核一致（震荡期计量跳过 + 实际负载极小）

## 五、测试矩阵（步骤4/5，Hermes venv）

| 测试 | 结果 | 备注 |
| --- | --- | --- |
| test_comfort_weight_dir | **9/9 PASS** | |
| test_day_short_cycle | **38/38 PASS** | |
| test_night_temp_gate | **18/18 PASS** | |
| test_sensor_fallback | **30/30 PASS** | |
| test_thermal_events | **14/14 PASS** | |
| test_vent_quiet_hours | **29/29 PASS** | |
| test_sustain_gate | **17/17 PASS** | 本轮修复前 AttributeError 假红（见七） |
| test_learn_ratchet | **14/14 PASS** | 直连真机未挂（传感器 19:26 已恢复） |
| test_off | PASS（脚本式） | |
| test_cloud | SKIP | 已知豁免（miio 0.5.12 --no-deps 无 cloud 模块） |
| test_humidity | TIMEOUT | 已知豁免（依赖实时读数，任务书明示勿等） |
| test_purifier | 超时 | 真机（净化器）无响应，环境依赖 |
| test_airpurifier | FAIL(env) | `No module named 'miio.airpurifier'`——与 test_cloud 同类（miio 0.5.12 --no-deps 缺模块），建议下轮加同款 SKIP 头 |
| ac_watch --selftest | **ALL PASS (v8.42)** | |

计数：**169/169 可执行用例全绿**（4 项环境豁免不计入）。

## 六、改动清单

| commit | 内容 | 性质 |
|---|---|---|
| `cea9068` | test_sustain_gate.py 对齐 v8.39 语义（-45/+35 行） | **仅测试文件**，py_compile ✅ + 17/17 ✅ + selftest ALL PASS ✅，已 push 成功（`fbe7f67..cea9068 master->master`） |

产品代码（ac_watch.py / ac_advisor.py / metrics_v1.py / cron / wrapper）：**零改动**。
不 bump 版本号：测试级修复无行为变更，v8.42 版本戳保持。

## 七、遗留事项（按优先级，等 Hermes 复核）

1. **P0** reconcile_state 手动误判（疑点1 延伸）：is_on 翻转 ≠ 用户意图，需加负载门控 + 核对与 H2 接管的执行顺序（reconcile 先行可能吃掉接管事件）——本轮只报告未实施。
2. **P1** comfort_weight 1.0 复位 0.4（假手动样本污染）。
3. **P1** metrics_v1.py 的 companion_target==0 防御：定性"需提交"，建议下轮 commit。
4. **P2** ac_collect.py 室内读数自 08-30 09:00 起连续 `indoor:TypeError(json.loads(None))`（233 次连续失败，readings.jsonl 的 temp/hum/co2 序列断流 2.5 天；ac_watch 自身读数路径正常）——疑似 ac_collect 对设备返回 None 未设防，独立 bug 建议单独排修。
5. **P2** test_airpurifier 加 SKIP 头（同 test_cloud 口径）。

## 八、红线遵守声明

- 仅只读采样上海空调伴侣 DID 2056557176 一次（`status()`，无任何 set 命令）；本溪空调/DID 90466860/91063311 全程未触碰 ✅
- 解释器全部使用 Hermes venv ✅；未动 cron/wrapper/产品代码 ✅；重放脚本未运行（无代码改动无需分支级重放）✅；无残留锁文件 ✅
