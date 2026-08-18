#!/usr/bin/env python3
"""v8.23 自学习单向棘轮修复 — 回归测试。

用户说「让它自动迭代吧」，动手前先审自学习闭环，发现它根本不会收敛：

  原实现的 evaluate_and_learn 里，失败时两个分支都做 `cur_adj - 1`，
  成功时**什么都不做**。于是无论评估结果如何，偏移量只降不升 —— 单向棘轮，
  必然一路漂到下限并永久停在边界上。

  实测证据：ac_learned.json 的 temp_cooling 已是 -2（钳位下限）。
  v11.1 那次「修漂移到 -8」只加了钳位止损，没修方向性，所以病根还在。

  叠加风险：顾问侧 effective_cooling_threshold += 偏移，于是基线 27 + (-2) = 25，
  而实控侧 ac_watch 用的是 27 —— 同一套策略两侧阈值不一致。

另外发现 v11.1 的功率闸门只做在 home_living.py，ac_advisor.py 那份副本没有
（同一逻辑两份副本只修一份，今天已第二次踩到）。1.5 匹带 65 平米降温慢是物理
限制，缺闸门会把它误判成阈值问题。

修复：①两份副本都加"成功时向 0 收敛"②ac_advisor 补齐功率闸门与钳位
③偏移复位为 0（旧值在基线 28 上学得，基线已降至 27 且加了持续判据）。
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
print("=" * 70)
print("v8.23 自学习单向棘轮修复 — 回归测试")
print("=" * 70)


def run_learn(mod, start_adj, action, pre_temp, cur_temp, pre_hum=50, cur_hum=50,
              measured_w=None):
    """在临时 learned 文件上跑一轮 evaluate_and_learn，返回调整后的偏移。"""
    tmpdir = tempfile.mkdtemp(prefix="learn_")
    path = os.path.join(tmpdir, "ac_learned.json")
    ts = (datetime.now() - timedelta(minutes=35)).isoformat()
    json.dump({"adjusted_thresholds": {"temp_cooling": start_adj},
               "decision_log": [{"time": ts, "action": action,
                                 "pre_temp": pre_temp, "pre_hum": pre_hum,
                                 "evaluated": False}]},
              open(path, "w", encoding="utf-8"), ensure_ascii=False)

    orig_file = mod.LEARN_FILE
    orig_w = getattr(mod, "AC_MEASURED_W", None)
    mod.LEARN_FILE = path
    mod.AC_MEASURED_W = measured_w
    try:
        # 两份实现读功率的方式不同：ac_advisor 用模块级 AC_MEASURED_W，
        # home_living 从 state["_prev_power"] 读。两边都注入，测试才对两者等效。
        state = {"last_temp": cur_temp, "last_hum": cur_hum,
                 "_prev_power": measured_w}
        mod.evaluate_and_learn(state, datetime.now().isoformat())
        return json.load(open(path, encoding="utf-8"))["adjusted_thresholds"]["temp_cooling"]
    finally:
        mod.LEARN_FILE = orig_file
        mod.AC_MEASURED_W = orig_w


for label, mod in (("ac_advisor", A), ("home_living", H)):
    print(f"\n[{label}]")

    # ── 失败 → 降低阈值（更早开）──
    got = run_learn(mod, 0, "cooling", 27.0, 27.0, measured_w=0)   # 没降温、压缩机没转
    R_.check("失败 → 偏移 0 → -1", got == -1, f"got={got}")

    # ── 成功 → 向 0 收敛（这是修复的核心）──
    got = run_learn(mod, -2, "cooling", 27.0, 25.0, measured_w=0)  # 降了 2°C = 成功
    R_.check("成功且偏移 -2 → 回收到 -1（不再永久停在边界）", got == -1, f"got={got}")

    got = run_learn(mod, 2, "cooling", 27.0, 25.0, measured_w=0)
    R_.check("成功且偏移 +2 → 回收到 +1", got == 1, f"got={got}")

    got = run_learn(mod, 0, "cooling", 27.0, 25.0, measured_w=0)
    R_.check("成功且偏移已中性 → 保持 0", got == 0, f"got={got}")

    # ── 钳位 ──
    got = run_learn(mod, -2, "cooling", 27.0, 27.0, measured_w=0)
    R_.check("失败但已在下限 → 钳在 -2", got == -2, f"got={got}")

    # ── 功率闸门：压缩机真在转 → 慢降温不算失败 ──
    got = run_learn(mod, 0, "cooling", 27.0, 27.0, measured_w=1000)
    R_.check("压缩机在转(1000W)+降温慢 → 不判失败（物理限制）",
             got == 0, f"got={got}")

    # ── 没开却闷了 → 降低阈值 ──
    got = run_learn(mod, 0, "off", 26.0, 29.0, measured_w=0)       # 升了 3°C
    R_.check("没开但温度升 3°C → 偏移 -1", got == -1, f"got={got}")

# ── 多轮不再单向漂移（核心属性）──
print("\n[收敛性：连续成功不应停在边界]")
adj = -2
for i in range(3):
    adj = run_learn(A, adj, "cooling", 27.0, 25.0, measured_w=0)
R_.check("从 -2 连续 3 次成功 → 收敛到 0", adj == 0, f"got={adj}")

adj = 0
for i in range(5):
    adj = run_learn(A, adj, "cooling", 27.0, 27.0, measured_w=0)
R_.check("连续 5 次失败 → 钳在 -2 不越界", adj == -2, f"got={adj}")

# ── 当前线上状态：偏移已复位、两侧阈值一致 ──
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
