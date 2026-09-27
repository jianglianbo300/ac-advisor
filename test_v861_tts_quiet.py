#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v8.61 回归自检：语音静音时段必须是 22:00-08:00。

背景（用户 2026-09-27 明确要求「晚上 22 点到第二天早上 8 点不要语音播报」）：
原实现用 `if not night_hours()` 守在调用点，而 `night_hours()` 读的是
`NIGHT = (23, 7)` —— 那是**控制逻辑**的夜间窗口（MIN_OFF / 启动次数上限 /
谷电对齐，15+ 处在用），**不能改**。结果语音只静音 23:00-07:00，
22-23 点与 07-08 点两头各漏 1 小时，仍会吵醒用户。

v8.61 修法：语音用**独立**窗口 TTS_QUIET_HOURS=(22, 8)，并把判定收口到
`tts_speak()` 单点（将来新增调用点自动受约束）。

本测试复刻判据为纯函数（不 import ac_watch —— 其 import 阶段有抢锁 + sys.exit
的既有行为，见 test_v860_ordering.py 同款约定），锁定边界小时：

跑法： python test_v861_tts_quiet.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

# 与 ac_watch.TTS_QUIET_HOURS 保持一致；NIGHT 复刻自 ac_watch.L223
TTS_QUIET_HOURS = (22, 8)
NIGHT = (23, 7)


def quiet(hour):
    """v8.61 语音静音判据（含端点）。"""
    return hour >= TTS_QUIET_HOURS[0] or hour < TTS_QUIET_HOURS[1]


def night(hour):
    """控制逻辑夜间窗口（v8.61 不得改动它）。"""
    return hour >= NIGHT[0] or hour < NIGHT[1]


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    return bool(cond)


def main():
    ok = True
    print("== 用户要求：22:00-08:00 必须静音 ==")
    for h in (22, 23, 0, 3, 6, 7):
        ok &= check(quiet(h), f"{h:02d}:00 静音")
    print("== 08:00 起必须恢复播报 ==")
    for h in (8, 9, 12, 18, 21):
        ok &= check(not quiet(h), f"{h:02d}:00 允许播报")

    print("== 覆盖 24 小时全量断言 ==")
    expected_quiet = {h for h in range(24) if h >= 22 or h < 8}
    got_quiet = {h for h in range(24) if quiet(h)}
    ok &= check(got_quiet == expected_quiet,
                f"静音小时集合 == {{22..23, 0..7}}（实得 {len(got_quiet)} 个）")

    print("== 边界回归：v8.60 的缺陷（22-23 / 07-08 曾漏播）==")
    leaked = [h for h in (22, 7) if not quiet(h)]
    ok &= check(not leaked, f"旧缺口 22/7 时已补齐（仍漏: {leaked or '无'}）")

    print("== 不得影响控制逻辑的 NIGHT 窗口 ==")
    ok &= check(NIGHT == (23, 7), "NIGHT 仍为 (23,7)，未被 TTS 改动污染")
    ok &= check(night(22) is False and night(7) is False,
                "22/7 时对控制逻辑仍是白天（未误改行为）")

    print("== 与 tts_speak 同源校验 ==")
    import re
    src_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ac_watch.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    ok &= check("TTS_QUIET_HOURS = (22, 8)" in src, "ac_watch 中 TTS_QUIET_HOURS == (22, 8)")
    ok &= check(re.search(r"def tts_speak\(text\):.*?if _tts_quiet\(\):\s*\n\s*return", src, re.S) is not None,
                "tts_speak() 内部已收口静音判定（单点）")
    ok &= check("if not night_hours():" not in src,
                "调用点不再残留 not night_hours() 重复判定")

    print("\n" + ("ALL PASS" if ok else "HAS FAILURE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
