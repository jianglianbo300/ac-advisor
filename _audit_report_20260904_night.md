# 空调策略审计报告（2026-09-04 晚，pi）

> 审计范围：v8.46/v8.47 落地验证 + ac_watch/ac_advisor 决策链代码审查 + 今晚实录。
> 红线遵守：未触碰空调伴侣、未下发控制命令、未改 cron/wrapper；仅运维级干预（见 §三）。
> 上一轮：`_audit_report_20260904.md`（傍晚，omp/hermes 线）——本报告是其 P0 的**复发追踪**。

---

## 一、总体结论

**v8.46 的幻象门控存在结构性漏洞：今晚（20:04 审计时点）P0 已实质复发**。v8.46 声称「18 用例分支重放全绿」，但线上当晚即漏过 3 条幻象锚点（19:18/19:42/20:04），comfort_weight 已从复位值 0.5 爬至 **0.8**（4 条幻象样本全部入账，18:38/19:18/19:42/20:04）。

**运维处置（已执行）**：comfort_weight 复位 0.5（备份 `.bak_auditreset_20260904`）+ state 清理（mode=off 对账、弹 manual_on_at/_pending_manual_on_learn/_on_flip_high_at、清 4 条幻象样本）。**代码级 P0 修复建议见 §四，未实施**（应走 v8.48 + 分支重放纪律）。

## 二、v8.46/v8.47 落地验证

| 项 | 状态 | 证据 |
|---|---|---|
| v8.46 幻象门控代码在位 | ✅ | `ac_advisor.py:1112-1161`：≤50W 静默翻下打 `_phantom_gate_at`、50-300W 单帧观察一拍、>300W/连续两 tick >50W 铁证 |
| v8.47 调度 override AH 豁免 | ✅ 代码在位 | `sched_override_exempt`/`_override_run_at` 字段（state 当前 None，今晚无预冷场景可验证） |
| v8.46 修复② 震荡窗口 35min | ✅ | `_anchor_oscillating(window_min=35)`（ac_advisor.py:982） |
| v8.46 修复③ None 功率延迟学习 | ✅ 在位但**未覆盖全部路径** | `reconcile_state` 开头 `_pending_manual_on_learn`（仅 load_power=None 路径使用） |
| kWh 今/累双口径 | ✅ | 日志行 `kWh今=6.19/累=118.6` 格式正常 |
| cron 三任务健康 | ✅ | 监控 */2min ok（failure_streak=0）、采集 */15min ok、每日建议 08:30 ok |

## 三、🔴 P0 复发：门控「功率铁证」路径被幻象功率穿透（本轮核心）

### 证据链（今晚实录）

1. **4 条幻象锚点入账**：`manual_on_log` 新增 18:38/19:18/19:42/20:04 四条 mode=cooling 样本；kWh今 **全天冻结 6.19**、累 118.5816 不动 → 压缩机零出力，全部是伴侣幻象翻转。
2. **comfort_weight 0.5 → 0.8**：v8.44 净优势棘轮下 recent-10 全 cooling（`manual_on_count - manual_off_count ≥ 2`）→ 每锚点 +0.1。与 09-01（v8.43 版复发）、09-04 晨（v8.46 修复前 9 条）**第三次同型事故**。
3. **门控状态机在位但被绕过**：state 中 `_phantom_gate_at=19:54`、`_on_flip_high_at=None`（证据链被消费）、`_pending_manual_on_learn=None`。

### 根因（代码级）

`ac_advisor.py:1148-1161`「开」翻转门控：**连续两 tick >50W（≤7min）或单 tick >300W → 视为「真运行的物理铁证」→ 打锚点 + 立即 `_learn_from_manual()`（无任何 kWh 二次验证）**。

漏洞链：`read_ac_power()`（ac_advisor.py:1305-1331）在 miio 返回 `st.load_power>0` 时直接采信——**该读数同样来自伴侣 IR 信念/瞬时功率**，幻象翻转窗口完全可能拿到 >50W 甚至 >300W 的瞬时读数（与 is_on 幻象同源）。v8.46 把「功率读数」当「夹钳物理现实」，但唯一真正的物理现实是 **kWh 增量**（电表级，今晨与今晚的幻象全部表现为 kWh 冻结）。**三层事实等级应反过来：kWh 增量 > 功率读数 > 伴侣 is_on**。

`_anchor_oscillating(35min)` 只在 load_power=None 时兜底（1118、1177 行），有功率读数的路径完全绕开震荡检测。

