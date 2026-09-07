# ac-advisor 空调闭环审计报告（2026-09-07，omp）

> 审计时间：2026-09-07 10:45-10:50（本地）。全程只读，未改任何生产文件、未执行任何控制命令。
> python 解释器：`D:/Hermes_Data/.hermes/hermes-agent/venv/Scripts/python.exe`
> 红线核对：本溪空调未碰；上海伴侣 DID 2056557176 (192.168.71.43) 仅执行 `status` 只读查询 1 次（1010W 压缩机运行）。

---

## ① 执行摘要

1. **空调在跑、监控在跑**：用户今早 10:34:52 手动开机（1010W 压缩机运行、目标 25°C），系统处于"手动开后保护期"正常保护中；ac_watch cron 10:34 恢复后每 2 分钟正常巡检。
2. **自动化停摆 14 小时（09-06 20:45 → 09-07 10:34）**：所有空调 cron（监控/采集/告警）同时空档，根因在 Hermes gateway 侧——事件循环冻结 + 4 次重启中 3 次 UNCLEANLY 退出（SIGKILL/OOM/VM death），非本项目代码缺陷。
3. **设备不可达 1 小时（09-06 20:06-20:45）**：空调伴侣 192.168.71.43 discover 失败，系统连续 27 次控制失败、想自动开 26°C 未能执行；告警机制按预期投递（阈值 8 次即告警）。设备侧问题（电源/网络），恢复后系统自动继续。
4. **代码侧全健康**：selftest v8.51 全过；测试基线 sensor_fallback 30/0、day_short_cycle 38/0、night_temp_gate 18/0 与 skill 记录完全一致；学习偏移 temp_cooling=0（启动线=27）；decision_log=50 条（上限内）。
5. **无 P0**；1 条 P1（gateway 稳定性，需 hermes 侧调查）+ 1 条 P1（设备可达性，需现场检查伴侣）。

---

## ② 系统健康总评

| 问题 | 结论 | 证据 |
|---|---|---|
| 空调在跑吗 | **在跑**（用户手动开，非系统开） | `ac_socket_control.py status`：10:44:55 通电/1010W/压缩机运行/目标25°C/Cool |
| 监控在跑吗 | **在跑**（14h 停摆后已恢复） | cron `420cdfe1a188` last run 10:42:55 ok；ac_watch.log 10:34-10:42 连续 tick |
| 自动化在跑吗 | **运行但中断过**；当前保护期无动作 | 14h 空档 + 恢复后 5 条"手动开后X分钟，暂不自动关"（正常保护） |
| 数据采集在跑吗 | **恢复** | cron `1a652e2e7da9` 10:46:07 ok（空档最后 09-06 20:45:57） |
| 告警在跑吗 | **恢复** | cron `8acdd78bbdc2` 10:40:54 补投 27 次失败告警（SEEN 已更新） |
| 建议 cron 在跑吗 | **正常**（每日任务） | `f5fe9cde6d38` last run 09-06 08:30:32 ok |

当前空调 1010W 属用户意图（手动开），系统保护期不干预，符合设计。

---

## ③ 发现的问题清单

### P1-1 Hermes gateway 停摆 14 小时（自动化中断根因）【hermes 侧，非本项目代码】

**证据链**（时间全部本地）：
- 09-06 20:45:04 ac_watch 最后正常 tick（output 目录 `2026-09-06_20-45-04.md`）
- 09-06 20:42:21 `errors.log`：`cron.jobs: Timed out waiting for local fire fence 8acdd78bbdc2; failing closed` + `fire claim ownership lost; interrupting stale run` ← 调度器先兆异常
- 09-06 20:48:53 `errors.log`：告警 cron 投递失败（weixin iLink rate limited ret=-2）
- 09-06 20:50 之后 gateway.log **零日志**（20:50~07:56 无任何 cron/平台事件），但进程心跳存活至 09-07 07:56（lifecycle_ledger：pid 22556，last_heartbeat 2026-09-06T23:56:52Z）→ **事件循环冻结，进程未死但不调度**
- 09-07 07:57:23 gateway.log：`Received UNKNOWN as a planned gateway stop — exiting cleanly`（外部触发重启）
- 09-07 08:01/08:09/08:21/09:00 四次重启，前三代均 `exited UNCLEANLY (no exit path ran — SIGKILL / OOM / VM death)`，各存活仅 8/10/35 分钟
- 09-07 09:00:23 第四代稳定；但空调 cron 直到 10:34:53 才恢复输出（恢复后 10:42 ok）

**影响**：14 小时内 0 次巡检、0 次采集、0 次告警（期间设备离线 1h 的后续告警被压到今早补投）。设备状态 20:45~10:34 完全盲区。

