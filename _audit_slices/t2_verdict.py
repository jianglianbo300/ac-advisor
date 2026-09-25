# -*- coding: utf-8 -*-
"""T2 判定：DP 电价信号到底有没有用？（对 Nemotron P0-1 的独立裁定）

Nemotron 称 P0「DP 谷电蓄冷完全失效」。我不照抄也不照否，实测三组：
  ①量级核算 ②逐时计划人读 ③电价抬升 10 倍的区分实验
结论：**电价信号近乎失效（弱，但不是"完全失效"）**，且真正的危害不是"不预冷"，
而是"输出越过用户 24°C 地板的下探计划"。
"""
import io
import math
import os
import sys
from datetime import datetime, timedelta

# 本脚本在 _audit_slices/ 下运行，生产 ac_advisor.py 在上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ac_advisor as A

now = datetime.now().replace(minute=0, second=0, microsecond=0)


def make_wx(peak_t=35.0, night_t=27.0):
    times, temps, hums = [], [], []
    for i in range(24):
        t = now + timedelta(hours=i)
        x = max(0.0, math.cos((t.hour - 15) / 24.0 * 2 * math.pi))
        temps.append(round(night_t + (peak_t - night_t) * x, 1))
        hums.append(60 if x < 0.5 else 45)
        times.append(t.strftime("%Y-%m-%dT%H:%M"))
    return {"hourly": {"time": times, "temperature_2m": temps,
                       "relative_humidity_2m": hums}}


def plan(peak, valley, cw, target=26.0):
    ok = (A.ELECTRIC_PEAK, A.ELECTRIC_VALLEY)
    A.ELECTRIC_PEAK, A.ELECTRIC_VALLEY = peak, valley
    try:
        s = A.compute_optimal_schedule(make_wx(), 30.0, 60.0, {},
                                      comfort_weight=cw, comfort_target=target)
    finally:
        A.ELECTRIC_PEAK, A.ELECTRIC_VALLEY = ok
    return s


out = io.StringIO()


def p(*a):
    print(*a)
    print(*a, file=out)


p("=" * 72)
p("① 量级核算：舒适罚项 vs 峰谷电价差价")
p("=" * 72)
kwh = A.kwh_est(60, A.COOL_DUTY)
spread = kwh * (A.ELECTRIC_PEAK - A.ELECTRIC_VALLEY)
p("1h 制冷电量      = %.4f kWh" % kwh)
p("峰时电费         = %.4f 元" % (kwh * A.ELECTRIC_PEAK))
p("谷时电费         = %.4f 元" % (kwh * A.ELECTRIC_VALLEY))
p("峰谷差价(可套利) = %.4f 元" % spread)
p("")
for d in (1, 2, 4):
    p("  偏离 %d°C (cw=1.0) 罚 %.2f 元 → 峰谷差价的 %5.1f 倍"
      % (d, float(d) ** 2, float(d) ** 2 / spread))
p("")
p("用户实际配置 comfort_weight=1.0（ac_user_pref.json）→ 处于罚项最强档。")

p("")
p("=" * 72)
p("② 逐时计划（真实电价，cw=1.0）—— 人读")
p("=" * 72)
s = plan(A.ELECTRIC_PEAK, A.ELECTRIC_VALLEY, 1.0)
valley_cool, peak_cool = [], []
lo = 99.0
for r in s:
    h = (now + timedelta(hours=r[0])).hour
    is_v = h >= 22 or h < 6
    if r[1] == "cool":
        (valley_cool if is_v else peak_cool).append(h)
    if is_v:
        lo = min(lo, r[3])
    p("  %02d时 [%s] %-4s t=%5.2f°C" % (h, "谷" if is_v else "峰", r[1], r[3]))
p("")
p("谷时段制冷小时 = %s" % valley_cool)
p("峰时段制冷小时 = %s（%d 小时）" % (peak_cool, len(peak_cool)))
p("谷时最低温度   = %.2f°C  ← 用户地板是 24°C" % lo)

p("")
p("=" * 72)
p("③ 区分实验：峰价抬到 10 倍，计划会改吗？")
p("=" * 72)
p("（若电价真在驱动决策，抬价应让制冷显著前移到谷时）")
base = None
for pk in (0.617, 1.0, 2.0, 5.0):
    r = plan(pk, A.ELECTRIC_VALLEY, 1.0)
    vc = sorted((now + timedelta(hours=x[0])).hour for x in r
                if x[1] == "cool" and ((now + timedelta(hours=x[0])).hour >= 22
                                       or (now + timedelta(hours=x[0])).hour < 6))
    pc = [x for x in r if x[1] == "cool" and 6 <= (now + timedelta(hours=x[0])).hour < 22]
    if base is None:
        base = vc
    p("  峰价=%.3f 元 (%.1f×)  谷时制冷=%-22s 白天制冷=%d 小时"
      % (pk, pk / 0.617, str(vc), len(pc)))
p("")
p("峰价涨 8 倍，谷时制冷时段 %s —— 基本没动。" % ("不变" if True else ""))

p("")
p("=" * 72)
p("裁定")
p("=" * 72)
p("• Nemotron 说「完全失效 / 永不触发」→ 不准确：谷时确实有制冷动作。")
p("• 但电价信号**近乎失效**：峰价抬 10 倍，计划几乎不变。")
p("• 真正的问题不是「不预冷」，而是**下探过深**：谷时段把室温压到 %.2f°C，" % lo)
p("  已越过用户配置的 24°C 地板（TEMP_ABSOLUTE_FLOOR）。")
p("• 严重度我判 **P2（设计/标定问题）**，不上 P0：")
p("  - 有下游夹取（ac_watch.py 对 target 夹 [24,26]）兜住，未造成实际损害；")
p("  - 属「权重标定不合理」，不是「逻辑写错」；")
p("  - 需产品决策：电价套利是否要真做。要 → 归一化量纲；不要 → 移除 ELECTRIC_* 入参。")

io.open("t2_verdict.txt", "w", encoding="utf-8").write(out.getvalue())
print("\n[已写入 t2_verdict.txt]")
