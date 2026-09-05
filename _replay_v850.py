# -*- coding: utf-8 -*-
# 分支级重放验证: v8.50 Astra 外审 15 项修复（2026-09-05）
#   pass2#1 find_pre_cool_window 四元组/绝对小时
#   pass2#2 幻象关翻转 kWh 验证（_run_start_kwh 佐证）
#   pass2#3 预算护栏优先（防同轮抵消锁死）
#   pass2#4 延迟学习用锚点快照（mode/rh 不错时点）
#   pass2#5 单帧功率≠成功证明（kWh 增量佐证）
#   pass2#6 天气 ISO 带时区不静默失效 + 空序列保护
#   pass2#7 low_rh_manual 只统计手动开机
#   pass1#2 绝对下限前移到 fan_only 之前
#   pass1#4 vent_gate 高温豁免 + 降雨否决
#   pass1#6/#8 未执行决策不计启动数（executed 标记）
#   pass1#3 _effective_running 对账后不重建（socket 需功率佐证）
import sys, io, json, os, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'D:\work\ac-advisor')
import ac_advisor as A
import ac_watch as W

results = []
def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))

_tmp = tempfile.mkdtemp()
_orig_sd, _orig_learn = A.SCRIPT_DIR, A.LEARN_FILE
A.SCRIPT_DIR = _tmp
A.LEARN_FILE = os.path.join(_tmp, 'ac_learned.json')
W.A.SCRIPT_DIR = _tmp
W.A.LEARN_FILE = A.LEARN_FILE
A.save_learned({'adjusted_thresholds': {}, 'decision_log': []})

def base_state():
    s = {'mode': 'off', 'manual_on_at': None, 'last_off_at': '2026-09-05T06:00:00'}
    s.setdefault('user_pref', {}).setdefault('manual_on_log', [])
    s.setdefault('rh_history', [['2026-09-05T09:00:00', 55]])
    return s

# ── V1/V2: find_pre_cool_window 四元组 + 绝对小时（pass2#1）──
sched = [(0, 'off', 0.0, 30.0), (6, 'cool', 1.0, 29.5), (7, 'cool', 1.1, 29.0)]
try:
    r = A.find_pre_cool_window(sched, 10)
    ok1 = True
except ValueError:
    ok1, r = False, None
check('V1 四元组解包不炸', ok1)
check('V2 预冷窗口产出（绝对小时语义）', ok1 and r is not None and r[0] is not None)

# ── V3-V7: 幻象关翻转 kWh 验证（pass2#2）──
# 幻象序列: on+1050W 锚点 → off+1W, kWh 冻结 → 静默对账不打锚点不喂学习
A.AC_SOCKET = 'on'
s = base_state(); log = s['user_pref']['manual_on_log']; s['estimated_kwh'] = 6.19
A.reconcile_state(s, '2026-09-05T09:00:00', load_power=1050)
A.AC_SOCKET = 'off'
s['_prev_power'] = 1050
A.reconcile_state(s, '2026-09-05T09:02:00', load_power=1)
check('V3 幻象关翻转不打manual_off锚点', s.get('manual_off_at') is None)
check('V4 幻象关翻转不喂学习', len(log) == 0)
check('V5 幻象关翻转静默对账off', s.get('mode') == 'off')

# 真手动: 运行期 kWh 有增量 → 关翻转正常打锚点+学习
A.AC_SOCKET = 'on'
s = base_state(); log = s['user_pref']['manual_on_log']; s['estimated_kwh'] = 6.19
A.reconcile_state(s, '2026-09-05T09:00:00', load_power=1050)
s['estimated_kwh'] = 6.39  # 真运行 ~20min
A.AC_SOCKET = 'off'; s['_prev_power'] = 1050
A.reconcile_state(s, '2026-09-05T09:20:00', load_power=1)
check('V6 真手动关仍打锚点', s.get('manual_off_at') == '2026-09-05T09:20:00')
# 说明: 开样本(延迟核验 20min 到期)与关样本都会入账——开一条 mode=cooling、
# 关一条 mode=off, 各归各位; 断言存在 mode=off 条目而非 len==1。
check(
    'V7 真手动关学习入账',
    any(m.get('mode') == 'off' for m in log) and len(log) >= 1,
)

