# AGENTS.md — 定频空调省电顾问（ac_advisor）

> 给 AI 代理的接续指南。**新会话开工前先读本文件**，10 秒恢复上下文。
> 最后更新：2026-08-14（v8.2.1 + Xiaomi Sound TTS 语音接入专项）

## 项目是什么

上海闵行 70 平（关厕门 60 平），两台松川 1.5 匹定频空调（KFRd-35GW，制冷 3500W / 输入 1076W / COP 3.25），实际只用一台（女儿屋）+ 风扇循环。脚本每天 7:00 由 Hermes cron 跑一次，把省电建议推送到微信。

## 当前架构（2026-08-11 快照）

| 位置 | 角色 |
|---|---|
| `D:\work\ac-advisor\ac_advisor.py` | **唯一维护点**（代码 + ac_state.json + miio_config.json 都在此目录） |
| `C:\Users\Administrator\.hermes\scripts\ac_advisor.py` | **薄包装器**（6 行）：exec 跳转到 D:\work 真实脚本 |
| cron job `fc62a2bfd83d` | 每天 7:00，deliver=weixin:o9cq804ZESqhOlQPsZtE5HOrHIOY@im.wechat，no-agent 直跑脚本 |
| `D:\Knowledge\03_Resources\定频空调省电顾问.md` | Obsidian 规则文档（决策逻辑 + 修正记录） |
| git 仓库 | 本目录，首次提交 `2f0fbcf`（2026-08-11） |

### 为什么是薄包装器而不是符号链接
Hermes cron 有安全校验：`--script` 只认 `~/.hermes/scripts/` 下的路径，且**符号链接解析到 scripts 目录外会被 Blocked**（`script path resolves outside the scripts directory`）。所以 cron 侧放一个 exec 包装器，真实代码留在 D:\work。

### 关键注意点
- 脚本内 `SCRIPT_DIR` 用 `os.path.realpath(__file__)` 解析（不能用 abspath，符号链接场景会错）
- 状态文件**唯一**：只写 `D:\work\ac-advisor\ac_state.json`（曾因双份脚本导致状态双份，已修复）
- **凭据唯一事实源 = `miio_config.json` 顶层 `ip`/`token`**：净化器 token `f92f4e...` 为权威（read_indoor 实际使用）；`test_humidity.py` 内的 `c126...` 为旧 token，**不得当作凭据引用**（2026-08-14 确认，暂不改该测试文件）
- 运行日志自动存 `~/.hermes/cron/output/fc62a2bfd83d/*.md`，无需手工记录
- **改代码只需改 D:\work 一份，无需手动同步**（旧 cp 双份机制已废弃）

## 决策逻辑（v4 状态机）

- 主信号 = 室内实测温湿度（小米净化器 4 Lite，IP 192.168.71.120）；室内不可用时湿度信号=None，禁用除湿分支
- 体感/室内温 ≥ 28°C → 制冷，设 max(26,min(28,室内-2))°C（v4.5：原"室外-7"在室外 35°C 时设定=室温会到温停机空转）
- 温度 [26,28) 且室内湿度 > 70% → 除湿
- 温度 ≥ 26 → 风扇够用
- 温度 < 26 → 不用开空调
- 除湿逃生门：温度 < 24°C 无条件关除湿（防越吹越冷）
- 除湿关标准：湿度 < 60% 或 温度 < 24°C（OR）
- 滞回：除湿开 70%、关 60%
- 状态约束：开 ≥ 40 分钟才关，关后 ≥ 30 分钟才开，连续运行 ≥ 180 分钟提醒切换
- **开窗/关窗（2026-08-11 新增）**：有雨（降雨≥45%，2026-08-12 与换气闸门对齐），或 室外湿度≥85% 且比室内高≥10 个百分点 → 关窗防潮；否则可开窗通风
- **⚠️ 防潮警示放结论前**（防投递链路润色丢失）
- 执行技巧（2026-08-12）：建议「集中一轮 40~60 分钟后关」时，用户在睡觉/出门没法守候 → 用**遥控器定时关机 60 分钟**代替手动关（到点自动停，与湿度降到 60% 关基本等价）

