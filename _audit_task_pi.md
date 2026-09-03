# AC-Advisor 审计任务书（2026-09-02 晚, v8.44 基线 96f685d）

## 背景快照（Hermes 摸底）

- HEAD=v8.44 (96f685d), 工作区: M _audit_task_pi.md, M ac_data/readings.jsonl, 未跟踪报告2份+日志+nul
- state: mode=cooling, target=25, 今日 kWh=8.95, last_on 14:52, last_off 16:24, _phantom_gate_at=09-01 21:12（正常诊断标记）
- 学习: temp_cooling 偏移=0 ✅, decision_log=50 满额
- v8.43 锚点震荡修复(53d32d9) + v8.44 棘轮修复(96f685d) 均已上线

## 审计范围（按 SKILL.md 标准清单）

1. **状态一致性**: _daily_kwh_date=今天? 手动锚点残留? histories<200?
2. **偏移健康**: adjusted_thresholds 全键 ≈0（已初验 temp_cooling=0, 查其余键）
3. **日志重建**: ac_watch.log 今日 开机/关机配对, 开机数=关机数±1?
4. **死代码检测**: state 键写操作 grep（用 state["_x"]= 模式）
5. **数值卫生**: DP/热模型 target 是否有物理夹取
6. **验证链**: py_compile（Hermes venv python!）→ selftest ALL PASS → 全量测试套件
   - 解释器必须 D:\Hermes_Data\.hermes\hermes-agent\venv\Scripts\python.exe
   - 测试纪律: current_price/load_learned/save_learned 三件套 mock; save_learned 不 mock 会血洗 ac_learned.json
7. **wrapper 一致性**: git hash-object 对比执行位(~/.hermes/scripts/ac_watch_wrapper.py) vs hermes_cron/ 受管副本
8. **v8.43/v8.44 效果层抽查**: _anchor_oscillating 是否还在正确工作（_phantom_gate_at 最近还在打=门控活着）; 锚点震荡复发检查（recent 日志中 manual_on/off 频次）
9. **峰电时段测试**: 若白天跑测试挂启动用例, 先判环境依赖假红再下结论

## 边界

- 发现问题: 当场修 + 验证 + commit（语义化 message）+ push（失败下轮重试, 勿反复）
- 红线: 本溪空调禁区; 唯一目标 DID 2056557176 本地 miio; 不动 cron 结构
- 测试 mock 血案预防: 严禁把空账本写回真实 ac_learned.json
- 不需要改的不要动: v8.44 刚上, 无新故障报告, 本次以"健康检查+小修"为主

## 交付

- 逐项结论表（✅/⚠️/❌ + 证据）
- 修复项: diff 摘要 + 验证输出 + commit hash
- 综合健康度评价 + 下一步建议