# ── V8/V9: 延迟学习锚点快照（pass2#4）──
A.AC_SOCKET = 'on'
s = base_state(); log = s['user_pref']['manual_on_log']
s['rh_history'] = [['2026-09-05T09:00:00', 70]]
s['estimated_kwh'] = 6.19
A.reconcile_state(s, '2026-09-05T09:00:00', load_power=1050)
s['estimated_kwh'] = 6.39
s['rh_history'].append(['2026-09-05T09:10:00', 55])
A.reconcile_state(s, '2026-09-05T09:10:00', load_power=1050)
check('V8 快照用锚点时刻RH(70)', len(log) == 1 and log[0]['rh'] == 70)
check('V9 快照mode=cooling', len(log) == 1 and log[0]['mode'] == 'cooling')

# ── V10/V11: 单帧功率≠成功（pass2#5）──
from datetime import datetime, timedelta
t0 = (datetime.now() - timedelta(minutes=45)).isoformat()
def eval_state(kwh_now):
    return {'last_temp': 28.0, 'last_hum': 65.0, 'estimated_kwh': kwh_now,
            '_daily_kwh': 8.0,  # 中性: 不触发预算加减分支(0 会触发"远低于预算→减偏移")
            '_kwh_by_price_band': {'peak': 0.0, 'valley': 0.0, 'date': '2026-09-05'},
            '_budget_prediction': {'date': '2026-09-05', 'predicted_kwh': 8.0}}
A.save_learned({'adjusted_thresholds': {'temp_cooling': 1.0}, 'decision_log': [
    {'time': t0, 'action': 'cooling', 'pre_temp': 28.0, 'pre_hum': 65.0,
     'evaluated': False, 'power_at_decision': 1050, 'kwh_at_decision': 9.0}]})
A.evaluate_and_learn(eval_state(9.0), '2026-09-05T12:00:00')
check('V10 幻象功率+kWh零增量不算成功(偏移不动)', A.load_learned()['adjusted_thresholds']['temp_cooling'] == 1.0)
A.save_learned({'adjusted_thresholds': {'temp_cooling': 1.0}, 'decision_log': [
    {'time': t0, 'action': 'cooling', 'pre_temp': 28.0, 'pre_hum': 65.0,
     'evaluated': False, 'power_at_decision': 1050, 'kwh_at_decision': 9.0}]})
A.evaluate_and_learn(eval_state(9.4), '2026-09-05T12:00:00')
check('V11 kWh有增量→成功回落', A.load_learned()['adjusted_thresholds']['temp_cooling'] == 0.0)

# ── V12/V13: 时区天气不静默失效（pass2#6）──
tz_times = [(datetime.now().astimezone() + timedelta(hours=i)).isoformat() for i in (8, 9, 10)]
wx_tz = {'hourly': {'time': tz_times, 'relative_humidity_2m': [75, 80, 85]}}
ok2, tg, _ = A.predict_dehumidify_need(wx_tz, 60, 30)
check('V12 带时区ISO正常判预除湿', ok2 is True and tg == 55)
wx_none = {'hourly': {'time': tz_times, 'relative_humidity_2m': [None, None, None]}}
try:
    ok3, _, _ = A.predict_dehumidify_need(wx_none, 60, 30)
    ok3 = ok3 is False
except Exception:
    ok3 = False
check('V13 湿度全None不炸', ok3)

# ── V14: low_rh 只统计手动开机（pass2#7）──
s = base_state()
s['user_pref']['manual_on_log'] = [{'ts': 'x1', 'rh': 62, 'mode': 'off'},
                                   {'ts': 'x2', 'rh': 63, 'mode': 'off'},
                                   {'ts': 'x3', 'rh': 64, 'mode': 'off'}]
A._learn_from_manual(s, '2026-09-05T10:00:00')
check('V14 关机样本不压低除湿阈值', s['user_pref'].get('hum_threshold') is None)