## 数据源

1. 室内 = 小米空气净化器 4 Lite（zhimi.airp.rma3）：温度 siid=3 piid=7，湿度 **siid=3 piid=1**（不是 siid=4 piid=1，那是滤芯参数——已踩过坑）
2. 室外 = Open-Meteo（上海闵行 31.11,121.38）

## 常用命令

```bash
cd /d/work/ac-advisor
python ac_advisor.py              # 手动跑一次（真实天气+真实室内）
python -m py_compile ac_advisor.py  # 语法检查
hermes cron run fc62a2bfd83d      # 触发 cron（走包装器）
hermes cron list | grep -A 10 fc62a2bfd83d   # 查状态/投递结果
cat ~/.hermes/cron/output/fc62a2bfd83d/$(ls -t ~/.hermes/cron/output/fc62a2bfd83d | head -1)  # 最新输出
```

## 2026-08-11 改动记录

1. **开窗/关窗矛盾修复**（Cline 审查发现）：雨天曾输出「开窗+风扇」与「关窗防潮」自相矛盾 → 警示前置 + 室外湿度纳入判断 + 分支 D 动态化
2. **阈值调优**：关窗阈值不设 80%（上海夏季清晨室外湿度常 85-95%，晴天也如此，会误判）→ 改为 85% + 比室内高 10 个百分点
3. **cron 投递修复**：deliver 从 `all`（含被移出群的飞书，报 230002）→ 微信单渠道
4. **单份代码结构**：符号链接被 Hermes 安全校验拦截 → 薄包装器方案，状态文件唯一化
5. **git 管理**：本仓库初始化，首次提交
6. **开发者模式**：已开启（AllowDevelopmentWithoutDevLicense=1），供未来符号链接/软链接使用

## 待办 / 已知问题

- [ ] iLink 限流（30s cooldown）：早上多个 cron 集中发消息会触发，属临时现象；若频繁出现考虑错峰或加重试
- [ ] **已知设计盲点（部分已解）**：B 分支"集中除湿一轮"原只有决策语义，无自动停止执行器——**v8.2.1 ac_watch 已实现自动停止（MIN_RUN≥40 且 RH≤66 → 关，硬上限 90min）**；完整 v9 burst lifecycle（run_reason/planned_off_at/独立执行器）仍为未来项
- [x] **B 类：`load_state()` 失败静默返回 default 偏危险** —— **v8.15 已修（2026-08-16）**：损坏时打印 `[ERROR] state load failed: ...` + `_state_load_failed` 标记；ac_watch 检测到标记 → 本次 tick fail-safe 跳过（不执行开/关），防丢 MIN_OFF 锚点
- [x] **P2-b / CLI execution path parity** —— **v1.1 已修（2026-08-16）**：ac.py on/off/temp/mode cool/dry 改走 `apply_and_commit()`（command→verify→按真实设备写 state），手动操作不再留 stale state；mode heat/auto 为手动裸模式（状态机不建模）走 send_command + 手动对账记账

## v9 路线图（2026-08-14 GPT 战略评审，按优先级）

1. **Burst lifecycle**：决策层管"为什么开"，执行器独立管"开多久"（state schema 增 `run_reason/planned_off_at/execution_id` + 独立定时执行器）
2. **功率+RH 曲线学习**：用真实运行数据（起始RH→时长→功率→终RH）替换 COOL_DUTY/DEHUMID_DUTY 拍脑袋系数；积累多晴天/雨天样本再标定
3. **用户手动行为记忆**：`user_manual_off_at` + `auto_resume_block`（手动关后 N 小时内降低自动权重，非完全禁止）
4. **预测性提前控制**：结合天气趋势/历史时段，提前降湿避免下午压缩机大功率
5. 复杂 AI 最后

