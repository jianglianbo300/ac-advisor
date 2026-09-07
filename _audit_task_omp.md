# 空调自动控制闭环（ac-advisor）审计任务书

> 给 omp：这是自包含任务书，严格按下面执行。你没有任何本会话上下文，所有信息都在这份文件里。

## 0. 你的角色
审计上海空调自动控制闭环（D:/work/ac-advisor）的健康状态与策略正确性。这是生产级系统，实时控制空调，审计要谨慎、证据优先，不要改任何生产代码（本次只审计，不改）。

## 1. 红线（违反 = 任务失败，绝对不可碰）
- 🔴 **本溪空调(DID 90466860)绝对禁区，不碰**
- 🔴 唯一控制目标=上海空调伴侣 DID 2056557176 (192.168.71.43)，**本次只读不写**，不要执行任何开机/关机/调温命令
- 🔴 高影响操作（改配置/重启/写代码）一律不做，只出审计结论
- 🔴 不要修改 ac_watch.py / ac_advisor.py 等任何生产文件，不 commit

## 2. 工作目录与环境
- 项目目录：`D:/work/ac-advisor`
- **python 解释器只能用**：`D:/Hermes_Data/.hermes/hermes-agent/venv/Scripts/python.exe`
  （注意：ac_watch.py 硬 `import pyttsx3`，系统 python 和 repo 的 .venv 都没装，会 ModuleNotFoundError——这是环境问题不是代码问题，看到这个报错别去改 import，直接用上面那个 Hermes venv python）
- 关键数据文件：
  - `ac_watch.log`（运行日志）
  - `ac_learned.json`（学习记录，含 adjusted_thresholds）
  - `ac_state.json`（当前状态）
  - `ac_data/readings.jsonl`（15分钟只读采集）
  - `miio_config.json`（设备 ip/token，只读用）
- git 仓库在 D:/work/ac-advisor

## 3. 审计清单（按序执行，每步记录证据）

### A. 系统是否活着（先确认，回答用户最关心的"空调在跑吗/监控在跑吗"）
1. cron 状态：`hermes cron list` 看 `420cdfe1a188`(空调自动监控)、`1a652e2e7da9`(采集)、`f5fe9cde6d38`(建议)、`8acdd78bbdc2`(告警) 是否 active、最近 last_run 是否成功
2. `tail -20 D:/work/ac-advisor/ac_watch.log` —— 看最近日志时间戳是否接近现在，有无异常
3. 空调当前真实状态：`cd D:/work/ac-advisor && <Hermes venv python> ac_socket_control.py status` 读功率/开关（>300W压缩机、>5W风扇、<5W关机）
   - 注意：刚手动开过空调的话，日志会显示"手动开后X分钟，暂不自动关（保护用户意图）"——这是正常保护，不是异常

### B. 状态一致性
4. `ac_state.json` 检查：`_daily_kwh_date` 是否今天？手动锚点(manual_on_at/off_at)是否残留？temp_history/rh_history <200 条？

### C. 偏移健康（学习回路）
5. `ac_learned.json` → `adjusted_thresholds.temp_cooling` 应≈0（启动线=27+偏移）；±2 即存在污染源。同时看 decision_log 条数（应≤50）和手动操作样本

### D. 日志重建
6. 从 ac_watch.log 配对"→ 开机/关机"动作，算今日开机数 vs 关机数（应±1 内）与段时长

### E. 死代码/数值卫生（只读检查，不改）
7. 对 ac_watch.py / ac_advisor.py 做静态检查：state 键是否有"只读不写"的（其判定必然失效）；DP/热模型算出的 target 是否硬夹取物理区间（历史出现过 target=55°C 被执行）

### F. 验证链（只读跑测试）
8. 用 Hermes venv python 跑：`python ac_watch.py --selftest`（全 PASS 为健康）
9. 测试套件：`cd D:/work/ac-advisor && for t in test_*.py; do <venv python> $t.py; done`（对照 skill 记录：sensor_fallback 30/0、day_short_cycle 38/0、night_temp_gate 18/0 为健康基线）
   - 注意：峰电时段依赖的启动类用例可能假红（is_peak 拦截），判别法见下

### G. 结论汇总
10. 输出审计结论：①系统是否健康 ②发现的真实问题（按严重度 P0/P1/P2）③每个问题的证据（文件+行号+数据）④修复建议（但本次不改）

## 4. 已知豁免 / 坑（遇到别误判）
- 峰电时段启动类测试假红：`is_peak and temp < temp_cooling+1 → return None` 会拦白天测试。判别：挂掉的启动用例，临时 mock `A.current_price = lambda: 0.307`（谷电）再跑，过了就是时段依赖不是 bug
- miio 直连设备型测试（test_learn_ratchet 直连真机）可能挂死，勿等
- test_humidity 依赖实时读数会 timeout，勿等
- venv 里若 import miio 报 `TypeError: subcon should be a Construct field` = construct 版本问题，别去修代码
- "手动开后X分钟，暂不自动关" = 正常保护期，不是异常
- ac_watch.log 关机行只含传感器读数不含 reason 字符串，无法直接知道走了哪个停机分支——要"决策日志重放"反推（手动调 decide() 枚举分支），时间有限可不做深

## 5. 产出要求
- **报告落盘**：`D:/work/ac-advisor/_audit_report_<日期>_omp.md`
- 报告结构：①执行摘要（3-5 条关键结论）②系统健康总评（空调在跑吗/监控在跑吗/自动化在跑吗）③发现的问题清单（严重度+证据+建议）④测试结果 ⑤（如有）需用户决策的事项
- **stdout 只留摘要**（10 行内），完整报告写文件
- 不要编造数据，所有结论必须能 grep 到证据