# ── V15: 预算护栏优先净回落（pass2#3）──
class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 5, 14, 0, 0)
_orig_dt = A.datetime
A.datetime = FakeDT
A.save_learned({'adjusted_thresholds': {'temp_cooling': 2.5}, 'decision_log': []})
st = {'last_temp': 29.0, 'estimated_kwh': 5.0, '_daily_kwh': 20.0,
      '_kwh_by_price_band': {'peak': 0.0, 'valley': 0.0, 'date': '2026-09-05'},
      '_budget_prediction': {'date': '2026-09-05', 'predicted_kwh': 8.0},
      'last_off_at': (datetime(2026, 9, 5, 13, 30, 0)).isoformat()}
A.evaluate_and_learn(st, '2026-09-05T14:00:00')
A.evaluate_and_learn(st, '2026-09-05T14:02:00')
adj15 = A.load_learned()['adjusted_thresholds']['temp_cooling']
check('V15 护栏优先净回落', adj15 < 2.5)
A.datetime = _orig_dt

# ── V16: fan_only 低温逃生门（pass1#2）──
r = W.decide(23.0, 70, True, 30, 90, False, 'fan_only', 15, None, 26,
             None, None, None, None, 20, None)
check('V16 fan_only低于下限逃生门关机', r[0] == 'off' and '绝对下限' in (r[2] or ''))

# ── V17-V19: vent_gate 高温豁免+降雨否决（pass1#4）──
check('V17 高温制冷不拦截', W.vent_gate_decision(14, 60, 28.0, 0, 18.0, 20.0) is False)
check('V18 降雨否决通风', W.vent_gate_decision(14, 60, 26.0, 80, 18.0, 20.0) is False)
check('V19 干爽低温仍拦截', W.vent_gate_decision(14, 60, 25.0, 0, 16.0, 20.0) is True)

# ── V20: 未执行决策不计启动数（pass1#6/#8）──
t_now = datetime.now()
def ts_ago(m):
    return (t_now - timedelta(minutes=m)).isoformat()
A.save_learned({'adjusted_thresholds': {}, 'decision_log': [
    {'time': ts_ago(5), 'action': 'cooling', 'executed': True},
    {'time': ts_ago(10), 'action': 'cooling', 'executed': False},
    {'time': ts_ago(15), 'action': 'cooling', 'executed': False},
]})
try:
    n_starts = W._starts_in_last_hour()
    check('V20 未执行决策不计启动数', n_starts == 1)
except AttributeError as e:
    check('V20 未执行决策不计启动数', False, str(e))

# ── V21-V23: _effective_running（pass1#3 + v8.50b H2门控）──
try:
    check('V21 幻象socket on+1W不判运行', W._effective_running({'mode': 'off'}, 'on', 1) is False)
    # v8.50b: 高功率兜底需 H2 确认（_unmanaged_run_since 存在）——孤立幻象高功率帧
    # 不再判运行；H2 双 tick 确认后才算。
    check('V22a 孤立高功率帧不判运行(无H2确认)', W._effective_running({'mode': 'off'}, 'on', 1050) is False)
    check('V22b H2确认窗口高功率判运行', W._effective_running({'mode': 'off', '_unmanaged_run_since': '2026-09-05T09:00:00'}, 'on', 1050) is True)
    check('V23 mode=cooling判运行', W._effective_running({'mode': 'cooling'}, 'off', 1) is True)
except AttributeError as e:
    check('V21 幻象socket不判运行', False, str(e))
    check('V22 H2窗口判运行', False, str(e))
    check('V23 mode判运行', False, str(e))

# ── V25: 未执行条目不污染启动状态机（v8.50b，Astra验收#8）──
# 场景①漏计复现: cooling-False → cooling-True, 应为 1 次启动
A.save_learned({'adjusted_thresholds': {}, 'decision_log': [
    {'time': ts_ago(10), 'action': 'cooling', 'executed': False},
    {'time': ts_ago(5), 'action': 'cooling', 'executed': True},
]})
check('V25a 未执行前置不吞真实启动', W._starts_in_last_hour() == 1)
# 场景②多计复现: cooling-True → off-False → cooling-True, 应为 1 次
A.save_learned({'adjusted_thresholds': {}, 'decision_log': [
    {'time': ts_ago(15), 'action': 'cooling', 'executed': True},
    {'time': ts_ago(10), 'action': 'off', 'executed': False},
    {'time': ts_ago(5), 'action': 'cooling', 'executed': True},
]})
check('V25b 未执行关机不产生假启动', W._starts_in_last_hour() == 1)

