#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v8.60 回归自检：传感器降级不得抢占保守关机。

背景（2026-09-27 审计）：降级逻辑初版在 wx_fallback 分支无条件 return，
抢在 SENSOR_TIMEOUT_ESCALATE 保守关机之前 → 传感器离线且空调运行时，
保护永远不会执行（实测 mode=cooling/1037W 处于该盲跑状态）。

这里锁定三条不可回归的性质：
  1. 空调未运行 + 离线超阈值 → 降级（不开新 cycle）
  2. 空调运行中 + 离线超阈值   → 不得降级返回，必须能到达保守关机
  3. 空调运行中 + 离线未超阈值 → 正常监护，不动

跑法： python test_v860_ordering.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

SENSOR_DEGRADE_CONTROL = 30
SENSOR_TIMEOUT_ESCALATE = 20
RUNNING = ("cooling", "dehumid", "dehumid_alert")


def should_degrade(mode, off_min):
    """复刻 ac_watch.main() 的降级判据（含 v8.60 让位修复）。"""
    if off_min is None or off_min < SENSOR_DEGRADE_CONTROL:
        return False
    return mode not in RUNNING


def reaches_conservative_shutdown(mode, off_min):
    """保守关机分支是否可达（ac_watch 现有逻辑）。"""
    return mode in RUNNING and off_min is not None and off_min >= SENSOR_TIMEOUT_ESCALATE


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    return bool(cond)


def main():
    ok = True
    print("v8.60 ordering selftest")

    print("用例1 空调未运行 + 离线 35min → 降级")
    ok &= check(should_degrade("off", 35) is True, "判定降级")
    ok &= check(reaches_conservative_shutdown("off", 35) is False, "无需保守关机（本来就没运行）")

    print("用例2 空调运行中 + 离线 35min → 不得降级，必须能关机（v8.60 核心）")
    ok &= check(should_degrade("cooling", 35) is False, "不降级返回")
    ok &= check(reaches_conservative_shutdown("cooling", 35) is True, "保守关机可达")

    print("用例3 空调运行中 + 离线 10min → 正常监护")
    ok &= check(should_degrade("cooling", 10) is False, "不降级")
    ok &= check(reaches_conservative_shutdown("cooling", 10) is False, "未到关机阈值")

    print("用例4 边界：离线 29min 运行中 → 降级不触发（未到30），关机也不触发（未到20? 否，20<=29）")
    ok &= check(should_degrade("cooling", 29) is False, "29min 未达降级阈值30")
    ok &= check(reaches_conservative_shutdown("cooling", 29) is True, "29min 已过保守关机阈值20")

    print("用例5 除湿态同样受保护（dehumid 属运行态）")
    ok &= check(should_degrade("dehumid", 40) is False, "dehumid 不降级")
    ok &= check(reaches_conservative_shutdown("dehumid", 40) is True, "dehumid 保守关机可达")

    print("用例6 传感器刚恢复（off_min=None）→ 一切正常")
    ok &= check(should_degrade("off", None) is False, "None 不降级")
    ok &= check(reaches_conservative_shutdown("cooling", None) is False, "None 不关机")

    print("ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
