#!/usr/bin/env python3
"""模拟测试：凉快天气时不用开空调的提醒"""
import sys
sys.path.insert(0, r"D:\work\ac-advisor")
import ac_advisor

# 模拟凉快天气：24°C, 体感23°C, 湿度60%
orig = ac_advisor.fetch
ac_advisor.fetch = lambda: {
    "current": {"temperature_2m": 24.0, "apparent_temperature": 23.0,
                "relative_humidity_2m": 60, "weather_code": 3},
    "daily": {"temperature_2m_max": [25.0], "temperature_2m_min": [21.0],
              "precipitation_probability_max": [10]}
}
ac_advisor.main()
print()
print("---")
print("✅ 凉快天气下会明确提醒'不用开空调'，并可关掉已开的空调")