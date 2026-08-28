# ac-advisor v8.35 审计收尾（派自 Hermes，2026-08-28）

仓库：D:/work/ac-advisor（HEAD=c67f3db v8.35，已推 GitHub）。
本轮审计已由 Hermes 跑完 10 项清单，系统健康、无紧急问题。剩余 2 项低优先级收尾。

## 红线
- 唯一控制目标=上海空调伴侣 DID 2056557176 (192.168.71.43)，本地 miio。禁云 API。
- 本溪空调 zhimi.aircondition.ma3 (DID 90466860) 绝对禁区；91063311 离线机勿碰。
- 不动 Hermes cron、~/.hermes/scripts/ac_watch_wrapper.py、gateway、模型/provider 配置。
- 改代码后必须 py_compile → selftest → 手动触发新分支实跑 → commit。

## 待办（2 项）

### 1. test_learn_ratchet 断言更新（测试过期，非产品 bug）
文件：D:/work/ac-advisor/test_learn_ratchet.py
现象：3 条 FAIL
- `[FAIL] 失败 → 偏移 0 → -1 got=0`
- `[FAIL] 成功且偏移 -2 → 回收到 -1（不再永久停在边界） got=-2`
- `[FAIL] 成功且偏移 +2 → 回收到 +1 got=0.5`

根因：测试断言的是 v8.23 时代行为（失败时偏移-1、负偏移回收到-1），但代码已迭代到 v8.35：
- 负偏移被 clamp 到 0（`max(0, min(2, cur_adj - 1))`，ac_advisor.py L221）
- 预算不足时-0.5（L236）
- 超预算时+0.5（L234）

修法：更新断言对齐当前行为，或标记 `@pytest.mark.skip` 注明 legacy。

### 2. 7 个只读 state 键清理（低优先级）
文件：D:/work/ac-advisor/ac_state.json + ac_watch.py/ac_advisor.py
只读不写的键：`_night_comp_starts` `last_dehumid_adjust_at` `last_outdoor_temp` `note` `prev_outdoor_temp` `src` `last_on_at`（ac_advisor.py L? 有 1 次写）

任务：
- 确认每个键是否仍有业务含义（如 `last_on_at` 可能是日志展示用）
- 无用的从 ac_state.json 移除 + 代码里删掉对应初始化
- 有用的补写操作或加注释说明只读原因

## 验证
- `python ac_watch.py --selftest` 全 PASS
- `for t in test_*.py; do python $t; done` 全绿（豁免：test_cloud 环境缺 miio.miotcloud、test_humidity 设备离线）
- wrapper 实跑：`~/.hermes/scripts/ac_watch_wrapper.py`（预期静默或真实动作行）

## 产出
- commit + push（被墙记录原因）
- 报告写到 D:/work/ac-acadvisor/_audit_report_omp.md（可选，stdout 摘要即可）
