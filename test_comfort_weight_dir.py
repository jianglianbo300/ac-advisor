#!/usr/bin/env python3
"""v8.37 comfort_weight 学习方向 — 回归测试。

背景（CodeBuddy hy4 线任务 #5）：_learn_from_manual 两个分支方向此前是反的。
语义核实：comfort_weight 全仓只作用于 ac_advisor.py:114 的
comfort_penalty = comfort_weight * (t_in - comfort_target)**2，
DP 求最小化 → 越大 = 越舍得开制冷。因此：
- 手动开机(cycling)≥3 条 → 用户嫌热 → 上调 comfort_weight
- 手动关机(off)≥3 条   → 用户嫌冷 → 下调 comfort_weight

测试纪律（2026-08-29 事故教训）：全程在临时目录跑（A.SCRIPT_DIR 重定向），
绝不触碰真实 ac_user_pref.json；不调 load_learned/save_learned/current_price。
"""

import json
import os
import sys
import tempfile

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
print("v8.37 comfort_weight 学习方向 — 回归测试")
print("=" * 70)

TMP = tempfile.mkdtemp(prefix="cw_dir_")
_orig_script_dir = A.SCRIPT_DIR
A.SCRIPT_DIR = TMP


def pref_path():
    return os.path.join(TMP, "ac_user_pref.json")


def run_manual(mode, pref_log_mode, start_cw):
    """构造 manual_log 后跑一轮 _learn_from_manual，返回文件里的新 comfort_weight。

    pref_log_mode: 预置 manual_log 里 9 条的 mode（追加的第 10 条用 state 的 mode）。
    """
    with open(pref_path(), "w", encoding="utf-8") as f:
        json.dump({"comfort_weight": start_cw}, f, ensure_ascii=False)
    log = [{"ts": f"2026-08-29T10:{i:02d}:00", "rh": 62, "mode": pref_log_mode}
           for i in range(9)]
    state = {"rh_history": [["2026-08-29T11:00:00", 62.0]],
             "user_pref": {"manual_on_log": log},
             "mode": mode}
    try:
        A._learn_from_manual(state, "2026-08-29T11:01:00")
    finally:
        pass
    with open(pref_path(), encoding="utf-8") as f:
        return json.load(f).get("comfort_weight")


try:
    # ── 核心方向 ──
    got = run_manual("cooling", "cooling", 0.2)
    R_.check("手动开≥3 → comfort_weight 上调（0.2→0.3，更舍得制冷）",
             got == 0.3, f"got={got}")

    got = run_manual("off", "off", 0.5)
    R_.check("手动关≥3 → comfort_weight 下调（0.5→0.4，更省电优先）",
             got == 0.4, f"got={got}")

    # ── 钳位 ──
    got = run_manual("cooling", "cooling", 1.0)
    R_.check("手动开≥3 但已在 1.0 → 钳在 1.0", got == 1.0, f"got={got}")

    got = run_manual("off", "off", 0.1)
    R_.check("手动关≥3 但已在 0.1 → 钳在 0.1", got == 0.1, f"got={got}")

    # ── 反方向不再发生（旧 bug 的两条死亡路径）──
    got = run_manual("cooling", "cooling", 0.2)
    R_.check("手动开≥3 不再下调（旧 bug 会 0.2→0.1）", got == 0.3, f"got={got}")

    got = run_manual("off", "off", 0.2)
    R_.check("手动关≥3 不再上调（旧 bug 会 0.2→0.3）", got == 0.1, f"got={got}")

    # ── 阈值不足 3 条 → 不动 ──
    with open(pref_path(), "w", encoding="utf-8") as f:
        json.dump({"comfort_weight": 0.4}, f, ensure_ascii=False)
    log = [{"ts": f"2026-08-29T10:{i:02d}:00", "rh": 62, "mode": "cooling"}
           for i in range(1)]  # 只有 1 条 on
    state = {"rh_history": [["2026-08-29T11:00:00", 62.0]],
             "user_pref": {"manual_on_log": log}, "mode": "off"}
    A._learn_from_manual(state, "2026-08-29T11:01:00")
    with open(pref_path(), encoding="utf-8") as f:
        got = json.load(f).get("comfort_weight")
    R_.check("on=1/off=1 都不足 3 条 → comfort_weight 不动", got == 0.4, f"got={got}")

    # ── 并列 ≥3：on 分支优先（if/elif 顺序，取更 comfort 侧）──
    got = run_manual("cooling", "off", 0.4)   # 9 off + 1 cooling → on=1? 不成立
    # 上面这组实际 on=1 off=9 → 走 off 分支下调：
    R_.check("on=1/off=9 → 走 off 分支下调（0.4→0.3）", got == 0.3, f"got={got}")

    # 真·并列：5 on + 5 off（log 9 条 off + 追加 1 条 cooling = 8/1... 构造 4 off + log 用 state 追加）
    with open(pref_path(), "w", encoding="utf-8") as f:
        json.dump({"comfort_weight": 0.4}, f, ensure_ascii=False)
    log = [{"ts": f"2026-08-29T10:{i:02d}:00", "rh": 62, "mode": m}
           for i, m in enumerate(["off", "cooling", "off", "cooling",
                                  "off", "cooling", "off", "cooling", "off"])]
    # log: off=5, on=4；追加 1 条 cooling → recent10 = off=4, on=6 → on 分支
    state = {"rh_history": [["2026-08-29T11:00:00", 62.0]],
             "user_pref": {"manual_on_log": log}, "mode": "cooling"}
    A._learn_from_manual(state, "2026-08-29T11:01:00")
    with open(pref_path(), encoding="utf-8") as f:
        got = json.load(f).get("comfort_weight")
    R_.check("on=6/off=4 并列超阈 → on 分支优先上调（0.4→0.5）",
             got == 0.5, f"got={got}")
finally:
    A.SCRIPT_DIR = _orig_script_dir

print("\n" + "=" * 70)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 70)
sys.exit(1 if R_.fail else 0)
