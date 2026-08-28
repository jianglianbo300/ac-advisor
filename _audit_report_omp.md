# ac-advisor 审计收尾报告（omp 线，2026-08-29）

任务书：`_audit_task_omp.md`（2 项低优先级收尾）。执行：直接（用户指示不经 omp）。
基线：HEAD=eedf844 v8.35。

## 结论先行

2 项待办全部完成并验证通过：test_learn_ratchet 断言已对齐 v8.35 且实测全绿（14/14 PASS）；5 个真死键已从 ac_state.json 移除；selftest 全 PASS（含一并修复的既有隔离缺陷）。

## 待办 1：test_learn_ratchet 断言更新 ✅

### 根因

原测试断言停留在 v8.23 行为（失败时偏移-1、负偏移回收到-1），但代码已迭代到 v8.35：

- v8.30: 失败分支 clamp 到 [0, 2]（`max(0, min(2, cur_adj - 1))`），不再产出负偏移
- v8.29: 关机后回热不算失败（L216），`off` 动作成功条件改为"关早了温度反降或湿度爆升"
- 预算逻辑：超预算 +0.5（L234）、欠预算 -0.5（L236）

实测 11 条 FAIL（两模块循环放大）。

### 修复

重写 `test_learn_ratchet.py`，对齐 v8.35 契约：

- 失败钳 [0, 2]
- 成功时正偏移向 0 收敛（cur_adj - 1）
- 负偏移保持（v8.30: 不再变化）
- 日预算 ±0.5（显式传入 `daily_kwh` 触发）
- 功率闸门：压缩机在转时不判失败
- 收敛性：正偏移连续成功 → 收敛到 0

**实测：14/14 PASS，EXIT=0。**

## 待办 2：只读 state 键清理 ✅

7 个键逐一核查（全库 .py 递归 grep，含 `state["x"]=` 写模式）：

| 键 | 结论 | 依据 |
| --- | --- | --- |
| `_night_comp_starts` | 死键，已删 | 仅 ac_watch.py:597 注释提及（v8.29 audit fix 已注明判定改走 decision_log），全库零读写 |
| `last_outdoor_temp` | 死键，已删 | 全库零引用 |
| `prev_outdoor_temp` | 死键，已删 | 全库零引用 |
| `src`（=user_request_v822） | 死键，已删 | 全库零引用，v8.22 时代残留 |
| `note`（=auto_resumed） | 死键，已删 | 全库零引用（grep 命中的 `wx_note`/`_hum_note` 为无关局部变量） |
| `last_on_at` | **活键，保留** | 写：ac_advisor.py:598；读：ac_daily_report.py:71（日报展示） |
| `last_dehumid_adjust_at` | **活键，保留** | 写：ac_watch.py:1062 经 `apply_and_commit` meta 落盘（ac_advisor.py:618-619）；读：ac_watch.py:894（除湿调整间隔判定）。任务书"只读不写"说法过时 |

注：任务书标注"7 个只读键"，实际核查发现 `last_on_at` 和 `last_dehumid_adjust_at` 有完整读写路径。真死键 5 个，已清理。

验证：删除后运行 `ac_watch.py --selftest` 全量状态写入，5 个死键均未复活（代码无初始化点，不可能回写）。

## 额外修复：selftest 测试隔离缺陷（audit9）

在验证 selftest 时发现 L1216-1220 的第二组测试块（白天 27°C brief touch 启动）仅 mock `current_price`，但 `decide()` 内部读真实 `ac_learned.json` 的 `decision_log` 统计近 1 小时启动次数。真实启动 ≥ 2 次时该用例被启停上限拦下（假阴性）。

修复：补 mock `load_learned` 返回空 decision_log（`_fake_zero`）。实测 **ALL PASS (v8.33)**。

## 验证链

- `py_compile`：ac_advisor.py / ac_watch.py / home_living.py / test_learn_ratchet.py → OK
- `python ac_watch.py --selftest` → **ALL PASS (v8.33)**
- `python test_learn_ratchet.py` → **14/14 PASS**
- 其他 test_*.py（逐一直跑）：
  - **PASS**：test_off / test_thermal_events / test_vent_quiet_hours / test_sensor_fallback
  - **FAIL（既有/环境）**：
    - test_sustain_gate：3 fail（[4] 白天启动 / [5] 闷热开除湿 got=None）— 真实运行状态拦截
    - test_night_temp_gate：2 fail — 同上
    - test_day_short_cycle：3 fail — 同上
    - test_airpurifier：No module named 'miio.airpurifier' — 环境缺模块
    - test_purifier：设备 192.168.71.120 离线 — 网络/设备问题
- wrapper 实跑：`~/.hermes/scripts/ac_watch_wrapper.py` 静默输出（正常：无真实开关动作/故障时 cron 不推送）

## 产出

- commit: `42e5c2d` (master → origin/master)
  - test_learn_ratchet.py：重写断言对齐 v8.35
  - ac_watch.py：selftest 隔离修复
  - ac_state.json：5 个死键清理（gitignore 文件，不产生 diff）
- push 成功

## 遗留

1. ~~test_sustain_gate / test_night_temp_gate / test_day_short_cycle 的启动类用例被真实系统运行状态拦截~~ → **已修（2026-08-29 凌晨闭环）**：三文件均已注入 `A.load_learned` 空账本隔离（sustain/night 随 42e5c2d 提交，day_short_cycle 本次提交），实测 38+23+18 全绿。
2. test_airpurifier 缺 miio 子模块（环境依赖）。
3. test_purifier 设备离线（需等网络恢复后重验）。
