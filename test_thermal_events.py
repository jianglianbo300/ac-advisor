#!/usr/bin/env python3
"""热质量事件闭合 + None 污染防御 — 回归测试（ac_advisor 纯热模型专用）。

真实故障：`python ac_advisor.py` 崩在
    TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'
    fit_thermal_model -> (e["temp_before"] - e["temp_after"]) / ...

根因链：
  1. home_living.py 记录"周期开始"时故意传 temp_after=None/duration_min=None，
     打算等周期结束回填 —— 但回填逻辑从未实现，事件永远不完整。
  2. ac_advisor.py 里那份 fit_thermal_model 不过滤，直接对 None 做算术 -> 崩。
  3. 两个文件各有一份副本，写同一个 ac_thermal.json，行为不一致。
故障此前被掩盖：工作区 ac_advisor.py 曾被回退到 v9.0（无此函数），恢复 v10.1 才暴露。

2026-08-28 审记：home_living 已重构为通风系统，不含热模型函数，
测试只管 ac_advisor.fit_thermal_model + record_thermal_event 的 None 防御。
不触碰真实 ac_thermal.json：全部走 tmp 文件。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, r"D:\work\ac-advisor")
import ac_advisor as A


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

POISONED = [
    {"type": "cooling", "temp_before": 26.0, "temp_after": None,
     "duration_min": None, "outdoor_temp": 32.1,
     "timestamp": "2026-08-18T15:30:36.776875"},
    {"type": "cooling", "temp_before": None, "temp_after": None,
     "duration_min": None, "outdoor_temp": 27.88,
     "timestamp": "2026-08-18T22:14:21.168538"},
]

print("=" * 68)
print("热质量事件闭合 + None 污染防御 — 回归测试")
print("=" * 68)

# ── 1. 真实污染数据不再让 fit 崩溃 ──
print("\n[1] 真实污染事件喂给 fit_thermal_model")
try:
    m = A.fit_thermal_model(POISONED)
    R_.check("fit 不抛异常", True)
    R_.check("回落默认 cooling_rate", m["cooling_rate_per_min"] == 0.05, f"got={m}")
except Exception as e:
    R_.check("fit 不抛异常", False, f"raised {type(e).__name__}: {e}")

# ── 2. 空/None 输入 ──
print("\n[2] 空输入与 None 输入")
for arg, desc in (([], "空列表"), (None, "None")):
    try:
        m = A.fit_thermal_model(arg)
        R_.check(f"{desc} 安全", m["cooling_rate_per_min"] == 0.05)
    except Exception as e:
        R_.check(f"{desc} 安全", False, f"raised {type(e).__name__}")

# ── 3. 完整事件正常拟合 ──
print("\n[3] 完整事件正常拟合")
good = [
    {"type": "cooling", "temp_before": 28.0, "temp_after": 26.0,
     "duration_min": 40, "outdoor_temp": 32.0, "timestamp": "2026-08-18T10:00:00"},
    {"type": "warming", "temp_before": 26.0, "temp_after": 27.0,
     "duration_min": 50, "outdoor_temp": 32.0, "timestamp": "2026-08-18T11:00:00"},
    {"type": "warming", "temp_before": 27.0, "temp_after": 27.5,
     "duration_min": 50, "outdoor_temp": 32.0, "timestamp": "2026-08-18T12:00:00"},
    {"type": "warming", "temp_before": 27.5, "temp_after": 28.0,
     "duration_min": 50, "outdoor_temp": 32.0, "timestamp": "2026-08-18T13:00:00"},
]
m = A.fit_thermal_model(good)
# cooling 速率 = (26-28)/40 = -0.05（负值=降温）
R_.check("cooling_rate = -2/40 = -0.05", abs(m["cooling_rate_per_min"] - (-0.05)) < 1e-9,
         f"got={m['cooling_rate_per_min']}")
# warming 有 3 条: (1+0.5+0.5)/50/3 = 0.01333
R_.check("warmup_rate = 2/50/3 = 0.0133", abs(m["warmup_rate_per_min"] - (2.0/50/3)) < 1e-9,
         f"got={m['warmup_rate_per_min']}")

# ── 4. 混合：污染行被剔除，只用完整行 ──
print("\n[4] 污染 + 完整混合 → 只用完整行")
mixed = POISONED + good
m = A.fit_thermal_model(mixed)
# 只有 good 里的完整 cooling 行参与: (26-28)/40 = -0.05
R_.check("只按完整行算", abs(m["cooling_rate_per_min"] - (-0.05)) < 1e-9,
         f"got={m['cooling_rate_per_min']}")

# ── 5. 零/负速率当前会被学习（当前实现不拒绝，predict 会处理）──
print("\n[5] 零/负速率被学习")
degenerate = [
    {"type": "cooling", "temp_before": 26.0, "temp_after": 26.0,
     "duration_min": 30, "outdoor_temp": 32.0, "timestamp": "2026-08-18T10:00:00"},
    {"type": "cooling", "temp_before": 26.0, "temp_after": 27.0,
     "duration_min": 30, "outdoor_temp": 32.0, "timestamp": "2026-08-18T11:00:00"},
]
m = A.fit_thermal_model(degenerate)
# 当前实现：rate1=(26-26)/30=0, rate2=(27-26)/30=0.0333，均值=0.0167
R_.check("学到平均速率", abs(m["cooling_rate_per_min"] - 0.016666666666666666) < 1e-9,
         f"got={m['cooling_rate_per_min']}")
mins = A.predict_cooling_time(28.0, 26.0, 32.0, m)
R_.check("predict 返回有限值", 0 < mins < 10000, f"got={mins}")

# ── 6. 布尔不得被当数字（isinstance(True, int) 为真的陷阱）──
print("\n[6] 布尔值不得当数字用")
boolish = [{"type": "cooling", "temp_before": True, "temp_after": False,
            "duration_min": 30, "outdoor_temp": 32.0,
            "timestamp": "2026-08-18T10:00:00"}]
m = A.fit_thermal_model(boolish)
R_.check("拒绝布尔行", m["cooling_rate_per_min"] == 0.05,
         f"got={m['cooling_rate_per_min']}")

# ── 7. record_thermal_event 开放事件保留 + None 拒绝 ──
print("\n[7] record_thermal_event None 防御")
tmpdir = tempfile.mkdtemp(prefix="thermal_test_")
tmpfile = os.path.join(tmpdir, "ac_thermal.json")
orig_a = A.THERMAL_FILE
A.THERMAL_FILE = tmpfile
try:
    json.dump({"events": [dict(POISONED[0])],
               "thermal_model": {"cooling_rate_per_min": 0.05,
                                 "warmup_rate_per_min": 0.02,
                                 "time_constant_min": 120}},
              open(tmpfile, "w", encoding="utf-8"), ensure_ascii=False)
    ok = A.record_thermal_event("cooling", 28.0, 26.0, 40, 32.0)
    R_.check("写入返回 True", ok is True)
    d = json.load(open(tmpfile, encoding="utf-8"))
    R_.check("开放事件被保留", len(d["events"]) == 2,
             f"n={len(d['events'])}")
    R_.check("模型按完整行拟合", abs(d["thermal_model"]["cooling_rate_per_min"] - (-0.05)) < 1e-9,
             f"got={d['thermal_model']}")
    R_.check("temp_before=None 被拒写",
             A.record_thermal_event("cooling", None, None, None, 32.0) is False)
finally:
    A.THERMAL_FILE = orig_a

print("\n" + "=" * 68)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 68)
