# -*- coding: utf-8 -*-
"""v8.37 峰电启动线闷热豁免 — 分支级重放审计。

重放纪律（08-29 事故教训）：mock 三件套 load_learned / save_learned / current_price，
不触碰任何真实状态文件。
"""
import sys
import ac_advisor as A
import ac_watch as W

# ── mock 三件套 ──
A.load_learned = lambda: {"adjusted_thresholds": {"temp_cooling": 0}, "decision_log": []}
A.save_learned = lambda *a, **k: None
PEAK, VALLEY = A.ELECTRIC_PEAK + 1, 0.0
A.current_price = lambda: PEAK

R = {"ok": 0, "fail": 0}
def check(name, got, want):
    if got[0] == want:
        R["ok"] += 1; print(f"  [PASS] {name} -> {got[0]}")
    else:
        R["fail"] += 1; print(f"  [FAIL] {name} -> got={got} want={want}")

base = dict(running=False, since_on=999, since_off=999,
            is_night=False, current_target=26, sustained=True, evening=False)

# 峰电
A.current_price = lambda: PEAK
check("峰电 27.0/RH50 干热 → 推迟不开（28 才开）",
      W.decide(temp=27.0, hum=50, **base), None)
check("峰电 28.0/RH50 干热达标 → 开",
      W.decide(temp=28.0, hum=50, **base), "cooling")
check("峰电 27.0/RH67 闷热 → 豁免立即开（v8.37 修复点）",
      W.decide(temp=27.0, hum=67, **base), "cooling")
check("峰电 26.0/RH70 → 仍推迟（豁免只在启动线上）",
      W.decide(temp=26.0, hum=70, **base), None)
check("峰电 27.0/hum=None → 保守推迟（缺湿度按干热）",
      W.decide(temp=27.0, hum=None, **base), None)

# 谷电
A.current_price = lambda: VALLEY
check("谷电 27.0/RH50 → 正常开",
      W.decide(temp=27.0, hum=50, **base), "cooling")
check("谷电 26.5/RH70 → 强力除湿路径不受影响",
      W.decide(temp=26.5, hum=70, **base), "cooling")

# sustained 闸门与闷热豁免的组合（v8.36 #1 语义）
A.current_price = lambda: PEAK
base_ns = dict(base, sustained=False)
check("峰电 27.0/RH67 闷热 + 未持续10min → 仍开（湿度优先，同 v8.36 设计）",
      W.decide(temp=27.0, hum=67, **base_ns), "cooling")
check("峰电 27.0/RH50 干热 + 未持续 → 推迟（推迟+sustain 双闸）",
      W.decide(temp=27.0, hum=50, **base_ns), None)

print(f"\n结果: {R['ok']} passed, {R['fail']} failed")
sys.exit(1 if R["fail"] else 0)