> **v8.2 本质 = 定时采样 + 状态机执行（07:00 每日一拍）**，无后台持续监控（样本 #1 的 12:21/13:00 读数均为人工查询，非系统监控）。真正的 v9 终态 = **感知→判断→执行→持续监控→停止/再启动 闭环**（对应 #1 burst lifecycle + 独立监控执行器，采样 5-15min 级）；v8.2 数据积累阶段先回答"多久开一次 / 开多久 / 什么湿度值得启动"，再建闭环。

### 控制哲学（GPT 建议 2026-08-14，v9 候选，今日冻结不实施）
- 目标：RH **60-70% 附近缓慢波动**，不追求压到 55-60%
- 启动两层：①RH≥70% & 室温≥26°C → 开一轮（= 现行 B 分支触发，**已实现**）；②RH 65-70% 且明显闷热、趋势持续上升 → 才开短 burst（**新档，v9 候选**）
- 停止：~66% 即可停（burst 已为 40-60min 时间界，天然落在此区间）；样本 #1 验证 72%→66% 仅 0.95kWh

## 数据积累（v8.2 当前阶段，2026-08-14 起）

- 每条真实运行样本追加到 `D:\work\ac-advisor\samples.jsonl`（当前最大价值：房子对空调的真实响应曲线，替换 DEHUMID_DUTY/COOL_DUTY 经验系数）
- 样本字段：`start_rh / start_temp / weather(雨晴) / mode / duration_min / avg_power_w / energy_kwh / end_rh / ts`
- 未来可选字段（积累稳定后再加）：`comfort_before/comfort_after`（人体感受，非 RH 数字）、`outdoor_snapshot`（outdoor_temp/outdoor_rh/rain——同室内 72%RH 晴天降湿快、雨天墙体释湿降湿慢）
- 样本量参考：10 条只够看趋势；20-50 条可拟合；100+ 条才可自动调参（防偶然天气错误学习）
- 目标至少 10-20 条覆盖：雨天高湿 / 晴天高湿 / 梅雨 / 夜间 / 白天，再标定
- **样本 #1 完成（2026-08-14）**：雨天 26°C/72%RH → cool24×53min/1075W/0.95kWh → 66%RH（净降 6pp），执行/状态/数据三闭环 ✓，第一条真实"房屋响应曲线"
- **优化指标修正**：不追求"最低耗电"，用 **单位舒适小时耗电 = 舒适小时/kWh**（舒适约束下最低耗电，类比"到达+安全+时间接受下最低油耗"）

## 2026-08-12 v4.5 改动记录（Buffy 审计修复）

1. **分支 A** 设定温度改 `室内-2`（防设定=室温时压缩机空转），模拟验证通过
2. **删死代码**：分支 C dehumid 块、分支 D 湿度>80 块（均不可达）
3. **ac_off_alert 落地**：off/fan + 湿度>78% + 温度≥24 → 提醒开空调压湿度（state.last_alert_day 每天限 1 次）
4. **last_off_at** 只在运行→关闭转换时刷新（修复"已关 X 分钟"锚点失真）
5. **密钥安全**：review_*.py 的 key 移入 `.env`（已 gitignore），`miio_config.json` 已 `git rm --cached`（含设备凭据，勿重新 add）
6. vent_reminder（桌面）坐标已统一闵行 31.11,121.38
7. 本文件 `timezone` 未使用说明已删（代码已清理）
8. **夜间方案对比块**（`night_cost_lines()`）：睡前 20:00~6:00 展示 0️⃣压一轮（用户现行打法，基准 ≈0.15元）+ 睡眠+26/24 + 除湿 四档估算（睡眠每小时+1°C 模型；除湿无睡眠联动）。湿度>70% 提示先压一轮再睡眠兜底；湿度不高提示压一轮收工最省

