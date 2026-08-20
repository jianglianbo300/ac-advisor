#!/usr/bin/env python3
"""换气提醒夜间静默 — 回归测试（2026-08-13 审计遗留待办）。

问题：alert_check 只避开 08:00-08:30，build_rows 收全 24 小时不过滤夜间。
      cron `*/5` 全天跑 → 凌晨的干燥窗口会推微信 + 弹 toast + 自动停空调，把人叫醒。

夜间静默要求：
  - 静默时段内（默认 22:00-07:00）不发提前提醒、不启动换气周期、不自动停空调
  - 已在进行中的换气周期不受影响：进入静默也要正常提醒关窗（否则窗户开一整夜）
  - 静默时段内不选夜间窗口，早上解除后正常工作

全部 monkeypatch，不动真空调、不发真通知。
"""
import sys
from datetime import datetime, timedelta

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


def make_weather(good_hours, today=None):
    """构造 hourly 预报：good_hours 里的小时是干燥好窗口，其余潮湿不可开。"""
    today = today or datetime.now().date().isoformat()
    times, rh, pp, rain, temp, wind, gust, wdir = [], [], [], [], [], [], [], []
    for hr in range(24):
        times.append(f"{today}T{hr:02d}:00")
        good = hr in good_hours
        rh.append(35 if good else 92)
        pp.append(0 if good else 90)
        rain.append(0.0 if good else 5.0)
        temp.append(24.0 if good else 27.0)
        wind.append(12.0)
        gust.append(18.0)
        wdir.append(0)
    return {
        "hourly": {
            "time": times, "relative_humidity_2m": rh,
            "precipitation_probability": pp, "precipitation": rain,
            "temperature_2m": temp, "wind_speed_10m": wind,
            "wind_gusts_10m": gust, "wind_direction_10m": wdir,
        },
        "current": {"temperature_2m": 24.0, "relative_humidity_2m": 35},
        "daily": {"precipitation_probability_max": [0]},
    }


def patch(vent_state, ac_off_calls):
    orig = {
        "load_vent_cycle": H.load_vent_cycle,
        "save_vent_cycle": H.save_vent_cycle,
        "read_ac_state": H.read_ac_state,
        "fetch_aqi": H.fetch_aqi,
        "load_state": H.load_state,
        "_vent_off_ac": None,  # removed: home_living 不控空调
    }
    box = dict(vent_state)
    # load_vent_cycle 必须返回副本：真实实现每次读文件得到新 dict，
    # 若返回 box 本身，save_vent_cycle 的 clear() 会先清空调用方持有的同一对象。
    H.load_vent_cycle = lambda: dict(box)

    def _save(s):
        box.clear()
        box.update(s)

    H.save_vent_cycle = _save
    H.read_ac_state = lambda: "off"
    H.fetch_aqi = lambda *a, **k: {}
    H.load_state = lambda: {}
    def _off(state, ts):
        ac_off_calls.append(ts)
        return "（已自动停空调）"
    H._vent_off_ac = _off

    def restore():
        for k, v in orig.items():
            setattr(H, k, v)
    return box, restore


def step(now, good_hours, vent_state=None):
    """跑一次 vent_cycle_step，返回 (输出文本, 结束后的状态, 停空调调用次数)"""
    ac_calls = []
    box, restore = patch(vent_state or {}, ac_calls)
    try:
        wx = make_weather(good_hours, now.date().isoformat())
        out = H.vent_cycle_step(wx, 26.0, 62.0, now)
    finally:
        restore()
    return out, dict(box), len(ac_calls)


TODAY = datetime.now().date()


def at(hr, mi=0):
    return datetime(TODAY.year, TODAY.month, TODAY.day, hr, mi)


print("=" * 68)
print("换气提醒夜间静默 — 回归测试")
print("=" * 68)

# ── 1. 凌晨好窗口：提前提醒必须静默 ──
print("\n[1] 凌晨 02:30，03:00 是好窗口 → 提前提醒应静默")
out, st, n_off = step(at(2, 30), good_hours={3})
R_.check("无提醒输出", out == "", f"got={out!r}")
R_.check("未记 last_pre（不消耗窗口）", "last_pre" not in st, f"st={st}")
R_.check("未停空调", n_off == 0)

# ── 2. 凌晨窗口到点：不得启动周期、不得停空调 ──
print("\n[2] 凌晨 03:00 窗口到点 → 不启动周期、不停空调")
out, st, n_off = step(at(3, 0), good_hours={3})
R_.check("无提醒输出", out == "", f"got={out!r}")
R_.check("未启动换气周期", not st.get("notified_start"), f"st={st}")
R_.check("未停空调（关键：夜间不动空调）", n_off == 0)

