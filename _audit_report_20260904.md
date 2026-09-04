# 空调策略审计报告（2026-09-04 傍晚）

> 审计范围：Hermes cron 实控链路（jobs.json）+ ac_watch.py v8.45 决策引擎 + 今日全天运行实录。
> 红线遵守：全程只读（未触碰空调伴侣、未下发控制命令、未改产品代码、未动 cron/wrapper）。
> 上一轮审计：`_audit_report_20260901.md`（P0/P1 遗留项追踪见下文）。

---

## 一、总体结论

**策略本体健康，v8.43–v8.45 三轮修复已落地且 169/169 测试全绿；但 09-01 审计的 P1（comfort_weight 被幻象样本污染）已复发**，根因是 v8.43 幻象门控在「功率断表（load_power=None）+ 翻转间隔略大于 20 分钟震荡窗口」场景下仍会泄漏。另有运维层问题：看门狗/换气提醒/早报等 4 个 cron 在 08-25→08-27 之间被清掉，AGENTS.md 未同步，告警链路实际缺位。

---

## 二、09-01 遗留项追踪

| 项 | 状态 | 证据 |
|---|---|---|
| P0 reconcile 手动锚点震荡 | **部分修复**（v8.43/43b，commit ee159a9 之后的 53d32d9） | `ac_advisor.py:1006-1122` 已带 load_power 门控 + `_anchor_oscillating`；但见下文「复发」 |
| P1 comfort_weight 复位 | **已复位后又复发** | v8.44（96f685d）改净优势棘轮并复位 0.5；当前工作区运行值又被打满 **1.0**（git diff: 0.5→1.0） |
| P1 metrics companion_target==0 | **已提交**（77a4376） | git log 确认 |
| P2 ac_collect TypeError 断流 | **已修复**（ee159a9 云读取→本地 miio 直读） | readings.jsonl 恢复流式写入，最新 17:30 |
| P2 test_airpurifier SKIP 头 | 未验证（本轮未跑该用例） | — |

## 三、🔴 P1 复发：幻象「手动开」锚点再次污染 comfort_weight（本轮核心发现）

### 证据链（2026-09-04 上午实录）

1. **锚点入账**：07:48–10:50 之间 `ac_state.json.user_pref.manual_on_log` 新增 **9 条 mode=cooling 手动开样本**（07:48/08:22/08:44/09:00/09:20/09:42/10:06/10:28/10:50），间隔 16–34 分钟；`ac_watch.log` 同步出现 9 簇「手动开后N分钟，暂不自动关（尊重用户意图）」。
2. **物理现实**：`ac_watch.log` kWh今 曲线 07:20→11:44 **钉死在 4.94 kWh 不动**——近 4.5 小时压缩机零出力。9 次"手动开"全部是无功耗的伴侣幻象翻转。
3. **污染路径**：v8.44 净优势棘轮下 recent-10 全是 cooling → `on_count-off_count=10 ≥ 2` → 每锚点 +0.1 → 0.5 → **1.0 打满**。与 09-01 同一后果：舒适权重被推满，DP 决策系统性偏向多开机。

### 根因分析

- v8.43 门控：`load_power ≤50W` 静默；`load_power=None` 时走 `_anchor_oscillating`（20 分钟窗口）。今晨翻转间隔 16–34 分钟，**窗口边缘刚好漏过**（16 分钟的那次照理应被拦，不排除该次功率读数瞬时 >50W 或读数为 None 判定路径差异，需代码级复核）。
- 「开」翻转分支对 `load_power >50W` 无条件按真手动处理（ac_advisor.py:1111-1117 注释"物理铁证"），但实测幻象翻转也能在断表窗口拿到 None→原语义。

### 修法建议（待复核后实施，本轮零改动）

1. 「开」翻转打锚点前要求**连续两个 tick >50W 或本 tick >300W（压缩机级）**的负载证据；仅一次 >50W 先打 `_phantom_gate_at` 观察一拍。
2. `_anchor_oscillating` 窗口 20min → 35min（覆盖实测 12–34 分钟幻象周期）。
3. `_learn_from_manual` 加功耗护栏：锚点时刻前 10 分钟 kWh 增量 <0.005 时不入账（幻象锚点零功耗，一拦一个准）。
4. comfort_weight 复位 0.5（同 09-01 处置）。

## 四、cron 链路审计（jobs.json @ 2026-09-04 17:29）