## 2026-08-14 v8.2.1 改动记录（ac_watch 自动监控闭环）

用户决定实施 GPT 方案：**感知→判断→执行→验证→提交 每 10 分钟自动闭环**（原 07:00 顾问 cron 保留）。

1. **`ac_advisor.py` 重构**：从 main() 抽出 `apply_and_commit(new_mode, target_temp, state)` = ac_apply→verify→commit 一次闭环，**行为不变**；advice(cron) 与 watcher 共用统一控制接口，禁止再复制裸 set_power/save_state 路径
2. **新增 `D:\work\ac-advisor\ac_watch.py`**（唯一维护点）+ 薄包装器 `~/.hermes/scripts/ac_watch.py`（同 ac_advisor 模式）：
   - 启动：`T≥28°C` OR（`T≥26°C` 且 `RH≥70%`），目标温度对齐 advisor 分支 A/B（T≥28→max(26,min(28,T-2))；26-28&RH≥70→24）
   - 停止：已运行 `≥MIN_RUN(40)` 分钟 且 `RH≤66%` → 关；硬上限 `MAX_RUN=90` 分钟 → 关
   - 保护：关后 `MIN_OFF(30)` 分钟内不重开；夜间（23-7）不自动启动
   - 复用 v8.1 接口：`load_state → reconcile_state → decide → apply_and_commit`，决策为纯函数 `decide()`（13 条 selftest 断言）
   - `--dry` 干跑 / `--selftest` 自测
3. **cron job `1d6c5460de5e`「空调自动监控」**：`*/10 7-23 * * *`，no-agent 直跑脚本，deliver=local（不占微信限流）；运行日志 `D:\work\ac-advisor\ac_watch.log`
4. **阈值对齐 GPT 方案**（停止 RH 66% 对齐样本 #1 实测 53min→66%，非老 60% 关线——watcher 主动停，不必压到 60）
5. 与 07:00 顾问 cron（fc62a2bfd83d）并存无冲突（顾问 07:00 启动后 watcher 会延续/按目标停止）；ac_off_alert 提醒 cron 保留不动
6. **已启用实控**（当前 27°C/68% 未达触发，安全）；次日 14:10 起每 10 分钟巡检
7. **BOM 根因修复**：13:00 人工对账时 ac_state.json 被写入 UTF-8 BOM，而 `load_state` 用默认编码读 → json.load 抛异常被吞 → 静默返回 default（mode/锚点全丢，MIN_OFF 失效、明早顾问会失忆）。修复：`load_state` 改 `encoding="utf-8-sig"`（BOM 容错）、`save_state` 显式 `encoding="utf-8"`、清掉现存 BOM。**教训：以后任何工具写 json 后必须验证 load_state 可读回**（watcher 日志 mode=None 即为告警信号）
8. **状态所有权收紧（审查 A 类，2026-08-14 15:00 完成）**：原实现 watcher/main 都在 `apply_and_commit` 调用前先改 state（mode/run_start/last_on_at/last_off_at）→ 若 `ac_apply` 失败，`save_state` 会把"意图态"落盘，空调实际没开 state 却显示 cooling（正是 P2 要消灭的问题）。修复：**`apply_and_commit` 成为 state 字段唯一写入者**——ac_apply→verify→`apply_state_from_verify`（按真实插座结果更新锚点）→commit；控制失败不改锚点只落盘当前真实状态；verify 矛盾按真实修正 + status=failed；verify 不可达不改锚点 + failed(verify_unreachable)。watcher 与 main 约束块均删掉提前改 state（main 只保留 new_mode/decision 文案调整）。dry-run 只 print 不触碰 state。副作用顺带清掉 P2-b 遗留的陈旧 run_start 锚点
9. **观察运行状态（2026-08-14 审查定级）**：**v8.2.1 = 工程实现完成，进入真实观察运行**。已实机证据：cron `1d6c5460de5e` 14:10 tick completed ✓、watcher no_action 路径 ✓（27°C/68% off，未误开）、production main ✓、dry-run ✓、13 decision + 9 commit selftest ✓。**待实机证据（两条触发链路）**：① 首次 `T≥28 或 (T≥26&RH≥70)` → 自动开机 → verify ON → state commit；② 运行 ≥40min 且 RH≤66% → 自动关机 → verify OFF → state commit。拿到两条证据才标"运行时验证完成"。期间不再改代码。已知观察（暂不动）：成功动作路径 ac_apply 内部 status() 预读 + verify_socket() 回读 = 两次 verify，若出现极短窗口读数跳变导致两次结果不一致再评估简化
10. **ac_control_init 漏调用 bug（2026-08-14 14:31 实测发现，修复）**：14:30 RH 升到 70% 触发启动决策，但执行 failed `control_unavailable` → 空调未开。根因：ac_watch.py 只调了 `read_ac_power()`（内部自建 miio 实例），从未调 `ac_control_init()`，全局 `AC_CTRL=None` → `ac_apply` 返回 control_unavailable。修复：watcher 补 `A.ac_control_init()`。修复后 14:31:25 **第一条自动启动实机证据**：cooling/24 action 开机 → verify ON → state commit（mode=cooling/run_start/last_on_at=14:31:24）✓。教训：**任何复用 ac_apply/apply_and_commit 的新执行入口必须先 ac_control_init()**（对照 ac.py 都有调）
11. **✅ 首次完整运行时闭环验证通过（2026-08-14 15:20）**：两条触发链路实机证据全部到手——① 自动启动 14:31（RH70→cooling/24→verify ON→commit）；② 自动停止 15:20（已开 49min≥40 且 RH57≤66→off→verify OFF→commit，state mode=off/run_start=null/last_off_at=15:20:37）。中间正确性：15:10 RH59 但已开 39.2<40 正确续跑（MIN_RUN 保护生效）。全样本 27°C/70%→25°C/57% 共 49min（比样本#1 的 53min→66% 更快更干）。**v8.2.1 = 工程实现 + 完整运行时闭环验证均通过**，watcher 继续每 10 分钟巡检。
## 2026-08-14 v8.3 压缩机状态识别层（基于 load_power，已实power，已实装）