# ── 3. 深夜 23:30 同样静默 ──
print("\n[3] 23:40 好窗口（次日 00:00 前 90 分钟内）→ 静默")
out, st, n_off = step(at(23, 40), good_hours={23})
R_.check("无提醒输出", out == "", f"got={out!r}")
R_.check("未停空调", n_off == 0)

# ── 4. 白天窗口正常工作（静默不能误伤白天）──
print("\n[4] 白天 14:30，15:00 好窗口 → 正常提前提醒")
out, st, n_off = step(at(14, 30), good_hours={15})
R_.check("有提前提醒", "换气提醒" in out, f"got={out!r}")
R_.check("记录 last_pre", st.get("last_pre") is not None)
R_.check("提前提醒不停空调", n_off == 0)

# ── 5. 白天窗口到点：启动周期 + 停空调 ──
print("\n[5] 白天 15:00 到点 → 启动周期 + 停空调")
out, st, n_off = step(at(15, 0), good_hours={15})
R_.check("提醒开窗", "通风时间到" in out, f"got={out!r}")
R_.check("周期已启动", st.get("notified_start") is True)
R_.check("已停空调", n_off == 1)

# ── 6. 进行中的周期跨入静默 → 仍须提醒关窗（不能让窗户开一夜）──
print("\n[6] 21:50 启动的周期，22:10 到期 → 静默期内仍提醒关窗")
end = at(22, 10)
running = {
    "date": TODAY.isoformat(), "window_hr": 21,
    "started_ts": at(21, 50).isoformat(timespec="seconds"),
    "dur_min": 20, "end_ts": end.isoformat(timespec="seconds"),
    "notified_start": True, "notified_end": False, "start_hum": 70.0,
}
out, st, n_off = step(at(22, 10), good_hours={21}, vent_state=running)
R_.check("静默期内仍提醒关窗（安全兜底）", "换气结束" in out, f"got={out!r}")
R_.check("标记 notified_end", st.get("notified_end") is True)

# ── 7. 静默期内的天气转差预警也要发（已开窗，属安全类）──
print("\n[7] 静默期内换气中天气转差 → 仍预警提前关窗")
running2 = {
    "date": TODAY.isoformat(), "window_hr": 22,
    "started_ts": at(22, 5).isoformat(timespec="seconds"),
    "dur_min": 40, "end_ts": at(22, 45).isoformat(timespec="seconds"),
    "notified_start": True, "notified_end": False, "start_hum": 70.0,
}
# 22 点设成坏天气（不在 good_hours）触发 warned_bad
out, st, n_off = step(at(22, 20), good_hours={3}, vent_state=running2)
R_.check("发出天气转差预警", "天气转差" in out, f"got={out!r}")

# ── 8. 早上 07:00 解除静默 ──
print("\n[8] 07:30，08:00 好窗口 → 静默已解除，正常提醒")
out, st, n_off = step(at(7, 30), good_hours={8})
R_.check("静默已解除，有提醒", "换气提醒" in out, f"got={out!r}")

# ── 9. 边界：静默期窗口不被预告；21:00 窗口正常 ──
print("\n[9] 边界：21:30 不预告 22:00（静默期）窗口；22:00 到点静默")
out_a, _, _ = step(at(21, 30), good_hours={22})
R_.check("21:30 不预告静默期窗口（避免提醒了却不执行）", out_a == "", f"got={out_a!r}")
out_c, st_c, n_off_c = step(at(20, 30), good_hours={21})
R_.check("21:00 窗口仍可预告（静默前）", "换气提醒" in out_c, f"got={out_c!r}")
out_b, _, n_off_b = step(at(22, 0), good_hours={22})
R_.check("22:00 已静默", out_b == "", f"got={out_b!r}")
R_.check("22:00 未停空调", n_off_b == 0)

# ── 10. 常量与实现存在性断言 ──
print("\n[10] 夜间静默实现断言")
R_.check("定义 VENT_QUIET_START", hasattr(H, "VENT_QUIET_START"))
R_.check("定义 VENT_QUIET_END", hasattr(H, "VENT_QUIET_END"))
R_.check("有 in_quiet_hours 判定函数", callable(getattr(H, "in_quiet_hours", None)))
if callable(getattr(H, "in_quiet_hours", None)):
    R_.check("in_quiet_hours(3) True", H.in_quiet_hours(at(3, 0)) is True)
    R_.check("in_quiet_hours(14) False", H.in_quiet_hours(at(14, 0)) is False)
    R_.check("in_quiet_hours(22) True（跨午夜区间）", H.in_quiet_hours(at(22, 0)) is True)
    R_.check("in_quiet_hours(7) False（端点解除）", H.in_quiet_hours(at(7, 0)) is False)

print("\n" + "=" * 68)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 68)
sys.exit(1 if R_.fail else 0)
