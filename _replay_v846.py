# -*- coding: utf-8 -*-
# 分支级重放验证: v8.46 幻象锚点门控修复(修复①②③)
import sys, io, json, importlib, types
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'D:\work\ac-advisor')
import ac_advisor as A
A.AC_SOCKET = 'on'  # 模拟伴侣 socket=on 运行时状态

results = []
def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))

def base_state():
    return {'mode': 'off', 'manual_on_at': None, 'last_off_at': '2026-09-04T06:00:00'}

# ── 场景A: 单帧 >50W (昨晨幻象主路径) → 观察一拍, 不打锚点不入账 ──
s = base_state()
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=200)
check('A1 单帧>50W不打锚点', s.get('manual_on_at') is None and s.get('mode') == 'off', str(s.get('manual_on_at')))
check('A2 打观察标记', s.get('_on_flip_high_at') == '2026-09-04T09:00:00' and s.get('_phantom_gate_at') == '2026-09-04T09:00:00')

# ── 场景B: 连续两 tick >50W (2min间隔) → 真手动, 锚点+立即学习 ──
s = base_state()
log = s.setdefault('user_pref', {}).setdefault('manual_on_log', [])
s['rh_history'] = [['2026-09-04T09:00:00', 55]]
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=200)
A.reconcile_state(s, '2026-09-04T09:02:00', load_power=200)
check('B1 两tick确认打锚点', s.get('manual_on_at') == '2026-09-04T09:02:00' and s.get('mode') == 'cooling')
check('B2 立即学习入账', len(log) == 1 and log[0]['ts'] == '2026-09-04T09:02:00')

# ── 场景C: >300W 压缩机级单帧铁证 → 立即锚点+学习 ──
s = base_state()
log = s.setdefault('user_pref', {}).setdefault('manual_on_log', [])
s['rh_history'] = [['2026-09-04T09:00:00', 55]]
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=1050)
check('C1 >300W立即锚点', s.get('manual_on_at') == '2026-09-04T09:00:00' and s.get('mode') == 'cooling')
check('C2 立即学习', len(log) == 1)

# ── 场景D: 断表窗口 16-24min 幻象周期 (今晨泄漏路径) → 35min 窗口拦截 ──
s = base_state()
A.reconcile_state(s, '2026-09-04T07:48:00', load_power=None)   # 首个翻转(不可区分)→锚点+pending
check('D1 断表首翻打锚点', s.get('manual_on_at') == '2026-09-04T07:48:00')
check('D2 学习挂起', '_pending_manual_on_learn' in s)
# 16min 后幻象翻转(今晨实际间隔) → 35min 窗口应拦
s['mode'] = 'off'  # 模拟伴侣回翻
A.reconcile_state(s, '2026-09-04T08:04:00', load_power=None)
check('D3 16min后翻转被震荡窗口拦截(静默对账)', s.get('manual_on_at') == '2026-09-04T07:48:00' or s.get('_phantom_gate_at') == '2026-09-04T08:04:00', json.dumps({k: s.get(k) for k in ('manual_on_at', '_phantom_gate_at')}, ensure_ascii=False))
# 24min 后再翻上(今晨间隔) → 断表+震荡窗口(35min内) → 不打新锚点
A.reconcile_state(s, '2026-09-04T08:28:00', load_power=None)
check('D4 24min后幻象开翻转被拦', s.get('_phantom_gate_at') == '2026-09-04T08:28:00' and s.get('mode') != 'cooling')

# ── 场景E: 断表锚点 + 10分钟 kWh 零增量 → 学习不入账(修复③核心) ──
s = base_state()
log = s.setdefault('user_pref', {}).setdefault('manual_on_log', [])
s['rh_history'] = [['2026-09-04T09:00:00', 55]]
s['estimated_kwh'] = 4.94
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=None)  # 锚点+pending
n0 = len(log)
check('E1 锚点已打学习未入账', s.get('manual_on_at') == '2026-09-04T09:00:00' and n0 == 0)
s['estimated_kwh'] = 4.94  # 幻象: 10分钟零功耗
A.reconcile_state(s, '2026-09-04T09:10:00', load_power=None)  # 触发到期验证
check('E2 零功耗→学习不入账', len(log) == 0 and '_pending_manual_on_learn' not in s)

# ── 场景F: 断表锚点 + 10分钟 kWh 有增量 → 保守入账 ──
s = base_state()
log = s.setdefault('user_pref', {}).setdefault('manual_on_log', [])
s['rh_history'] = [['2026-09-04T09:00:00', 55]]
s['estimated_kwh'] = 4.94
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=None)
s['estimated_kwh'] = 4.97  # 压缩机真实出力
A.reconcile_state(s, '2026-09-04T09:10:00', load_power=None)
check('F 有功耗→学习入账', len(log) == 1 and log[0]['ts'] == '2026-09-04T09:00:00')

# ── 场景G: 电量不可测(None)→保守入账(不误伤) ──
s = base_state()
log = s.setdefault('user_pref', {}).setdefault('manual_on_log', [])
s['rh_history'] = [['2026-09-04T09:00:00', 55]]
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=None)
A.reconcile_state(s, '2026-09-04T09:10:00', load_power=None)
check('G 电量缺失→保守入账', len(log) == 1)

# ── 场景H: 两tick确认链被低功率读数打断 → 证据链清零 ──
s = base_state()
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=200)
A.reconcile_state(s, '2026-09-04T09:02:00', load_power=30)   # 低功率打断
check('H1 低功率打断不打锚点', s.get('manual_on_at') is None and s.get('mode') == 'off')
check('H2 证据链已清', '_on_flip_high_at' not in s)
A.reconcile_state(s, '2026-09-04T09:04:00', load_power=200)  # 重新起链: 观察一拍
check('H3 重起链仍需观察一拍', s.get('manual_on_at') is None and s.get('_on_flip_high_at') == '2026-09-04T09:04:00')

# ── 场景I: ≤50W 待机幻象 → 完全不动(回归 v8.43 行为) ──
s = base_state()
A.reconcile_state(s, '2026-09-04T09:00:00', load_power=1)
check('I 待机幻象不动state', s.get('mode') == 'off' and s.get('manual_on_at') is None and s.get('_phantom_gate_at') == '2026-09-04T09:00:00')

fails = [r for r in results if not r[1]]
for name, ok, d in results:
    print(('PASS' if ok else 'FAIL'), name, ('| ' + d if d and not ok else ''))
print('== replay: %d/%d ==' % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)