> 核心改动：从 `is_on`（意图状态）升级为 `load_power`（实际压缩机状态），修复定频空调到温停机后系统误判为"仍在除湿"的问题。

### 做了什么
- **新增 `compressor_state(load_power)`**：`>300W=压缩机运行` / `5~50W=仅风扇` / `≤5W=关机` / `None=未知`
- **假运行检测**：`fan_only` + RH>66% + 持续>10min → 自动降 2°C 重启压缩机
- **压缩机重启冷却 30min**：存 `now+30min` 的 ISO 时间戳，`datetime.now() < cooldown_until` 判断
- **湿度达标自动关**：假运行中 RH≤60% → 直接关
- **重启状态恢复**：首次启动见 `fan_only` 且无记录 → 自动补 `last_compressor_stop_at=now`，本轮生效
- **35 条 selftest + dry-run 验证通过**

### 阈值
| 参数 | 值 | 含义 |
|------|-----|------|
| COMPRESSOR_POWER_THRESHOLD | 300W | 高于此值=压缩机在转 |
| FAN_ONLY_POWER_MAX | 50W | 5~50W=仅风扇，压缩机停 |
| COMPRESSOR_FALSE_RUN_MIN | 10min | 停多久判定为假运行 |
| COMPRESSOR_RESTART_COOLDOWN | 30min | 重启后冷却期 |
| COMPRESSOR_RESTART_DROP | 2°C | 假运行时降 2°C 重启 |
| COMPRESSOR_STOP_RH | 66% | 假运行检测湿度阈值 |
| COMPRESSOR_EXIT_RH | 60% | 湿度达标退出阈值 |

