# ac-advisor 全面审计任务（v8.35 后复审）— 派自 Hermes

日期：2026-08-28。仓库：D:/work/ac-advisor（HEAD=c67f3db v8.35，已推 GitHub）。
你是本仓库的审计执行者。产出=审计报告 +（如有问题）当场修复 + 验证 + commit。

## 红线（违反=任务失败）
1. 唯一控制目标=上海空调伴侣 DID 2056557176 (192.168.71.43)，本地 miio。禁止云 API 读写空调状态。
2. 本溪空调 zhimi.aircondition.ma3 (DID 90466860) 绝对禁区；91063311（爸爸家）离线机勿碰。
3. 不改 Hermes cron、不动 ~/.hermes/scripts/ac_watch_wrapper.py（只做 git hash-object 一致性核对 vs 仓库 hermes_cron/ 受管副本，不一致=报告项+用执行位副本回灌）、不重启 gateway、不碰任何模型/provider 配置。
4. 改决策/记账代码后必须：手动构造状态触发新分支实跑一次（selftest 绿 ≠ 运行时分支对，v8.31 NameError 教训）。可用 `from ac_watch import decide` 分支级重放对照新旧行为。
5. 备份惯例：改动前 `*.bak-20260828[-tag]`；收尾 commit + push（被墙失败记录原因即可，下轮重试）。

## 审计清单（按序执行）
0. 先读仓库 AGENTS.md（若有）+ `git status`。已知 untracked：_archive/、ac_advice.py、ac_collect.py、ctrlc_sentinel.py、spawn_monitor.py 等——判断哪些应入库、哪些该进 .gitignore，列为报告项（本轮不必强行清理）。
1. 状态一致性：ac_state.json 的 _daily_kwh_date=今天？manual_on_at/manual_off_at 残留？temp_history/rh_history ≤200 条？_kwh_by_price_band 与 _daily_kwh 账目交叉（差>0.05 度=漏账窗口）。
2. 偏移健康：ac_learned.json 的 adjusted_thresholds.temp_cooling 应≈0（启动线=27+偏移）；±2 即有污染源，追 decision_log 找元凶。
3. 日志重建：ac_watch.log 近 48h「开机/关机」配对得段时长与 kWh 差；今日开机数=关机数±1 为健康；异常段（超 90min、runtime=0 假运行）列出。
4. 死代码检测：对 ac_state.json 每个 `_x` 键在代码里 grep `state["_x"]=` 写操作（⚠️ 必须用 state["_x"]= 模式匹配，裸 _x= 会误报）；只读不写的键其判定必然失效，列出。
5. 数值卫生：decide()/DP/热模型产出的 target 是否全部硬夹取物理区间（历史出过 target=55°C 被真执行）。
6. v8.35 声称修复的效果级验证（别只 grep 代码在位，要验到效果层）：
   a. selftest 峰电 mock：27/30°C 启动用例是否真 mock 了谷电价？
   b. DP 蓄冷缓存失效：命中后 temp<=蓄冷目标 → override False，凌晨 04:50 抖振路径是否堵死？
   c. manual_on_at 锚点过期清除是否已移出 mode 前置（off 态残留锚点会被清）？
   d. DAY_TEMP_REACHED_SLACK=0.5 是否仍在（v8.32 曾被悄悄回退到 1.0，对魔数先 git blame 历史）。
7. 测试套件全跑：`for t in test_*.py; do python $t; done`。已知豁免：test_learn_ratchet 直连真机会挂死→跳过勿等；test_humidity 依赖实时读数→timeout 跳过；test_cloud SKIP=环境缺 miio.miotcloud（0.5.12 --no-deps 装法所致），非产品 bug。峰电时段（白天）跑测试若启动类用例挂一片，先判时段依赖（文件级 `A.current_price = lambda: 0.307` 注入已在 day_short_cycle/night_temp_gate/sustain_gate 落地，只验分支逻辑不测峰谷策略），修测试不改产品。
8. 停止出口枚举：`grep -n 'return ("off"' ac_watch.py` 全列一遍，逐条确认该有的前置（温度达标/最小有效运行）没再被砍——v8.32→v8.33 教训：一个 bug 有多条出口，修一半=没修。
9. 日志证据链：关机行无 reason 字符串（apply_and_commit 成功路径不回传），抽 3 个近期关机点用决策日志重放反推走了哪个分支（参数法见 references/replay-20260827-v833.md）。

## 验证链（改了任何东西后）
py_compile 两文件 → `python ac_watch.py --selftest` 全 PASS → 手动触发新分支实跑 → wrapper 实跑（唯一执行位 ~/.hermes/scripts/ac_watch_wrapper.py，预期静默或真实动作行；「无需动作」类例行行被吞=正常）→ commit。

## 环境备注
- python：repo .venv 或 Hermes venv（D:/Hermes_Data/.hermes/hermes-agent/venv/Scripts/python.exe，miio 0.5.12 --no-deps 已装，验证 `python -c "import miio; print(miio.__version__)"`）。
- Windows shell 避坑你自己的 AGENTS.md 已有，不再重复；多行 PowerShell 一律写 .ps1 文件用 Windows 路径 -File 执行。
- 参考资料（可读）：D:/Hermes_Data/.hermes/skills/iot/ac-advisor-ops/ 下的 SKILL.md 与 references/system-map.md、references/audit-playbook.md、references/replay-20260827-v833.md。先读 system-map。
- 「上次修好了」不可信，每轮实测。

## 产出
最终报告（简体中文）写到 D:/work/ac-advisor/_audit_report_pi.md，stdout 只留摘要：
- 结论先行：系统当前健康度一句话
- 发现清单：按 [严重/建议/观察] 分级，每条带证据（文件:行号 / 日志时间戳）
- 修复清单：改了什么、怎么验证的、commit hash
- 遗留：没修的说明原因
