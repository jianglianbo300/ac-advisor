#!/usr/bin/env python3
"""v8.23 持续高温判据 — 回归测试。

用户反馈序列（2026-08-19 凌晨）：
  27.0°C 时："现在热了是不没给我开空调啊"
  降到 26.0°C 后："其实刚才也不太热"

两句不矛盾，它们卡出了舒适边界：27 = 有点热但能忍，26 = 不热。
所以该改的不是启动线（回到 28 只覆盖 0.7% 历史采样 = 变回"基本不自动开"，
正是他最初的抱怨），而是**加时间维度**：碰一下 27 不算热，持续 27 才算。

数据依据（2384 次采样）：≥27°C 共 54 段，13 段短于 10min（开门/走动/日照的
短暂触碰），真热中位持续 18min、最长 182min → 10min 门槛滤噪声且不漏真热。

设计要点：
  - sustained=None（历史数据不足）→ 放行，fail-open，不因缺数据不制冷
  - 室温 >= SUSTAIN_URGENT_T(29) → 明确过热，不等持续时长
  - 高湿闷热走除湿分支，不受持续时长约束（闷是即时体感）
"""
import sys
from datetime import datetime, timedelta

import ac_advisor as A
import ac_watch as W

# v8.33 审计修复：本文件用例只验温度/持续/AH 分支逻辑，不测峰谷电策略。
# 峰电封锁(temp_cooling+1)是按真实钟点生效的（20-22点跑测试必挂），
# 锁定谷电价使结果与运行时段解耦。
A.current_price = lambda: 0.307

# 2026-08-28 审计：隔离真实学习数据（同 test_day_short_cycle.py 注释）——
# 近 1h 真实启动记录会触发启停上限，启动类用例假红。
A.load_learned = lambda: {"adjusted_thresholds": {"temp_cooling": 0}, "decision_log": []}


class R:
    def __init__(self):
        self.ok = self.fail = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            print(f"  [PASS] {name}")
        else:
            self.fail += 1
            print(f"  [FAIL] {name} {detail}")


R_ = R()
NOW = datetime.now()


def hist(*pairs):
    """pairs = (分钟前, 温度)"""
    return {"temp_history": [[(NOW - timedelta(minutes=m)).isoformat(), t]
                             for m, t in pairs]}


print("=" * 70)
print("v8.23 持续高温判据 — 回归测试")
print("=" * 70)

# ── 1. sustained_above 判定 ──
print("\n[1] 持续判定函数")
R_.check("常量 SUSTAIN_MIN 存在", hasattr(W, "SUSTAIN_MIN"))
R_.check("常量 SUSTAIN_URGENT_T 存在", hasattr(W, "SUSTAIN_URGENT_T"))

r = W.sustained_above(hist((2, 26.0), (0, 27.0)), 27.0, 10, NOW.isoformat())
R_.check("刚碰到线（跨度仅 2min）→ None（数据不足）", r is None, f"got={r}")

r = W.sustained_above(
    hist((14, 27.0), (12, 27.0), (10, 27.0), (8, 27.0),
         (6, 27.0), (4, 27.0), (2, 27.0), (0, 27.0)),
    27.0, 10, NOW.isoformat())
R_.check("持续 14min 全在线上 → True", r is True, f"got={r}")

r = W.sustained_above(
    hist((14, 27.0), (12, 27.0), (8, 26.0), (6, 27.0),
         (4, 27.0), (2, 27.0), (0, 27.0)),
    27.0, 10, NOW.isoformat())
R_.check("窗口内 8min 前掉到 26 → False", r is False, f"got={r}")

r = W.sustained_above({"temp_history": []}, 27.0, 10, NOW.isoformat())
R_.check("空历史 → None", r is None, f"got={r}")

r = W.sustained_above({}, 27.0, 10, NOW.isoformat())
R_.check("无 temp_history 字段 → None", r is None, f"got={r}")

# 窗口外的低温不影响判定（只看最近 need_min）
r = W.sustained_above(
    hist((30, 22.0), (14, 27.0), (12, 27.0), (10, 27.0),
         (8, 27.0), (6, 27.0), (4, 27.0), (2, 27.0), (0, 27.0)),
    27.0, 10, NOW.isoformat())
R_.check("窗口外(30min前)的低温不影响 → True", r is True, f"got={r}")

# ── 2. 夜间启动受持续判据约束 ──
print("\n[2] 夜间启动（用户实际场景）")