### 关键工程审计（用户已确认收口）
- P0 cooldown：`state["compressor_restart_cooldown_until"] = (now_dt + timedelta(minutes=30)).isoformat()` ✅ 实测 1800s
- P1 重启恢复：`last_comp_stop = now_ts` 写入后同步本地变量，首个周期生效 ✅

## 2026-08-14 Xiaomi Sound TTS 语音接入专项（v8.2.1 + 增量，已实装）"}]

> 本次改动 = **v8.2.1 + Xiaomi Sound TTS 增量功能已实装**；主控制阈值与状态机一字未改（冻结边界覆盖项，非推翻）。

### 做了什么
- 新增 `D:\work\ac-advisor\xiaomi_tts.py`（独立 TTS adapter）：云端 MiNA 调 Xiaomi Sound（miotDID=501560617 → deviceID UUID 运行时解析，进程内缓存）
- `ac_watch.py` 接入：**仅在 `ctrl["status"]=="action"`（command→verify→commit 成功）后**播报「空调已自动开机/关机」；failed / verify 矛盾 / no_action / --dry 一律不播
- TTS 自身失败**静默返回 False，绝不影响空调控制主链路**；12s 超时防挂死；selftest 13 decide + 9 状态路径全部通过
- 凭据落盘：`C:\Users\Administrator\xiaomi_auth.json`（micoapi ssecurity/serviceToken）+ `C:\Users\Administrator\.mi.token`（passToken，token 过期时静默刷新，无需重新扫码）
- 手动播报：`python xiaomi_tts.py "文本"`

### ⚠️ 10012 根因（最容易再次踩的坑，务必先读）
- **现象**：OTP 短信登录在 `verifyPhone` 阶段返回 `code 10012 非法请求`（昨天同一份代码还能收码）
- **真实根因（不是限流！）**：小米账号侧**灰度切换了 2FA 流程**——`serviceLoginAuth2` 的 notificationUrl 从旧的 `/identity/authStart`（miservice `_verify_otp` 只认识这个）变成新的 `/pass2/redirect?sid=passport` → 打开后跳 `fe/service/userCross`（React SPA）→ `identity/list` 返回 10001、`verifyPhone` 返回 10012
- **不要再**：反复撞 OTP（会真的触发风控）、改 UA/deviceId 硬凑（随机 deviceId 是必要的，固定 D84D92 反而 auth 70016）
- **正确解法 = QR 扫码登录**（手机米家 App 确认，零短信）：
  1. `GET account.xiaomi.com/longPolling/loginUrl`（qs/callback/sid=xiaomiio）→ 得 `qr`(图)/`lp`(长轮询)/`timeout`，**响应带 `&&&START&&&` 前缀需剥离**
  2. 下载并打开二维码图 → 用户扫码确认
  3. 长轮询 `lp` → 返回 `userId/ssecurity/cUserId/passToken/location`
  4. 跟随 `location` → xiaomiio serviceToken
  5. **passToken 复用静默登录 micoapi**（`acct.login('micoapi')`，无验证码）→ micoapi serviceToken → MiNA TTS
- **正式工具**：`D:/work/ac-advisor/qr_login_tts.py`（QR 扫码会话缓存 `qr_session.json` 在本项目目录，10 分钟内免重复扫码；用法 `python qr_login_tts.py [--force|--no-tts]`，token 过期时一键重新登录 + TTS 验证）

### 其他已验证结论
- **LAN miIO 不能 TTS**：`miIO.speaker_command` 对 l16a 返回 `user ack timeout`（-9999）——Xiaomi Sound 的 TTS 只能走云端 MiNA，局域网只能做设备状态/控制
- **Chrome CDP cookie 不能直接自动登录**：web 会话的 passToken 调 serviceLogin 仍返回 70016 跳登录页（UA 不匹配 web 签发态）；QR 登录才是干净的静默路径
- 环境注意：aiohttp `cookie_jar.update_cookies()` 需传 `URL` 对象（非 str），且畸形 cookie 名会抛 `CookieError`——注入前按白名单过滤

