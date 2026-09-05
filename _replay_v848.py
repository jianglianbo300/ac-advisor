# -*- coding: utf-8 -*-
# 分支级重放验证: v8.48 幻象锚点门控补丁(09-04晚审计 P0 三发)
#   修复① 学习喂入统一延迟验证(功率铁证路径不再立即入账)
#   修复② 震荡检测扩容到全部路径
#   修复③ 观察一拍不再打 _phantom_gate_at(不误拦双tick证据链)
# 本脚本取代 _replay_v846.py 的 A2 断言口径(观察拍不再打 _phantom_gate_at)。
import sys, io, json, importlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'D:\work\ac-advisor')
import ac_advisor as A
A.AC_SOCKET = 'on'

results = []
def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))

def base_state():
    s = {'mode': 'off', 'manual_on_at': None, 'last_off_at': '2026-09-04T06:00:00'}
    s.setdefault('user_pref', {}).setdefault('manual_on_log', [])
    s.setdefault('rh_history', [['2026-09-04T09:00:00', 55]])
    return s

# ── v8.46 回归: 场景A 单帧 50-300W → 观察一拍(仅 _on_flip_high_at) ──
s = base_state()
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=200)
check('A1 单帧>50W不打锚点', s.get('manual_on_at') is None and s.get('mode') == 'off')
check('A2 观察拍只打_on_flip_high_at(v8.48③)', s.get('_on_flip_high_at') == '2026-09-04T09:00:00' and '_phantom_gate_at' not in s)

# ── v8.46 回归: 场景B 两tick确认 → 锚点 + 学习挂起(v8.48① 变更) ──
s = base_state()
log = s['user_pref']['manual_on_log']
s['estimated_kwh'] = 100.0
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=200)
A.reconcile_state(s, '2026-09-04T09:02:00', load_power=200)
check('B1 两tick确认打锚点', s.get('manual_on_at') == '2026-09-04T09:02:00' and s.get('mode') == 'cooling')
check('B2 学习挂起不立即入账(v8.48①)', len(log) == 0 and s.get('_pending_manual_on_learn', {}).get('ts') == '2026-09-04T09:02:00')
check('B3 观察拍不自设gate→证据链可完成(v8.48③)', s.get('_on_flip_high_at') is None)

# ── v8.46 回归: 场景C >300W 单帧 → 立即锚点 + 学习挂起 ──
s = base_state()
log = s['user_pref']['manual_on_log']
s['estimated_kwh'] = 100.0
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=1050)
check('C1 >300W立即锚点', s.get('manual_on_at') == '2026-09-04T09:00:00' and s.get('mode') == 'cooling')
check('C2 学习挂起', len(log) == 0 and '_pending_manual_on_learn' in s)

# ── v8.48 新增: 场景J 今晚复发精确复刻——铁证锚点 + kWh 冻结 → 学习永不入账 ──
s = base_state()
log = s['user_pref']['manual_on_log']
s['estimated_kwh'] = 6.19  # kWh今全天冻结(今晚实况)
A.reconcile_state(s, '2026-09-04T18:38:00', load_power=1050)  # 幻象拿到高功率瞬时值
check('J1 铁证路径锚点已打(首翻不可区分)', s.get('manual_on_at') == '2026-09-04T18:38:00')
check('J2 学习挂起未入账', len(log) == 0 and s.get('_pending_manual_on_learn', {}).get('kwh') == 6.19)
A.reconcile_state(s, '2026-09-04T18:48:00', load_power=1050)  # 10min 后 kWh 仍 6.19
check('J3 零功耗→学习不入账且pending清除', len(log) == 0 and '_pending_manual_on_learn' not in s)
A.reconcile_state(s, '2026-09-04T18:58:00', load_power=1050)  # 迟到核验也无副作用
check('J4 迟到核验无重复入账', len(log) == 0)

# ── v8.48 新增: 场景K 功率锚点 + kWh 真实增量 → 10min 后入账 ──
s = base_state()
log = s['user_pref']['manual_on_log']
s['estimated_kwh'] = 6.19
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=1050)
s['estimated_kwh'] = 6.38  # 真压缩机 ~1100W×10min
A.reconcile_state(s, '2026-09-04T09:10:00', load_power=1050)
check('K 真实功耗→学习入账', len(log) == 1 and log[0]['ts'] == '2026-09-04T09:00:00' and '_pending_manual_on_learn' not in s)