**建议**（不改本 repo）：
1. 查 09-07 07:57-09:00 四次重启根因：`hermes gateway` 日志 + 系统事件日志（是否 OOM：i7-8550U 平台内存压力）。`hermes update` 后未重启 gateway（cron list 顶部有警告）疑似关联。
2. 事件循环冻结的诱因可能是 20:41-20:48 weixin 连续 rate-limited + fire fence 超时互锁；确认 hermes 版本是否有该已知问题。
3. 长期：考虑给空调闭环加看门狗（repo 已有 `spawn_monitor.py`/`ac_watchdog.py` 但未挂 cron，AGENTS.md 已注明勿重建——需用户决策）。

### P1-2 空调伴侣 192.168.71.43 不可达约 1 小时（09-06 20:06-20:45）【设备侧】

**证据**：
- ac_watch.log 19:50:59 起 `执行 cooling target=26 → failed status_read_failed: Unable to discover the device 192.168.71.43`，至 20:45:04 共 ~28 次
- ac_alerts.jsonl：20:06:59 streak=8 → 20:45:02 streak=27（每 2 分钟一条，共 20 条）
- 当日早些 06:51-06:55 也有零星 3 次失败（夜间蓄冷尝试），之后 310 条无动作行正常 → 白天并非持续不可达
- 期间室温 28.0°C（>27 启动线），系统想开 26°C 未能执行 → 用户最终 09-07 10:34 手动开机

**影响**：约 1h 高温无制冷（若用户在屋）。系统行为正确：连续失败计数 + 告警（阈值 8 次），恢复后自动继续。

**建议**：检查 192.168.71.43 空调伴侣电源/网络（是否断电重启、Wi-Fi 弱）。设备恢复后无需人工干预。

### P2-1 告警补投 14h 滞后（停摆次生效应）

**证据**：09-06 20:45 的 streak=27 告警（`ac_alerts.jsonl` 末条），今早 10:40:54 才由 cron `8acdd78bbdc2` 投递（output 目录可见），SEEN 已更新为 `2026-09-06T20:45:02`。用户今早收到一条"连续27次失败"但已是 14h 前的旧告警。

**判定**：非 bug（wrapper 逻辑正确：新告警→投递+记 SEEN），是 cron 停摆的必然结果。可接受。

### P2-2 相邻系统：NDX 信号 cron 连续失败（非本审计范围，顺带记录）

**证据**：`ffcc15d009c2` 6 failures in a row（last run 09-07 09:12 error，Interrupted by shutdown）。与 gateway 重启周期吻合，恢复后需观察。

### 未发现（核销历史风险项）
- **target 硬夹取**：DP 热模型调度目标已 `int(min(26, max(24, ...)))` 双重夹取（ac_watch.py:1600 与 :1692），历史 55°C 问题（v8.36 修复）未复发
- **state 死键**：09-04 审计清理后无残留；`last_on_at`/`last_dehumid_adjust_at` 读写路径完整（ac_advisor.py:598 / ac_watch.py:1788+1492）
- **偏移健康**：`adjusted_thresholds.temp_cooling = 0`（±2 即污染，0 健康）
- **状态一致性**：`_daily_kwh_date=2026-09-06` 未翻日是停摆副产物（今日首读成功即翻转），非 bug；temp_history 12 条 < 200 合规

---

## ④ 测试结果

| 测试 | 结果 | 基线对照 |
|---|---|---|
| `ac_watch.py --selftest` | **PASS**（v8.51，断言式打版本号即全过） | — |
| test_sensor_fallback | 30 passed, 0 failed | 30/0 ✓ |
| test_day_short_cycle | 38 passed, 0 failed | 38/0 ✓ |
| test_night_temp_gate | 18 passed, 0 failed | 18/0 ✓ |
| test_off | PASS（24°C/60% 不启动制冷） | ✓ |
| test_thermal_events | 14 passed, 0 failed | ✓ |
| test_vent_quiet_hours | 29 passed, 0 failed | ✓ |
| test_comfort_weight_dir | 10 passed, 0 failed | ✓ |
| test_sustain_gate | 17 passed, 0 failed | ✓ |
| test_cloud | SKIP（环境缺 miio.miotcloud，0.5.12 --no-deps 装法无 cloud 模块，已知） | 豁免 |

按任务书豁免未跑：test_learn_ratchet（直连真机可能挂死）、test_airpurifier/test_purifier（依赖实时读数）。峰电时段假红判别：本日测试全绿，无需 mock 复判。

---

## ⑤ 需用户决策的事项

1. **gateway 稳定性**（P1-1）：09-06 晚冻结 + 今晨 3 次 UNCLEANLY 重启是本系统 14h 盲区的唯一根因。是否调查 hermes 侧（内存/版本）？本 repo 无代码可改。
2. **设备可达性**（P1-2）：192.168.71.43 伴侣昨晚约 1h 不可达，建议现场查电源/网络；如伴侣频繁掉线需换策略（如降级腾讯/百度源采集，见 a-stock 备源思路——不适用，此为 miio 直连，只能修设备）。
3. **（可选）** 是否接受 10:40 补投旧告警的行为；如需更早感知，可在 wrapper 加"告警时间超过 N 分钟标记为补投"提示，避免误读。本次未改。
