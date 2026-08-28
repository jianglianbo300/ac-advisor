#!/usr/bin/env python3
"""凉快天气不用开空调 — 回归测试"""
import sys
sys.path.insert(0, r"D:\work\ac-advisor")
from unittest import mock
import ac_advisor as A
import ac_watch as W

# 凉快天气 24°C/60%：低于启动线 27°C，不应开机
with mock.patch.object(A, "current_price", return_value=A.ELECTRIC_VALLEY), \
     mock.patch.object(A, "load_learned", return_value={"adjusted_thresholds":{"temp_cooling":0},"decision_log":[]}):
    r = W.decide(24, 60, False, None, None, False, "off", None, None, 26, None, None, False, None, None)
    assert r[0] is None, f"24°C 不应开机，got {r}"
    print("PASS: 24°C/60% 不启动制冷")
print("✅ 凉快天气下系统不会建议开机")