### 修法建议（P0，建议走 v8.48）

1. **统一延迟学习**：`_pending_manual_on_learn`（10min kWh ≥0.005 验证）从「仅 None 路径」扩展到**全部打锚点路径**——真手动开机 10 分钟内 50W+ 功率累积必 ≥0.008kWh 过闸，幻象零功耗必被拦。
2. **功率铁证加 kWh 佐证**：连续两 tick >50W/单 tick >300W 路径在入账前要求锚点时刻前 10 分钟 `_daily_kwh` 增量 ≥0.002（≈12W 均值以上），否则只打 `_phantom_gate_at` 观察。
3. **震荡检测扩容**：`_anchor_oscillating` 判据从「仅 None」放开到所有路径（或至少 50-300W 区间），与 kWh 护栏双保险。
4. 复位 comfort_weight（本轮已代执行）。

### 决策影响评估（为什么没立即实施）

decide() 被 30 分钟锚点 TTL 短路的窗口有限；comfort_weight 上限 1.0 的后果是舒适偏置非安全问题；今晚已复位。v8.43→v8.47 每版都以「分支重放全绿」为纪律上线，该修复应带分支重放（幻象功率读数序列用例当前缺失——正是 18 用例没覆盖到的分支），不宜夜间裸改实控代码。

## 四、其余观察

1. **decide() 饥饿轻量复现**：19:18-20:04 期间 17 条「暂不自动关」日志（30min TTL 内每 2min 一条），与 09-01 全天 545 条同机制。锚点清理后已恢复。
2. **kWh 双账本口径**：日志显示「kWh今=6.19/累=118.6」，`update_kwh` 逻辑（ac_watch.py:422-440）在 load_power=None 时不计增量也不推进 prev_ts——断表窗口期间真实用电（若有）会低估，与幻象场景叠加后「零功耗铁证」仍可靠（今晨 4.5h 冻结不可能有真压缩机）。
3. **v8.47 未实战验证**：state 无 `_override_run_at` 记录，今晚无 DP 预冷/预除湿触发，建议下次谷电窗口观察。
4. P3（承接傍晚报告）：NDX 信号 cron `last_status=error`（Interrupted by shutdown）——实测 09-04 09:04 输出正常落盘、信号正确，属 gateway 重启竞态的**账面噪音**，非数据问题。

## 五、验证

- comfort_weight 复位后 `ac_user_pref.json` 合法 JSON、0.5 ✅
- state 清理后 mode=off、manual_on_at=None、幻象样本 4 条移除 ✅（下一 tick cron 接管）
- 全程未改产品代码、未动 cron、未发控制命令 ✅

---

## 六、v8.48 已同夜实施（本报告 §三 修法落地，commit 8256d87 已推 GitHub）

用户追问"都修了吗"后同夜补齐代码级 P0：

1. **修复① 学习喂入统一延迟验证**：功率铁证路径不再立即 `_learn_from_manual()`，与断表路径一律走 `_pending_manual_on_learn`（10min kWh ≥0.005 核验）。幻象零功耗 → 永不入账；真压缩机 10min ≥0.005kWh 必过闸。
2. **修复② 震荡检测扩容**：`_anchor_oscillating(35min)` 从「仅 None 路径」放开到全部开翻转路径（今晚 19:42/20:04 场景的精确拦截）；真运行由 H2 接管兜底，不损失真实手动。
3. **修复③ 观察一拍不再打 `_phantom_gate_at`**：观察拍自设标记会误拦下一 tick 的双 tick 证据链（场景 B 曾会被误杀）。

**验证**：`_replay_v848.py` 25/25 分支重放全绿（含今晚 J/K/L 场景精确复刻：铁证锚点+kWh冻结→学习永不入账 / 真实功耗→入账 / 窗口内高功率翻转→震荡拦截）；快测套件 7 文件 156 用例 rc=0；selftest 通过（v8.48 横幅=断言全过）；py_compile ✅。
**运行时处置**：21:20 复发污染再清（cw 0.6→0.5、清 2 条幻象样本、弹锚点）。**预期效果：comfort_weight 不再被幻象推高；decide() 饥饿只剩每 >35min 间隔的单次首翻（≤30min，假运行熔断 10min 自愈）**。
**遗留**：观察拍不打 gate 后 `_phantom_gate_at` 仅由确证幻象/静默对账分支写入——审计口径以 `_on_flip_high_at`+`_phantom_gate_at` 两者并读。
