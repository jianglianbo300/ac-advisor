#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Home Living Unified Advisor v11.2 - Shanghai Minhang
Merges vent_reminder v2.2 + home_living into a single ventilation/weather/reminder module.

v11.2 changes:
- Removed ~600 lines of duplicated infrastructure code (now imports from ac_advisor)
- Ventilation ACH model: wind pressure + stack effect, mix efficiency 0.7

"""

import gzip
import json
import math
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timezone, timedelta

from ac_advisor import (
    load_learned, save_learned, evaluate_and_learn, log_decision,
    fetch_weather, read_indoor, filter_clean_reminder, weather_cn,
    load_state, LAT, LON,
)

# -- Ensure miio is findable (cron may use python3.11, miio installed in 3.12) --
_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]
for _p in _MIIO_PATHS:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# -- Vent reminder constants --
BASE_ACH = 47.0
BASE_WIND = 6.9  # 4-window cross-vent: 6.9 m/s -> ACH 47
MIX_EFF = 0.7    # cross-vent short-circuit loss
SAFETY = 1.2     # duration safety factor

# Hard gate thresholds
RAIN_PP_MAX = 45      # rain prob >= 45% -> no open
RAIN_MM_MAX = 1.0     # rain intensity > 1mm/h -> no open
DEW_DELTA_MAX = 3.0   # outdoor dewpoint - indoor >= 1.5C -> moisture ingress
PM25_MAX = 75         # PM2.5 >= 75 ug/m3
WIND_MAX_MS = 10.8    # sustained wind >= 6级 (10.8 m/s)
GUST_MAX_MS = 15.0    # gust >= 15 m/s
AC_BLOCK_MODES = ("cooling", "dehumid", "dehumid_alert")

# -- Quiet hours (2026-08-13 audit leftover) --
VENT_QUIET_START = 22   # inclusive, 22:00 -> quiet
VENT_QUIET_END = 7      # exclusive, 07:00 -> active again

# -- Shared state files --
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "home_state.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")
LEARN_FILE = os.path.join(SCRIPT_DIR, "ac_learned.json")
EVAL_DELAY_MIN = 30
EVAL_STALE_MIN = 120
ERR_STATE_FILE = os.path.join(SCRIPT_DIR, "vent_error_state.json")

# -- Weather API (QW CMA) --
def _load_env():
    """Read .env next to this script (gitignored); keys stay out of the source."""
    f = os.path.join(SCRIPT_DIR, ".env")
    try:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass

_load_env()
QW_HOST = os.environ.get("QW_HOST", "kf54e6wb7f.re.qweatherapi.com")
QW_KEY = os.environ.get("QW_API_KEY", "")


# ============================================================
# AC control stub (home_living does NOT control AC)
# ============================================================
def read_ac_state():
    """Read AC state from home_state.json: returns mode or None."""
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        return st.get("mode")
    except Exception:
        return None


def dew_point(temp_c, rh):
    """Magnus formula approximate dewpoint (C)."""
    if temp_c is None or rh is None or rh <= 0 or rh > 100:
        return None
    a, b = 17.62, 243.12
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    return (b * gamma) / (a - gamma)


def fetch_aqi(days=1):
    """Free PM2.5 hourly forecast (Open-Meteo Air Quality). Returns None on failure."""
    try:
        url = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={LAT}&longitude={LON}"
               f"&hourly=pm2_5&forecast_days={days}&timezone=Asia%2FShanghai")
        d = json.load(urllib.request.urlopen(url, timeout=20))
        return dict(zip(d["hourly"]["time"], d["hourly"]["pm2_5"]))
    except Exception:
        return None


def ach(w_kmh, dt=0):
    """ACH model: wind pressure term (calibrated 47@6.9m/s) +
    stack effect term (temp delta chimney), orthogonal sum, * mix efficiency."""
    w = w_kmh / 3.6
    ach_w = BASE_ACH * w / BASE_WIND
    ach_stack = 0.0
    if dt is not None and abs(dt) >= 4:
        ach_stack = 2.5 * (abs(dt) / 8.0) ** 0.5
    return max(0.8, (ach_w ** 2 + ach_stack ** 2) ** 0.5) * MIX_EFF


def t95(w_kmh, dt=0):
    """95% ventilation duration (minutes), with safety factor."""
    a = ach(w_kmh, dt)
    return 3.0 / a * 60.0 * SAFETY if a >= 0.5 else 999.0


def gate_check(rh, pp, rain_mm, temp, wind_ms, gust_ms, pm25,
               indoor_temp, indoor_rh):
    """Unified decision gate. Returns (ok: bool, reason: str|None)."""
    if pp is not None and pp >= RAIN_PP_MAX:
        return False, f"降雨概率 {pp}%"
    if rain_mm is not None and rain_mm > RAIN_MM_MAX:
        return False, f"降雨 {rain_mm}mm/h"
    if wind_ms is not None and wind_ms >= WIND_MAX_MS:
        return False, f"持续风 {wind_ms}m/s (≥6级)"
    if gust_ms is not None and gust_ms >= GUST_MAX_MS:
        return False, f"阵风 {gust_ms}m/s"
    if pm25 is not None and pm25 >= PM25_MAX:
        return False, f"PM2.5 {pm25}ug/m³"
    # Use 'temp' parameter (outdoor temp), not 'out_temp'
    if indoor_temp is not None and indoor_rh is not None and temp is not None:
        dp_in = dew_point(indoor_temp, indoor_rh)
        dp_out = dew_point(temp, rh) if rh is not None else None
        if dp_in is not None and dp_out is not None:
            if dp_out - dp_in >= DEW_DELTA_MAX:
                return False, f"室外露点比室内高 {dp_out - dp_in:.1f}°C，开窗湿气灌入"
    return True, None


def vent_advice(now_rh, out_rh, now_temp=None, out_temp=None):
    """Indoor/outdoor comparison + AC linkage advice."""
    lines = []
    if out_rh is None:
        return lines
    if now_rh is not None:
        if now_rh >= 70:
            lines.append("🥵 室内偏闷，建议短促换气")
        elif now_rh >= 60:
            lines.append("🟡 室内一般，可选择性开窗")
        else:
            lines.append("🟢 室内舒适")
    if now_temp is not None and out_temp is not None:
        if out_temp >= 35:
            lines.append("🥵 室外炎热，不建议开窗")
        elif out_temp >= now_temp - 2:
            lines.append("🟡 室外温度不低，短换即可")
    return lines


def verdict(rh):
    """Ventilation verdict from RH."""
    if rh < 60:  return ("极佳", "🟢")
    if rh < 70:  return ("好", "🟢")
    if rh < 78:  return ("一般", "🟡")
    if rh < 85:  return ("偏差", "🟠")
    return ("不推荐", "🔴")


WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def wind_dir_cn(deg):
    """Wind direction degrees -> Chinese compass."""
    if deg is None:
        return None
    dirs = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]
    return dirs[int((deg % 360) / 45) % 8] + "风"


def build_rows(h, today):
    """Build candidate rows from hourly forecast for today."""
    rows = []
    for i, t in enumerate(h["time"]):
        hr = int(t[11:13])
        if t < today:
            continue
        if t > today and hr < 8:
            continue
        rh = h["relative_humidity_2m"][i]
        pp = h["precipitation_probability"][i]
        temp = h["temperature_2m"][i]
        rows.append({
            "hr": hr, "rh": rh, "pp": pp, "rain_mm": 0,
            "temp": temp, "wind_kmh": 0, "wind_ms": 0,
            "wind_dir": None,
        })
    return rows
def pick_best(rows, indoor_temp, indoor_rh, ac_mode, aqi):
    """Unified gate filter + dew-point delta sort."""
    cand = []
    for r in rows:
        ok, reason = gate_check(r["rh"], r["pp"], r["rain_mm"], r["temp"],
                                r["wind_ms"], 0,
                                aqi.get(f"{r['hr']:02d}:00") if aqi else None,
                                indoor_temp, indoor_rh)
        if not ok:
            continue
        if ac_mode in AC_BLOCK_MODES and r["pp"] >= 40:
            continue
        cand.append(r)
    if not cand:
        return None, None
    today = date.today().isoformat()
    cand.sort(key=lambda r: (
        -r["rh"],
        r["pp"],
        r["rain_mm"],
    ))
    return cand[0], None


def daily_report():
    """Daily ventilation report (08:00 mode)."""
    data = fetch_weather()
    if "error" in data:
        return notify_error_once("weather", data["error"])
    h = data.get("hourly", {})
    if not h or not h.get("time"):
        return notify_error_once("hourly", "no hourly data")
    today = date.today().isoformat()
    rows = build_rows(h, today)
    if not rows:
        return "⚠️ 预报数据为空，今日换气提醒无法生成"
    indoor_temp, indoor_hum = read_indoor()
    ac_mode = read_ac_state()
    aqi = fetch_aqi(1)
    best, blocked = pick_best(rows, indoor_temp, indoor_hum, ac_mode, aqi)

    lines = []
    lines.append(f"🌬️ 今日换气提醒 ({today} {WD[date.today().weekday()]})")
    lines.append("─" * 18)
    if best is None:
        # 每日最低换气保障
        best2 = None
        for r in rows:
            if r["pp"] >= 70 or r["rain_mm"] > 2.0:
                continue
            if r["wind_kmh"] / 3.6 >= 12:
                continue
            if r["rh"] >= 95:
                continue
            best2 = r
            break
        if best2 is None:
            lines.append("🚫 今日极端天气，无法换气")
            lines.append(f"   原因: {blocked}")
            if indoor_temp is not None:
                lines.append(f"   📍 室内实测: {indoor_temp}°C / {indoor_hum:.0f}%")
            lines.append("─" * 18)
            lines.append("🌧 今天天气极端，关窗靠空调/除湿机")
            return "\n".join(lines)
        dt = (best2["temp"] - indoor_temp) if indoor_temp is not None else 0
        dur = min(15.0, t95(best2["wind_kmh"], dt))
        lines.append("⚡ 每日最低换气（天气一般，短促换气）")
        lines.append(f"   ⏱ {dur:.0f} 分钟（{min(15, int(dur))}分钟后关窗）")
        lines.append(f"   📍 {best2['hr']:02d}:00  风{best2['wind_kmh']:.0f}km/h RH{best2['rh']}%")
        if indoor_temp is not None:
            lines.append(f"   📍 室内实测: {indoor_temp}°C / {indoor_hum:.0f}%")
        lines.append("─" * 18)
        lines.append("💡 关窗后开空调除湿，短促换气不浪费")
        return "\n".join(lines)

    vv, emoji = verdict(best["rh"])
    dt = (best["temp"] - indoor_temp) if indoor_temp is not None else 0
    dur = t95(best["wind_kmh"], dt)
    pm25 = aqi.get(f"{today}T{best['hr']:02d}:00") if aqi else None
    dur_s = f"约 {dur:.0f} 分钟" if dur <= 90 else "风小，配风扇 ~10-15 分钟"
    rain = "☔有雨" if best["pp"] >= 40 else ("🌦有雨概率" if best["pp"] >= 20 else "☀无雨")
    if indoor_temp is None:
        lines.append("   ⚠️ 室内无实时读数，露点/湿度防潮未校验——此窗口仅供参考，开窗前请先确认室外不潮，仅短促换气")
    lines.append(f"🏆 最佳窗口: {best['hr']:02d}:00  RH{best['rh']}% {rain}{best['pp']}%")
    lines.append(f"   温度{best['temp']}°C 风{best['wind_kmh']:.0f}km/h → {emoji}{vv}")
    _wd = wind_dir_cn(best.get("wind_dir"))
    if _wd:
        lines.append(f"   🧭 {_wd}{best.get('wind_dir')}° → 开迎风1-2扇+背风2扇, 4窗全开最畅")
    if pm25 is not None:
        lines.append(f"   🍃 PM2.5 {pm25:.0f}µg/m³ {'✅' if pm25 < 35 else ('🟡' if pm25 < 75 else '❌')}")
    lines.append(f"   ⏱ 建议时长: {dur_s}")
    lines.append(f"   操作: 4窗全开+房门全开, 到点关")
    lines.append(f"   🤖 到点 {best['hr']:02d}:00 系统自动停空调并提醒你开窗，计时到点再提醒你关窗")
    if indoor_temp is not None:
        lines.append(f"   📍 室内实测: {indoor_temp}°C / {indoor_hum:.0f}%")
    for x in vent_advice(indoor_hum, best["rh"], indoor_temp, best["temp"]):
        lines.append(f"   {x}")
    lines.append("─" * 18)
    if best["rh"] >= 85:
        lines.append("❌ 今天全天高湿/降雨，不宜开窗")
        lines.append("   除湿请关窗靠空调/除湿机")
    elif best["rh"] < 70:
        lines.append(f"✅ 今天可以开窗{emoji}，甚至顺带除湿")
    elif best["rh"] < 78:
        lines.append(f"🟡 今天湿度一般，短换即可")
    else:
        lines.append(f"🟠 今天湿度偏高，只在 {best['hr']:02d}:00 前后快速换气")
    lines.append("─" * 18)
    return "\n".join(lines)


# ============================================================
# Auto vent cycle: stop AC -> remind open -> timed close
# ============================================================
VENT_CYCLE_FILE = os.path.join(SCRIPT_DIR, "vent_cycle_state.json")


def load_vent_cycle() -> dict:
    try:
        if os.path.exists(VENT_CYCLE_FILE):
            with open(VENT_CYCLE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def save_vent_cycle(d: dict):
    """Persist vent cycle state (atomic write: tmp -> rename)."""
    tmp = VENT_CYCLE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, VENT_CYCLE_FILE)
    except Exception:
        pass

def in_quiet_hours(now=None):
    """True inside the no-disturb window (default 22:00-07:00, wraps midnight)."""
    if now is None:
        now = datetime.now()
    hr = now.hour
    return hr >= VENT_QUIET_START or hr < VENT_QUIET_END


def vent_cycle_step(data, indoor_temp, indoor_hum, now=None):
    """Full auto vent cycle."""
    if now is None:
        now = datetime.now()
    today = now.date().isoformat()
    st = load_vent_cycle()
    now_min = now.hour * 60 + now.minute

    # -- Venting in progress -> end reminder / early-weather warning --
    if st.get("notified_start") and not st.get("notified_end"):
        try:
            end_ts = datetime.fromisoformat(st["end_ts"])
        except Exception:
            save_vent_cycle({})
            return ""
        if now < end_ts and not st.get("warned_bad"):
            _h = (data.get("hourly", {}) or {})
            if _h.get("time"):
                for _r in build_rows(_h, today):
                    if _r["hr"] == now.hour:
                        _bad = ((_r["pp"] is not None and _r["pp"] >= RAIN_PP_MAX)
                                or (_r["rain_mm"] is not None and _r["rain_mm"] > RAIN_MM_MAX))
                        if _bad:
                            st["warned_bad"] = True
                            save_vent_cycle(st)
                            return (f"🌧 换气中天气转差（{now.hour:02d}:00 降雨概率{_r['pp']}%/"
                                    f"降水{_r['rain_mm']:.1f}mm）→ 建议提前关窗，防雨水湿气飘入")
                        break
        if now >= end_ts:
            st["notified_end"] = True
            st["ended_ts"] = now.isoformat(timespec="seconds")
            save_vent_cycle(st)
            _sh = st.get("start_hum")
            _hum_note = ""
            if _sh is not None and indoor_hum is not None:
                _d = _sh - indoor_hum
                if _d >= 5:
                    _hum_note = f"（室内湿度 {_sh:.0f}%→{indoor_hum:.0f}%，降 {_d:.0f}pp ✅）"
                elif indoor_hum <= 60:
                    _hum_note = f"（室内湿度 {_sh:.0f}%→{indoor_hum:.0f}%，已到舒适区 ✅）"
                elif _d < 0:
                    _hum_note = f"（⚠️ 湿度反升 {_sh:.0f}%→{indoor_hum:.0f}%，室外湿气可能灌入，尽快关）"
                else:
                    _hum_note = f"（室内湿度 {_sh:.0f}%→{indoor_hum:.0f}%）"
            return (f"⏰ 换气结束（{st.get('dur_min', '?')} 分钟到，{end_ts.strftime('%H:%M')}）{_hum_note}\n"
                    "   请关窗。关窗后如需可再开空调；要我帮你恢复就说一声。")
        return ""

    # -- Quiet hours: no new cycle, no AC action, no notification --
    if in_quiet_hours(now):
        return ""

    h = data.get("hourly", {}) or {}
    if not h or not h.get("time"):
        return ""
    rows = build_rows(h, today)
    if not rows:
        return ""
    upcoming = [r for r in rows
               if now_min <= r["hr"] * 60 <= now_min + 90
               and not in_quiet_hours(now.replace(hour=r["hr"], minute=0))]
    if not upcoming:
        return ""

    # -- Pick best upcoming window --
    indoor_rh = indoor_hum
    ac_mode = read_ac_state()
    aqi = fetch_aqi(1)
    best, blocked = pick_best(upcoming, indoor_temp, indoor_rh, ac_mode, aqi)
    if best is None:
        return ""

    # -- Idle -> heads-up --
    if not st.get("notified_start"):
        st["notified_start"] = True
        st["start_hum"] = indoor_hum
        st["date"] = today
        save_vent_cycle(st)
        return (f"📢 下一换气窗口 {best['hr']:02d}:00（RH{best['rh']}% {best['temp']}°C）\n"
                "   请留意：到点系统会自动停空调并提醒你开窗")

    # -- Not yet at window start: silent --
    if now.hour < best["hr"]:
        return ""

    # -- Window start: stop AC + remind open --
    if now.hour == best["hr"] and not st.get("notified_open"):
        st["notified_open"] = True
        save_vent_cycle(st)
        dt = (best["temp"] - indoor_temp) if indoor_temp is not None else 0
        dur = t95(best["wind_kmh"], dt)
        end_dt = now + timedelta(minutes=dur)
        save_vent_cycle({
            "date": today,
            "start_hum": indoor_hum,
            "dur_min": int(dur),
            "end_ts": end_dt.isoformat(timespec="seconds"),
            "notified_start": True,
            "notified_open": True,
        })
        return (f"🕐 {now.strftime('%H:%M')} 换气窗口已到\n"
                f"   请开窗换气约 {dur} 分钟，到 {end_dt.strftime('%H:%M')} 提醒你关窗")

    return ""


def alert_check():
    """Alert mode: full auto vent cycle."""
    now = datetime.now()
    if now.hour == 8 and now.minute < 30:
        return ""
    data = fetch_weather()
    if "error" in data:
        return ""
    indoor_temp, indoor_hum = read_indoor()
    return vent_cycle_step(data, indoor_temp, indoor_hum, now)


def notify_error_once(key, detail):
    """Error silence: alert mode silent; daily mode same error 24h max 1."""
    if "--alert" in sys.argv:
        return ""
    try:
        if os.path.exists(ERR_STATE_FILE):
            with open(ERR_STATE_FILE, encoding="utf-8") as f:
                es = json.load(f)
        else:
            es = {}
    except Exception:
        es = {}
    now = datetime.now()
    if key in es:
        try:
            last = datetime.fromisoformat(es[key])
            if (now - last).total_seconds() < 86400:
                return ""
        except Exception:
            pass
    es[key] = now.isoformat(timespec="seconds")
    try:
        with open(ERR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(es, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return f"⚠️ 换气提醒数据异常（{detail[:100]}）→ 今日暂停，明早 08:00 恢复"


def notify_windows(title, text):
    """Windows toast notification (parallel with WeChat)."""
    try:
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$text = $t.GetElementsByTagName('text');"
            "$text.Item(0).AppendChild($t.CreateTextNode('{0}')) > $null;"
            "$text.Item(1).AppendChild($t.CreateTextNode('{1}')) > $null;"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PiAgent').Show($toast)"
        ).format(title, text.replace("'", "").replace('"', ''))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=10, capture_output=True)
    except Exception:
        pass



def main():
    """Home living main: ventilation/weather/reminder only (no AC control)."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # -- 0. Mode dispatch ---------------------------------------------------
    if "--alert" in sys.argv:
        _alert_text = alert_check()
        print(_alert_text)
        if _alert_text:
            notify_windows("🌬 换气提醒", _alert_text)
        return

    if "--daily" in sys.argv or "--report" in sys.argv:
        _report = daily_report()
        print(_report)
        _toast = "\n".join(_report.splitlines()[:3])
        notify_windows("🌬 今日换气", _toast)
        return

    # -- 1. Fetch weather, read indoor --
    wx = fetch_weather()
    if "error" in wx:
        print(f"⚠️ 天气API失败: {wx['error']}")
        print("🏠 上海闵行 · 家居生活顾问")
        print("  数据不可用，请稍后再查")
        return

    indoor_temp, indoor_hum = read_indoor()

    # -- 2. Build output --
    cur = wx.get("current", {})
    dai = wx.get("daily", {})
    temp = cur.get("temperature_2m", 0)
    feels = cur.get("apparent_temperature", 0)
    hum_out = cur.get("relative_humidity_2m")
    wcode = cur.get("weather_code", 0)
    max_t = dai.get("temperature_2m_max", [0])[0]
    rain = dai.get("precipitation_probability_max", [0])[0]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = []
    lines.append(f"🏠 上海闵行 · 家居生活顾问 v11.2")
    lines.append(f"📅 {now_str} · {weather_cn(wcode)}")
    lines.append("")
    lines.append(f"  室外: {temp:.1f}°C  体感: {feels:.1f}°C  湿度: {hum_out:.0f}%")
    if indoor_temp is not None:
        lines.append(f"  室内: {indoor_temp:.1f}°C  湿度: {indoor_hum:.0f}%")
    else:
        lines.append("  室内传感器不可用")
    lines.append(f"  今日最高: {max_t:.1f}°C  降雨: {rain:.0f}%")
    lines.append("")

    # -- 3. Ventilation report --
    try:
        _daily_vent = daily_report()
        if _daily_vent:
            for _vl in _daily_vent.splitlines():
                lines.append(f"  {_vl}")
            lines.append("")
    except Exception:
        pass

    # -- 4. Vent advice --
    vent_lines = vent_advice(indoor_hum, hum_out, indoor_temp,
                             wx.get("current", {}).get("temperature_2m"))
    if vent_lines:
        lines.append("  🌬 即时通风建议:")
        for vl in vent_lines:
            lines.append(f"     {vl}")
    lines.append("")

    # -- 5. Window close reminder --
    if rain >= 45:
        lines.append("  ⚠️ 今日有雨，请勿开窗（防潮）")
    elif hum_out is not None and hum_out >= 85:
        lines.append(f"  ⚠️ 室外潮湿({hum_out:.0f}%)，请勿开窗（防潮）")

    # -- 6. Filter reminder --
    reminder = filter_clean_reminder()
    if reminder:
        lines.append(reminder)
    lines.append("")
    lines.append("─" * 40)
    lines.append("数据: 和风天气(CMA) + Open-Meteo空气质量 + 小米净化器4Lite")

    print("\n".join(lines))

    # Windows toast notification
    notify_windows("🏠 家居生活顾问", "通风/换气提醒生成")


if __name__ == "__main__":
    main()