# ── v8.48 新增: 场景L 今晚 19:42/20:04 场景——窗口内翻转带高功率读数 → 震荡拦截 ──
s = base_state()
log = s['user_pref']['manual_on_log']
s['estimated_kwh'] = 6.19
A.reconcile_state(s, '2026-09-04T19:18:00', load_power=1050)  # 锚点(首翻)
n_before = len(log)
s['mode'] = 'off'  # 伴侣回翻
A.reconcile_state(s, '2026-09-04T19:42:00', load_power=1050)  # 24min 后再翻上, 仍带"铁证"读数
check('L1 窗口内高功率翻转被震荡拦截(v8.48②)', s.get('manual_on_at') == '2026-09-04T19:18:00' and s.get('mode') == 'off', json.dumps({k: s.get(k) for k in ('manual_on_at', '_phantom_gate_at')}, ensure_ascii=False))
check('L2 不产生新学习', len(log) == n_before)

# ── v8.48 新增: 场景M 窗口(35min)过后 → 不误伤长时间间隔的真翻转 ──
s = base_state()
s['estimated_kwh'] = 100.0
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=None)   # 锚点
s['mode'] = 'off'
A.reconcile_state(s, '2026-09-04T09:40:00', load_power=1050)   # 40min 后(>35min 窗口)
check('M 窗口外翻转允许新锚点', s.get('manual_on_at') == '2026-09-04T09:40:00' and s.get('mode') == 'cooling')

# ── v8.46 回归: 场景D 断表幻象周期 35min 窗口拦截 ──
s = base_state()
A.reconcile_state(s, '2026-09-04T07:48:00', load_power=None)
check('D1 断表首翻打锚点', s.get('manual_on_at') == '2026-09-04T07:48:00')
check('D2 学习挂起', '_pending_manual_on_learn' in s)
s['mode'] = 'off'
A.reconcile_state(s, '2026-09-04T08:04:00', load_power=None)
check('D3 16min后翻转被拦', s.get('_phantom_gate_at') == '2026-09-04T08:04:00' and s.get('mode') != 'cooling')
A.reconcile_state(s, '2026-09-04T08:28:00', load_power=None)
check('D4 24min后幻象开翻转被拦', s.get('_phantom_gate_at') == '2026-09-04T08:28:00' and s.get('mode') != 'cooling')

# ── v8.46 回归: 场景E/F/G 断表延迟学习三分支 ──
s = base_state(); log = s['user_pref']['manual_on_log']; s['estimated_kwh'] = 4.94
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=None)
s['estimated_kwh'] = 4.94
A.reconcile_state(s, '2026-09-04T09:10:00', load_power=None)
check('E 零功耗→不入账', len(log) == 0 and '_pending_manual_on_learn' not in s)

s = base_state(); log = s['user_pref']['manual_on_log']; s['estimated_kwh'] = 4.94
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=None)
s['estimated_kwh'] = 4.97
A.reconcile_state(s, '2026-09-04T09:10:00', load_power=None)
check('F 有功耗→入账', len(log) == 1 and log[0]['ts'] == '2026-09-04T09:00:00')

s = base_state(); log = s['user_pref']['manual_on_log']
s['estimated_kwh'] = None
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=None)
A.reconcile_state(s, '2026-09-04T09:10:00', load_power=None)
# v8.50d 口径变更 (Astra验收三#2/#4): 电量缺失 = 无法核验 = 不喂样本（宁缺毋滥）。
# 原 v8.46 语义「电量不可测保守入账」与 v8.48 确立的「kWh 是唯一不可伪造量」
# 矛盾——电量缺失直接通过会把幻象样本放进学习。真手动事件在电量恢复后由
# 后续事件继续学习，损失有限。本条取代 v848 原 G 断言。
check('G 电量缺失→不入账 (v8.50d)', len(log) == 0)

# ── v8.46 回归: 场景H 低功率打断证据链 ──
s = base_state()
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=200)
A.reconcile_state(s, '2026-09-04T09:02:00', load_power=30)
check('H1 低功率打断不打锚点', s.get('manual_on_at') is None and s.get('mode') == 'off')
check('H2 证据链已清', '_on_flip_high_at' not in s)

# ── v8.46 回归: 场景I ≤50W 待机幻象不动 ──
s = base_state()
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=1)
check('I 待机幻象不动state', s.get('mode') == 'off' and s.get('manual_on_at') is None and s.get('_phantom_gate_at') == '2026-09-04T09:00:00')

fails = [r for r in results if not r[1]]
for name, ok, d in results:
    print(('PASS' if ok else 'FAIL'), name, ('| ' + d if d and not ok else ''))
print('== replay v8.48: %d/%d ==' % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)
