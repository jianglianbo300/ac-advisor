#!/usr/bin/env python3
"""v8.35 自学习闭环 — 回归测试。

验证 evaluate_and_learn 在当前版本下的行为：
- 负偏移被 clamp 到 0（v8.30: 防止启动线压进抖振死区）
- 成功时正偏移向 0 收敛（cur_adj - 1）
- 预算逻辑：超预算 +0.5，欠预算 -0.5
- 功率闸门：压缩机在转时不判失败
- v8.29 修复：关机后回热不算失败
- 收敛性：正偏移连续成功 → 收敛到 0

历史注记：v8.23 时代失败时偏移 -1 并可负向累积，v8.30 起负偏移 clamp 到 0。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import ac_advisor as A


class R:
    def __init__(self):
        self.ok = 0
        self.fail = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            print(f"  [PASS] {name}")
        else:
            self.fail += 1
            print(f"  [FAIL] {name} {detail}")


R_ = R()
print("=" * 70)
print("v8.35 自学习闭环 — 回归测试")
print("=" * 70)


def run_learn(start_adj, action, pre_temp, cur_temp, pre_hum=50, cur_hum=50,
              measured_w=None, daily_kwh=8.0):
    """在临时 learned 文件上跑一轮 evaluate_and_learn，返回调整后的偏移。

    daily_kwh 默认 8.0 = daily_budget，使预算逻辑两个分支都不触发，
    从而隔离测试核心学习行为。需测试预算逻辑时显式传入其他值。
    """
    tmpdir = tempfile.mkdtemp(prefix="learn_")
    path = os.path.join(tmpdir, "ac_learned.json")
    ts = (datetime.now() - timedelta(minutes=35)).isoformat()
    json.dump({"adjusted_thresholds": {"temp_cooling": start_adj},
               "decision_log": [{"time": ts, "action": action,
                                 "pre_temp": pre_temp, "pre_hum": pre_hum,
                                 "evaluated": False}]},
              open(path, "w", encoding="utf-8"), ensure_ascii=False)

    orig_file = A.LEARN_FILE
    orig_w = getattr(A, "AC_MEASURED_W", None)
    A.LEARN_FILE = path
    A.AC_MEASURED_W = measured_w
    try:
        _today = datetime.now().strftime("%Y-%m-%d")
        state = {"last_temp": cur_temp, "last_hum": cur_hum,
                 "_prev_power": measured_w, "_daily_kwh": daily_kwh,
                 "_budget_prediction": {"date": _today, "predicted_kwh": 8.0}}  # P2: 短路 fetch_weather 真网络调用
        A.evaluate_and_learn(state, datetime.now().isoformat())
        return json.load(open(path, encoding="utf-8"))["adjusted_thresholds"]["temp_cooling"]
    finally:
        A.LEARN_FILE = orig_file
        A.AC_MEASURED_W = orig_w


print("\n[ac_advisor]")

# ── 失败 → 降低阈值（但 v8.30 起负偏移 clamp 到 0）──
got = run_learn(0, "cooling", 27.0, 27.0, measured_w=0)   # 没降温、压缩机没转
R_.check("失败 → 偏移 0 → 0（v8.30: 负偏移 clamp 到 0）", got == 0, f"got={got}")

# ── 成功且偏移 > 0 → 向 0 收敛 ──
got = run_learn(2, "cooling", 27.0, 25.0, measured_w=0)   # 降了 2°C = 成功
R_.check("成功且偏移 +2 → 回收到 +1", got == 1, f"got={got}")

got = run_learn(1, "cooling", 27.0, 25.0, measured_w=0)
R_.check("成功且偏移 +1 → 回收到 0", got == 0, f"got={got}")

# ── 成功但偏移 ≤ 0 → 不再变化（v8.30: 不再产出负偏移）──
got = run_learn(0, "cooling", 27.0, 25.0, measured_w=0)
R_.check("成功且偏移已中性 → 保持 0", got == 0, f"got={got}")

got = run_learn(-2, "cooling", 27.0, 25.0, measured_w=0)
R_.check("成功且偏移 -2 → 保持 -2（v8.30: 负偏移不再变化）", got == -2, f"got={got}")

# ── 钳位：失败但已在 0 → 钳在 0 ──
got = run_learn(0, "cooling", 27.0, 27.0, measured_w=0)
R_.check("失败但已在 0 → 钳在 0（v8.30: 负偏移 clamp）", got == 0, f"got={got}")

# ── 功率闸门：压缩机真在转 → 慢降温不算失败 ──
got = run_learn(0, "cooling", 27.0, 27.0, measured_w=1000)
R_.check("压缩机在转(1000W)+降温慢 → 不判失败（物理限制）",
         got == 0, f"got={got}")

# ── v8.29 修复：关机后回热不算失败 ──
got = run_learn(0, "off", 26.0, 29.0, measured_w=0)       # 升了 3°C
R_.check("没开但温度升 3°C → 保持 0（v8.29: 回热不是失败）", got == 0, f"got={got}")

# ── 多轮收敛性 ──
print("\n[收敛性：连续成功正偏移应收敛到 0]")
adj = 3
for i in range(5):
    adj = run_learn(adj, "cooling", 27.0, 25.0, measured_w=0)
R_.check("从 +3 连续 5 次成功 → 收敛到 0", adj == 0, f"got={adj}")

print("\n[收敛性：连续失败不再越界]")
adj = 0
for i in range(5):
    adj = run_learn(adj, "cooling", 27.0, 27.0, measured_w=0)
R_.check("连续 5 次失败 → 钳在 0 不越界", adj == 0, f"got={adj}")

# ── 预算逻辑（显式传入 daily_kwh 触发）──
print("\n[预算逻辑]")
got = run_learn(0, "cooling", 27.0, 25.0, measured_w=0, daily_kwh=12.0)
R_.check("超预算(12kWh > 8x1.3=10.4) → 偏移 +0.5", got == 0.5, f"got={got}")

got = run_learn(2, "cooling", 27.0, 25.0, measured_w=0, daily_kwh=12.0)
R_.check("超预算 + 成功收敛 → +1 后 +0.5 = 1.5", got == 1.5, f"got={got}")

# ── 当前线上状态 ──
print("\n[线上状态]")
live = A.load_learned().get("adjusted_thresholds", {}).get("temp_cooling", 0)
R_.check("线上偏移已复位为 0", live == 0, f"got={live}")

import ac_watch as W
R_.check("顾问侧与实控侧阈值一致",
         A.TEMP_COOLING + live == A.TEMP_COOLING == int(W.NIGHT_START_T),
         f"advisor={A.TEMP_COOLING + live} watch={A.TEMP_COOLING} night={W.NIGHT_START_T}")

print("\n" + "=" * 70)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 70)
sys.exit(1 if R_.fail else 0)