def night(temp, sustained, hum=50, ah=13.0):
    return W.decide(temp=temp, hum=hum, running=False, since_on=None, since_off=99,
                    is_night=True, compressor=None, current_target=26, ah=ah,
                    compressor_run_min=None, night_comp_starts=[], sustained=sustained)


# v8.33 审计注：d464391(v8.31) 有意移除夜间 sustained 判据（夜间短循环已由
# MIN_OFF 15min + 每小时启停上限防护；sustained 过滤会把缓慢夜间升温卡死在 27°C，
# 见 ac_watch.py decide() 内注释）。本组断言随新语义更新：
# 「短暂触碰」与「持续」现在都放行，未持续/数据不足不再有区别。
m, t, reason = night(27.0, False)
R_.check("27°C 未持续 → 开机（v8.31 移除夜间持续闸门后的新语义）", m == "cooling", f"got={m} {reason}")
m, t, reason = night(27.0, True)
R_.check("27°C 持续 10min → 开机（语义不变）", m == "cooling", f"got={m} {reason}")
R_.check(f"目标 {t}°C 低于室温 ≥2°C", t is not None and 27.0 - t >= 2, f"target={t}")
m, _, _ = night(27.0, None)
R_.check("数据不足 → 放行开机（fail-open 不因缺数据不制冷）", m == "cooling", f"got={m}")

# ── 3. 紧急过热不等持续时长 ──
print("\n[3] 明确过热的紧急放行")
m, _, reason = night(W.SUSTAIN_URGENT_T, False)
R_.check(f"{W.SUSTAIN_URGENT_T}°C + 未持续 → 仍开机（明确过热）",
         m == "cooling", f"got={m} {reason}")
m, _, _ = night(W.SUSTAIN_URGENT_T - 0.1, False)
R_.check(f"{W.SUSTAIN_URGENT_T - 0.1}°C + 未持续 → 开机（夜间无持续闸门，v8.31 起）",
         m == "cooling", f"got={m}")

# ── 4. 白天同样受约束 ──
print("\n[4] 白天启动")


def day(temp, sustained, hum=50, ah=13.0):
    return W.decide(temp=temp, hum=hum, running=False, since_on=None, since_off=99,
                    is_night=False, compressor=None, current_target=26, ah=ah,
                    compressor_run_min=None, night_comp_starts=[], sustained=sustained)


m, _, _ = day(27.0, False)
R_.check("白天 27°C 短暂触碰 → 不开", m is None, f"got={m}")
m, _, _ = day(27.0, True)
R_.check("白天 27°C 持续 → 开机", m == "cooling", f"got={m}")
m, _, _ = day(29.5, False)
R_.check("白天 29.5°C 未持续 → 紧急放行", m == "cooling", f"got={m}")

# ── 5. 闷热不受持续时长约束（闷是即时体感） ──
print("\n[5] 高湿闷热免等待")
m, t, reason = day(27.0, False, hum=A.HUM_DEHUMID_ON, ah=17.0)
R_.check(f"RH={A.HUM_DEHUMID_ON}% 闷热 + 未持续 → 仍开除湿",
         m == "cooling", f"got={m} {reason}")
R_.check("走的是除湿分支", "除湿" in str(reason), f"got={reason}")

# ── 6. 不影响运行中的关机决策 ──
print("\n[6] 关机路径不受影响")
m, _, reason = W.decide(
    temp=23.0, hum=70, running=True, since_on=5, since_off=None, is_night=True,
    compressor="compressor", current_target=25, ah=18.0, compressor_run_min=3,
    sustained=False,
)
R_.check("过冷逃生门仍无条件关机", m == "off", f"got={m} {reason}")

# ── 7. 启动线未被回调（27 保持） ──
print("\n[7] 启动线保持 27（不回退到 28）")
R_.check("TEMP_COOLING 仍为 27", A.TEMP_COOLING == 27, f"got={A.TEMP_COOLING}")
R_.check("NIGHT_START_T 仍为 27.0", W.NIGHT_START_T == 27.0, f"got={W.NIGHT_START_T}")
R_.check("紧急线高于启动线", W.SUSTAIN_URGENT_T > A.TEMP_COOLING,
         f"urgent={W.SUSTAIN_URGENT_T} start={A.TEMP_COOLING}")

print("\n" + "=" * 70)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 70)
sys.exit(1 if R_.fail else 0)