### 2026-08-15 cron 全天化（修复凌晨无人巡检）
- `空调自动监控` job `1d6c5460de5e`：schedule `*/10 7-23 * * *` → `*/10 * * * *`（脚本内夜间模式 NIGHT=(23,7) 本就设计通宵运行，此前被外层 cron 窗口卡死，凌晨 0–7 点零巡检）
- 事件：23:50 停机 T=23/RH=69 → 01:25 闷到 27°C/71%/AH=18.3 无人开；手动 tick 即自动开机 27°C

## 2026-08-16 v8.12-v8.14 改动记录（OpenCode 接续 + 三模型交叉审查）

> 版本线：v8.12（OpenCode 接续）→ v8.13（glm-5.2 + nemotron 审查）→ v8.14（E 谷电积极版）。全部在 `ac_watch.py`，git 已提交（`0fa8a5f`）。

### v8.12（OpenCode 接续，审计 A-D 方案）
1. **P1 修 bug**：关机分支只清 `cycle_comp_total`，遗留 `compressor_on_min`/`compressor_on_since` → 下周期时长虚高（实测 16:30 周期 24min = 残留14+实际10）。修复：off 分支同时清零
2. **除湿起步 24→25°C**（`DEHUMID_START_TARGET=25`）：治 27°C 开机 12 分钟吹到 23°C 触发逃生门早停
3. **cycle_log 透传 `abort_reason`**：停机原因可审计
4. **效率统计只算完整周期**（`MIN_COMP_MIN=40`）：剔除污染短周期，报告自愈

### v8.13（glm-5.2 + nemotron-3-ultra-free 交叉审查，三处）
1. **假运行盲区兜底（F1）**：压缩机停 + 湿度 56~66% 时既不重启也不关机 → 风扇空耗。修复：停运≥10min 且 RH≤66% 直接关机
2. **升温也写冷却锁（F2）**：`last_dehumid_adjust_at` 从 `target < current_target` 改为 `!=`，防虚拟变频升温后 Tier2 立即降回震荡
3. **夜间 AH 启动温差守卫（F3）**：`temp - night_target >= 1°C` 才允许启动，防定频机压缩机不转只吹风

### v8.14（E 方案，谷电积极版）
- 22-6 谷电半价时段除湿启动阈值 65→62（`VALLEY_START_RH=62`），更早压湿省钱；峰电维持 65 不推迟
- E 原案"峰电完全不开除湿"已评估**不建议**（省钱上限 31 元/月，白天 54% 时间闷着），量化依据见 Obsidian 审计文档

### 模型可用性快照（2026-08-16 实测）
- ✅ sensenova/deepseek-v4-flash（默认）、sensenova/glm-5.2（推理型，需大 max_tokens）、opencode-zen/nemotron-3-ultra-free、opencode-zen/laguna-s-2.1-free、orca/orcarouter/free、nous/meituan-longcat-2.0:free（**带 :free 后缀**，付费版 404 余额不足）
- ❌ tokenfaucet（Sonnet5/Kimi/Qwen/V4Pro 402）、cline（Sonnet4.6 404、Gemini2.5Pro 402）、nvidia（410 EOL）、tencent MiMo（402）、freemodel（401）

### 验证
- 每次改动均过 `py_compile` + `python ac_watch.py --dry` + 纯函数冒烟
- 长期效果对比脚本：`python verify_v814_effect.py`（按 v8.12 落地时间分组，5 指标对比，`--snapshot` 存基线）
- 审查产物：`_review_strong_*.txt`（glm-5.2/nemotron/龙猫），审查脚本 `review_strong_models.py` 已入库
