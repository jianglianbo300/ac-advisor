#!/usr/bin/env python3
"""热质量事件闭合 + None 污染防御 — 回归测试。

真实故障：`python ac_advisor.py` 崩在
    TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'
    fit_thermal_model -> (e["temp_before"] - e["temp_after"]) / ...

根因链：
  1. home_living.py 记录"周期开始"时故意传 temp_after=None/duration_min=None，
     打算等周期结束回填 —— 但回填逻辑从未实现，事件永远不完整。
  2. ac_advisor.py 里那份 fit_thermal_model 不过滤，直接对 None 做算术 -> 崩。
  3. 两个文件各有一份副本，写同一个 ac_thermal.json，行为不一致。
故障此前被掩盖：工作区 ac_advisor.py 曾被回退到 v9.0（无此函数），恢复 v10.1 才暴露。

不触碰真实 ac_thermal.json：全部走 tmp 文件。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import ac_advisor as A
import home_living as H


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

# ── 1. 真实污染数据不再让 fit 崩溃（两份实现都要防）──
print("\n[1] 真实污染事件喂给 fit_thermal_model")
for label, mod in (("ac_advisor", A), ("home_living", H)):
    try:
        m = mod.fit_thermal_model(POISONED)
        R_.check(f"{label}.fit 不抛异常", True)
        R_.check(f"{label} 回落默认 cooling_rate",
                 m["cooling_rate_per_min"] == 0.05, f"got={m}")
    except Exception as e:
        R_.check(f"{label}.fit 不抛异常", False, f"raised {type(e).__name__}: {e}")

# ── 2. 空/None 输入 ──
print("\n[2] 空输入与 None 输入")
for label, mod in (("ac_advisor", A), ("home_living", H)):
    for arg, desc in (([], "空列表"), (None, "None")):
        try:
            m = mod.fit_thermal_model(arg)
            R_.check(f"{label} {desc} 安全", m["cooling_rate_per_min"] == 0.05)
        except Exception as e:
            R_.check(f"{label} {desc} 安全", False, f"raised {type(e).__name__}")

# ── 3. 完整事件正常拟合 ──
print("\n[3] 完整事件正常拟合")
good = [
    {"type": "cooling", "temp_before": 28.0, "temp_after": 26.0,
     "duration_min": 40, "outdoor_temp": 32.0, "timestamp": "2026-08-18T10:00:00"},
    {"type": "warming", "temp_before": 26.0, "temp_after": 27.0,
     "duration_min": 50, "outdoor_temp": 32.0, "timestamp": "2026-08-18T11:00:00"},
]
for label, mod in (("ac_advisor", A), ("home_living", H)):
    m = mod.fit_thermal_model(good)
    R_.check(f"{label} cooling_rate = 2/40 = 0.05", abs(m["cooling_rate_per_min"] - 0.05) < 1e-9,
             f"got={m['cooling_rate_per_min']}")
    R_.check(f"{label} warmup_rate = 1/50 = 0.02", abs(m["warmup_rate_per_min"] - 0.02) < 1e-9,
             f"got={m['warmup_rate_per_min']}")

# ── 4. 混合：污染行被剔除，只用完整行 ──
print("\n[4] 污染 + 完整混合 → 只用完整行")
mixed = POISONED + good
for label, mod in (("ac_advisor", A), ("home_living", H)):
    m = mod.fit_thermal_model(mixed)
    R_.check(f"{label} 只按完整行算", abs(m["cooling_rate_per_min"] - 0.05) < 1e-9,
             f"got={m['cooling_rate_per_min']}")

# ── 5. 零/负速率不得被学习（否则 predict 除以 ~0）──
print("\n[5] 零/负速率不入模型")
degenerate = [
    {"type": "cooling", "temp_before": 26.0, "temp_after": 26.0,
     "duration_min": 30, "outdoor_temp": 32.0, "timestamp": "2026-08-18T10:00:00"},
    {"type": "cooling", "temp_before": 26.0, "temp_after": 27.0,
     "duration_min": 30, "outdoor_temp": 32.0, "timestamp": "2026-08-18T11:00:00"},
]
for label, mod in (("ac_advisor", A), ("home_living", H)):
    m = mod.fit_thermal_model(degenerate)
    R_.check(f"{label} 保持默认非零速率", m["cooling_rate_per_min"] == 0.05,
             f"got={m['cooling_rate_per_min']}")
    mins = mod.predict_cooling_time(28.0, 26.0, 32.0, m)
    R_.check(f"{label} predict 返回有限值", 0 < mins < 10000, f"got={mins}")

# ── 6. 布尔不得被当数字（isinstance(True, int) 为真的陷阱）──
print("\n[6] 布尔值不得当数字用")
boolish = [{"type": "cooling", "temp_before": True, "temp_after": False,
            "duration_min": 30, "outdoor_temp": 32.0,
            "timestamp": "2026-08-18T10:00:00"}]
for label, mod in (("ac_advisor", A), ("home_living", H)):
    m = mod.fit_thermal_model(boolish)
    R_.check(f"{label} 拒绝布尔行", m["cooling_rate_per_min"] == 0.05,
             f"got={m['cooling_rate_per_min']}")

# ── 7. home_living 周期闭合回填（核心新逻辑）──
print("\n[7] home_living 周期闭合回填")
tmpdir = tempfile.mkdtemp(prefix="thermal_test_")
tmpfile = os.path.join(tmpdir, "ac_thermal.json")
orig_file = H.THERMAL_FILE
H.THERMAL_FILE = tmpfile
try:
    # 第一次：开启制冷（开放事件）
    H.record_thermal_event("cooling", 28.0, None, None, 32.0)
    d = json.load(open(tmpfile, encoding="utf-8"))
    R_.check("首个事件已落盘", len(d["events"]) == 1, f"n={len(d['events'])}")
    R_.check("首个事件为开放态", d["events"][0]["temp_after"] is None)

    # 人为把开始时间挪早 40 分钟，模拟一个真实周期
    started = datetime.now() - timedelta(minutes=40)
    d["events"][0]["timestamp"] = started.isoformat()
    json.dump(d, open(tmpfile, "w", encoding="utf-8"), ensure_ascii=False)

    # 第二次：转 warming，应闭合上一条
    H.record_thermal_event("warming", 26.0, None, None, 32.0)
    d = json.load(open(tmpfile, encoding="utf-8"))
    R_.check("事件数为 2", len(d["events"]) == 2, f"n={len(d['events'])}")
    prev = d["events"][0]
    R_.check("上一条被回填 temp_after=26.0", prev["temp_after"] == 26.0, f"got={prev}")
    R_.check("上一条被回填 duration≈40", prev["duration_min"] in (39, 40, 41),
             f"got={prev['duration_min']}")
    R_.check("模型已从真实周期学习",
             abs(d["thermal_model"]["cooling_rate_per_min"] - (2.0 / prev["duration_min"])) < 1e-6,
             f"got={d['thermal_model']}")

    # 亚分钟抖动不得闭合（无速率信号）
    H.record_thermal_event("cooling", 27.0, None, None, 32.0)
    d = json.load(open(tmpfile, encoding="utf-8"))
    R_.check("亚分钟抖动不闭合上一条", d["events"][1]["temp_after"] is None,
             f"got={d['events'][1]}")

    # 超长间隔（进程停机）不得闭合
    d["events"][-1]["timestamp"] = (datetime.now() - timedelta(minutes=900)).isoformat()
    json.dump(d, open(tmpfile, "w", encoding="utf-8"), ensure_ascii=False)
    H.record_thermal_event("warming", 25.0, None, None, 32.0)
    d = json.load(open(tmpfile, encoding="utf-8"))
    R_.check("超长间隔(900min)不闭合", d["events"][2]["temp_after"] is None,
             f"got={d['events'][2]}")
finally:
    H.THERMAL_FILE = orig_file

# ── 8. ac_advisor 写入端保留开放事件（不能破坏 home_living 的回填）──
print("\n[8] ac_advisor 写入端保留开放事件")
tmpfile2 = os.path.join(tmpdir, "ac_thermal2.json")
orig_a = A.THERMAL_FILE
A.THERMAL_FILE = tmpfile2
try:
    json.dump({"events": [dict(POISONED[0])],
               "thermal_model": {"cooling_rate_per_min": 0.05,
                                 "warmup_rate_per_min": 0.02,
                                 "time_constant_min": 120}},
              open(tmpfile2, "w", encoding="utf-8"), ensure_ascii=False)
    ok = A.record_thermal_event("cooling", 28.0, 26.0, 40, 32.0)
    R_.check("写入返回 True", ok is True)
    d = json.load(open(tmpfile2, encoding="utf-8"))
    R_.check("开放事件被保留（供 home_living 回填）", len(d["events"]) == 2,
             f"n={len(d['events'])} events={d['events']}")
    R_.check("模型只按完整行拟合",
             abs(d["thermal_model"]["cooling_rate_per_min"] - 0.05) < 1e-9,
             f"got={d['thermal_model']}")
    R_.check("temp_before=None 被拒写",
             A.record_thermal_event("cooling", None, None, None, 32.0) is False)
finally:
    A.THERMAL_FILE = orig_a

print("\n" + "=" * 68)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 68)
sys.exit(1 if R_.fail else 0)
