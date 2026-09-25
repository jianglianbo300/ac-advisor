# 独立严重度复核（人读代码 + 查调用方 + 查生产数据）

> 模型给的 severity **不可直接采信**。逐条按"可达性"重新裁定。

| ID | 模型判定 | 我的复核 | 依据 |
|----|---------|---------|------|
| T5 | P1 | **P0** ✅已修 | 生产事故实证：08-21 `cooling target=55` 真实下发，持续 ~2h12m |
| T1 | P1 | **P1** ✅已修 | 生产路径可达（矛盾回读是真实现象），20 轮 0 告警 |
| T2 | P2 | **P2，需产品决策** | DP 电价套利实测无效；非 bug，是意图问题 |
| T6 | P2 | **P2，真实可达** | 见下方"唯一存活"分析 |
| T3 | P2 | **latent（死代码）** | `find_pre_cool_window` **零生产调用点**（仅 `_replay_v850.py`） |
| T4 | P2 | **latent（被旁路）** | 需 `last_hum=None`；L1197 会保留旧值；且 50/50 条生产日志都有 `kwh_at_decision` → 走第一分支，永不进该逻辑 |
| T7 | P2 | **latent（不可达）** | `decide()` **从不返回 `fan_locked`**（实测仅 `cooling`/`off`） |

## 唯一存活的新发现：T6 的前提确实可达

模型的 T6 harness 手工构造了 `{run_start:"08:00", estimated_kwh:9.0, _run_start_kwh:5.0}`
这种**不一致状态**，看着像凭空捏造。但查写入点后发现前提**真的可达**：

- `_run_start_kwh` 只在 **1 处**写入：`ac_advisor.py:1327`（手动开机锚点，与 `run_start` 同写）
- 但 `run_start` 在 **8+ 处**写入，其中两处**不**刷新 `_run_start_kwh`：
  - **`ac_advisor.py:1379`** ← `apply_state_from_verify` 的 `real=="on" and not was_on` 路径
  - `ac_watch.py:1328`

→ **存在路径：新 run_start 配旧 `_run_start_kwh`** → 门控 `estimated_kwh - _run_start_kwh >= 0.005`
用错误的起点比对，**增量被高估 → 幻象配对可能被学习**。
这正是 v8.50e 想堵的东西，但只在手动开机路径堵住了，`apply_state_from_verify` 这条漏了。

**建议修法**：在 `apply_state_from_verify` 的 `not was_on` 分支同步刷新
`state["_run_start_kwh"] = state.get("estimated_kwh")`（与 L1327 对齐）。
**与 T5/T1 同源**：`_run_start_kwh` 的"起点"语义没有在所有 `run_start` 写入点保持一致。

## 方法论教训
**"代码有缺陷" ≠ "缺陷会发作"。** 严重度必须三步：
1. 读代码确认逻辑错；
2. **查调用方**确认可达（T3/T7 死在这步）；
3. **查生产数据**确认是否已发生（T5 靠这步从 P1 升到 P0；T4 靠这步被降级）。

模型只做了第 1 步。**但它构造的"人工状态"不该一概否定** ——
T6 那个看似捏造的 state，反而指出了一处真实的写入点不一致。
**别急着把"我构造不出的状态"判成幻觉，要去查它能不能被真实路径产生。**