| 任务 | id | 调度 | 状态 |
|---|---|---|---|
| 空调自动监控（实控核心） | 420cdfe1a188 | */2min | ✅ ok，累计 6734 次，failure_streak=0 |
| 空调数据采集(只读) | 1a652e2e7da9 | */15min | ✅ ok，1185 次 |
| 空调米家调参建议(每日) | f5fe9cde6d38 | 08:30 | ✅ ok |

**⚠️ 缺失项（对比 08-18 bak 与 AGENTS.md 声明）**：

- `cca8361f1c4c` 看门狗（ac_watchdog.py，30min 心跳报警）——**不在 cron 中**，`ac_watchdog_state.json` 最后写入 08-17，已 18 天未运行；
- `c1be342fa05b` 换气到点提醒（home_living_alert.py）——不在 cron；
- `fc62a2bfd83d` 每日 07:00 早报、效率分析、周快照——均不在 cron。

08-25→08-27 的 jobs 清理（bak-20260827-longcat 已只剩 6 job）把上述任务一并移除，但 `hermes_cron/` 里 wrapper 仍在、AGENTS.md（08-25 版）仍声称在跑。**若为有意下线请更新 AGENTS.md；若非有意，看门狗告警链路（压缩机卡死/传感器离线外部报警）目前是真空**，建议恢复或明确由 ac_watch 内部告警替代。

另：wrapper 同步核验 `hermes_cron/ac_watch_wrapper.py` ≡ `~/.hermes/scripts/ac_watch_wrapper.py`（逐字一致）✅。

## 五、今日运行行为评估

- **夜间谷电策略正常**：23:00 后 AH 门控 + 谷电预冷（03:45/04:46/05:46 三次 target=24 预冷）运作良好，周期 28–45min、duty≈0.95、单周期 0.32–0.75kWh，AH 达标即停。今日谷电段 4.02kWh / 峰电段 2.17kWh，负荷确实压在谷电。
- **压缩机保护生效**：09-03 16:50「连续运行91分钟强行关机」正确触发（WATCH_MAX_RUN=90）。
- **小瑕疵①**：09-03 23:28「谷电预湿」开机 2 分钟后被「夜间湿度已达标 AH=13.6」关掉——预湿分支与夜间 AH 停机门打架，白折腾一次启停。建议预湿分支挂起 AH 停机门或 AH 门加预湿豁免。
- **小瑕疵②**：今日 11:14/11:30 两个垃圾周期——室内传感器离线期间 temp 回退到室外缓存值（30.53 恒定），仍开了 target=28 的 cycle，压缩机 0 分钟、数分钟即中止。SENSOR_FALLBACK_ON_ALLOWED=False 只挡"开机决策"，没挡已开状态下的 cycle 记账，建议 fallback 温度来源时不开新 cycle。
- **decide() 饥饿**：上午幻象锚点期间「暂不自动关」保护反复短路 decide()（09-01 全天 545 条的轻量复现），下午恢复后 17:08/17:24 自动开/关决策正常（28°C→26°C，0.26kWh，AH 达标停机）。

## 六、验证与测试

- `py_compile` ac_watch.py / ac_advisor.py / ac_collect.py ✅
- 测试矩阵（本轮实测）：test_comfort_weight_dir 9 / test_day_short_cycle 38 / test_night_temp_gate 18 / test_sensor_fallback 30 / test_thermal_events 14 / test_vent_quiet_hours 29 / test_sustain_gate 17 / test_learn_ratchet 14 —— **合计 169/169 全绿** ✅
- git：本地 == origin/master（a0be9b4 v8.45 已推送）；工作区仅运行时脏数据（readings.jsonl、ac_user_pref.json）；.env / qr_session.json / ac_learned.json 均在 .gitignore ✅

## 七、遗留事项（按优先级）

1. **P0** 幻象「手动开」门控泄漏 + comfort_weight 复发污染（见 §三，含修法）。
2. **P1** 看门狗/换气提醒 cron 缺位与 AGENTS.md 漂移（见 §四，需用户裁决：恢复 or 确认下线并改文档）。
3. **P2** 谷电预湿 vs 夜间 AH 停机门冲突（§五①）。
4. **P2** 传感器 fallback 温度不应参与新 cycle 记账（§五②）。
5. **P3** 顺带发现：NDX T+K3 信号推送 cron failure_streak=5（最近 5 次均 "Interrupted by shutdown"，与空调无关但建议排查）。