# ── V26: 快照rh不依赖当前rh_history（v8.50b，Astra验收#12）──
s = base_state()
s['user_pref']['manual_on_log'] = []
s['rh_history'] = []  # 当前历史为空
s['user_pref'] = {'manual_on_log': []}
A._learn_from_manual(s, '2026-09-05T11:00:00', rh=62, mode='cooling')
log26 = s['user_pref']['manual_on_log']
check('V26 空历史+显式快照仍入账', len(log26) == 1 and log26[0]['rh'] == 62 and log26[0]['mode'] == 'cooling')

# ── V27: 窗口前已运行 → 窗口内首个cooling不算新启动（v8.50c，Astra验收二#8 P2）──
t_far = ts_ago(90)  # 窗口外
t_60 = ts_ago(55)   # 窗口内
A.save_learned({'adjusted_thresholds': {}, 'decision_log': [
    {'time': t_far, 'action': 'cooling', 'executed': True},   # 90min前已运行
    {'time': t_60, 'action': 'cooling', 'executed': True},    # 窗口内继续运行(承接)
    {'time': ts_ago(5), 'action': 'cooling', 'executed': True},
]})
check('V27 窗口前已运行不误计启动', W._starts_in_last_hour() == 0)
# 窗口前已运行 → off → cooling: 计 1
A.save_learned({'adjusted_thresholds': {}, 'decision_log': [
    {'time': t_far, 'action': 'cooling', 'executed': True},
    {'time': t_60, 'action': 'off', 'executed': True},
    {'time': ts_ago(5), 'action': 'cooling', 'executed': True},
]})
check('V27b 窗口前运行+关+开=1次', W._starts_in_last_hour() == 1)

# ── V28: pending rh快照缺失 → 跳过学习（v8.50c，Astra验收二#12）──
A.AC_SOCKET = 'on'
s = base_state(); log = s['user_pref']['manual_on_log']
s['rh_history'] = []   # 锚点时刻无rh快照
s['estimated_kwh'] = 6.19
A.reconcile_state(s, '2026-09-05T09:00:00', load_power=1050)
s['estimated_kwh'] = 6.39  # 电量佐证成立
A.reconcile_state(s, '2026-09-05T09:10:00', load_power=1050)
check('V28 缺rh快照不喂样本', len(log) == 0)

# ── V29: 无kwh快照的旧日志条目+单帧高功率 → 不可评价（v8.50c，Astra验收二#13）──
A.save_learned({'adjusted_thresholds': {'temp_cooling': 1.0}, 'decision_log': [
    {'time': t0, 'action': 'cooling', 'pre_temp': 28.0, 'pre_hum': 65.0,
     'evaluated': False, 'power_at_decision': 1050}  # 无kwh_at_decision字段(旧条目)
]})
A.evaluate_and_learn(eval_state(9.0), '2026-09-05T12:00:00')
_dl29 = A.load_learned()['decision_log']
check('V29 缺kwh快照标不可评价且偏移不动',
      _dl29[0].get('eval_note') == 'insufficient_evidence_no_kwh'
      and A.load_learned()['adjusted_thresholds']['temp_cooling'] == 1.0)

# ── V24: log_decision executed 标记（pass1#6/#8）──
try:
    A.save_learned({'adjusted_thresholds': {}, 'decision_log': []})
    A.log_decision({}, 'cooling', 27, 60, ts_ago(1), reason='t', executed=False)
    _dl = A.load_learned()['decision_log']
    check('V24 log_decision记executed', len(_dl) == 1 and _dl[0].get('executed') is False)
except TypeError as e:
    check('V24 log_decision记executed', False, str(e))

fails = [r for r in results if not r[1]]
for name, ok, d in results:
    print(('PASS' if ok else 'FAIL'), name, ('| ' + d if d and not ok else ''))
print('== replay v8.50: %d/%d ==' % (len(results) - len(fails), len(results)))
A.SCRIPT_DIR, A.LEARN_FILE = _orig_sd, _orig_learn
sys.exit(1 if fails else 0)
