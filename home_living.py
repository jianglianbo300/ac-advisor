#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Home Living Unified Advisor v11.0 - Shanghai Minhang
Merges vent_reminder v2.2 + home_living into a single ventilation/weather/reminder module.

v11.1 changes:
- Stripped all AC control (delegated to ac_watch.py via ac_advisor.py)
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
from enum import Enum
import ac_advisor
from ac_advisor import load_learned, save_learned, evaluate_and_learn, log_decision

# -- Ensure miio is findable (cron may use python3.11, miio installed in 3.12) --
_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]
for _p in _MIIO_PATHS:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# -- Threshold constants (unified header, configurable) --
# Note: AC control thresholds are now in ac_advisor.py, used by ac_watch.py.
# home_living.py only keeps ventilation-related thresholds.

# -- Vent reminder constants --
LAT, LON = 31.11, 121.38  # Shanghai Minhang
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
# The vent cron runs every 5 min all day and build_rows keeps all 24 hours, so a
# dry 03:00 window used to push WeChat + toast AND auto-stop the AC mid-sleep.
# Inside quiet hours we neither start a cycle nor touch the AC. An already
# running cycle still gets its close reminder / bad-weather warning: leaving the
# windows open all night is worse than one late notification.
VENT_QUIET_START = 22   # inclusive, 22:00 -> quiet
VENT_QUIET_END = 7      # exclusive, 07:00 -> active again

# -- Shared state files --
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "home_state.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")
LEARN_FILE = os.path.join(SCRIPT_DIR, "ac_learned.json")
# v8.23 回评时窗（与 ac_advisor 对齐）：决策后至少等 EVAL_DELAY_MIN 才回评；
# 超过 EVAL_STALE_MIN 视为跨了别的事件，消费但不学习。
EVAL_DELAY_MIN = 30
EVAL_STALE_MIN = 120
ERR_STATE_FILE = os.path.join(SCRIPT_DIR, "vent_error_state.json")

# -- Weather API (QW CMA) --
# Forecast (temp/humidity/precip/wind) comes from QW CMA = China Meteorological
# Administration data. Only PM2.5 uses Open-Meteo. fetch_weather() emits an
# Open-Meteo compatible shape, hence the temperature_2m style field names
# downstream - the data itself is QW.
def _load_env():
    """Read .env next to this script (gitignored); keys stay out of the source."""
    f = os.path.join(SCRIPT_DIR, ".env")
    try:
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_env()
QW_HOST = os.environ.get("QW_HOST", "kf54e6wb7f.re.qweatherapi.com")
QW_KEY = os.environ.get("QW_API_KEY", "")

# -- Measured power globals --
AC_MEASURED_W = None
AC_SOCKET = None
AC_CTRL = None


# ============================================================
# Explicit state machine
# ============================================================
class ACState(Enum):
    """AC running state enumeration."""
    OFF = "off"
    COOLING = "cooling"
    COOLING_MAINTAIN = "cooling_maintain"
    DEHUMID = "dehumid"
    FAN = "fan"
    FAN_LOCKED = "fan_locked"


# Transition table: current -> allowed target set
TRANSITIONS = {
    ACState.OFF: {ACState.COOLING, ACState.DEHUMID, ACState.FAN},
    ACState.COOLING: {ACState.COOLING_MAINTAIN, ACState.OFF, ACState.FAN},
    ACState.COOLING_MAINTAIN: {ACState.OFF, ACState.COOLING},
    ACState.DEHUMID: {ACState.OFF, ACState.FAN},
    ACState.FAN: {ACState.OFF, ACState.COOLING, ACState.DEHUMID},
    ACState.FAN_LOCKED: {ACState.OFF, ACState.FAN, ACState.COOLING},
}


def transition(current: ACState, target: ACState) -> ACState:
    """State transition: legal -> move, illegal -> keep current."""
    allowed = TRANSITIONS.get(current, set())
    if target in allowed:
        return target
    return current


# ============================================================
# Comfort index + wet-bulb temperature
def comfort_index(temp, hum):
    """Comfort index = T + 0.02 * (RH - 10)."""
    if hum is None:
        return temp
    return temp + 0.05 * (hum - 10)

def wet_bulb_temp(temp_c, rh):
    """Wet-bulb temperature (Stull 2011 empirical formula)."""
    if rh is None:
        return temp_c
    rh = rh / 100.0
    t = temp_c
    wb = t * math.atan(0.151977 * (rh + 8.313659) ** 0.5) \
         + math.atan(t + rh) - math.atan(rh - 1.67633) \
         + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) \
         - 4.686035
    return wb



# ============================================================
# Seasonal adaptation
# ============================================================
def seasonal_adjustments():
    """Auto-switch by month:
    Summer(7-8): normal cooling
    Plum rain(6): dehumid priority, temp threshold +1C
    Spring/Autumn(4-5/9-10): fan priority, temp threshold +2C
    Winter(11-3): close windows, no AC
    Returns (temp_offset, hum_offset, strategy_label).
    """
    m = datetime.now().month
    if m in (7, 8):
        return 0, 0, "盛夏制冷"
    elif m == 6:
        return 1, -5, "梅雨除湿优先"
    elif m in (4, 5, 9, 10):
        return 2, 5, "春秋风扇优先"
    else:
        return 4, 0, "冬季关窗优先"


# ============================================================
# Predictive pre-cooling
# ============================================================
def should_precool(wx, current_hi, threshold_hi):
    """If next 3h HI exceeds threshold + 3C, suggest pre-cooling."""
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    hums = hourly.get("relative_humidity_2m", [])
    if not times or not temps:
        return False, None, None
    now_h = datetime.now().hour
    idx = None
    for i, t in enumerate(times):
        if len(t) >= 13 and t[11:13] == f"{now_h:02d}":
            idx = i
            break
    if idx is None:
        return False, None, None
    max_future_hi = current_hi
    for i in range(idx + 1, min(idx + 4, len(temps))):
        t = temps[i]
        h = hums[i] if i < len(hums) else None
        hi = comfort_index(t, h) if h is not None else t
        max_future_hi = max(max_future_hi, hi)
    if max_future_hi >= threshold_hi + 3:
        return True, max_future_hi, idx
    return False, None, None


# ============================================================
# Adaptive threshold learning (persistent)
# ============================================================
def load_thermal_data() -> dict:
    """Load thermal learning data (events + fitted model)."""
    default = {
        "events": [],
        "thermal_model": {
            "cooling_rate_per_min": 0.05,
            "warmup_rate_per_min": 0.02,
            "time_constant_min": 120,
        },
    }
    try:
        if os.path.exists(THERMAL_FILE):
            with open(THERMAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_thermal_data(data: dict):
    """Persist thermal learning data (atomic write)."""
    tmp = THERMAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, THERMAL_FILE)


def record_thermal_event(event_type, temp_before, temp_after=None, duration_min=None, outdoor_temp=None):
    """Record a thermal event: cooling / warming cycle start.

    An event opens with temp_after/duration_min unknown and is closed by the
    next state change: that transition's temp_before is, by definition, the
    previous cycle's end temperature, and the timestamp delta is its duration.
    Without this back-fill every event stayed incomplete forever, so the file
    only ever held unusable rows (and ac_advisor's fitter, which does not
    filter, crashed on the None values)."""
    data = load_thermal_data()
    events = data.get("events", [])
    now = datetime.now()

    # Close the most recent open event using this transition as its end point.
    if events and temp_before is not None:
        prev = events[-1]
        if prev.get("temp_after") is None and prev.get("temp_before") is not None:
            try:
                started = datetime.fromisoformat(prev["timestamp"])
                dur = (now - started).total_seconds() / 60.0
            except Exception:
                dur = None
            # Guard both ends: a sub-minute flap carries no rate signal, and a
            # multi-hour gap means the process was down, not a real cycle.
            if dur is not None and 1.0 <= dur <= 720.0:
                prev["temp_after"] = temp_before
                prev["duration_min"] = int(round(dur))

    events.append({
        "type": event_type,
        "temp_before": temp_before,
        "temp_after": temp_after,
        "duration_min": duration_min,
        "outdoor_temp": outdoor_temp,
        "timestamp": now.isoformat(),
    })
    data["events"] = events[-100:]
    data["thermal_model"] = fit_thermal_model(data["events"])
    save_thermal_data(data)


def fit_thermal_model(events):
    """Fit thermal model from completed events (temp_after + duration known).
    Returns {"cooling_rate_per_min": x, "warmup_rate_per_min": y, "time_constant_min": z}."""
    def _usable(e, kind):
        if e.get("type") != kind:
            return False
        for k in ("temp_before", "temp_after", "duration_min"):
            v = e.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return False
        return e["duration_min"] > 0

    cooling = [e for e in (events or []) if _usable(e, "cooling")]
    warming = [e for e in (events or []) if _usable(e, "warming")]
    model = {
        "cooling_rate_per_min": 0.05,
        "warmup_rate_per_min": 0.02,
        "time_constant_min": 120,
    }
    if cooling:
        rates = [(e["temp_before"] - e["temp_after"]) / max(e["duration_min"], 1)
                 for e in cooling[-20:]]
        # A cycle that never cooled gives rate <= 0; learning it would make
        # predict_cooling_time divide by ~0 and return absurd durations.
        rates = [r for r in rates if r > 0]
        if rates:
            model["cooling_rate_per_min"] = sum(rates) / len(rates)
    if warming:
        rates = [(e["temp_after"] - e["temp_before"]) / max(e["duration_min"], 1)
                 for e in warming[-20:]]
        rates = [r for r in rates if r > 0]
        if rates:
            model["warmup_rate_per_min"] = sum(rates) / len(rates)
    return model


def predict_cooling_time(temp_current, temp_target, outdoor_temp, thermal_model):
    """Predict minutes to cool from temp_current to temp_target."""
    rate = thermal_model.get("cooling_rate_per_min", 0.05)
    diff = temp_current - temp_target
    if diff <= 0:
        return 0
    return int(diff / rate)


# ============================================================
# Original functions (retained)
# ============================================================
def dehumid_duty(temp, hum=None):
    """Fixed freq dehumid duty cycle: temp base * humidity factor."""
    if temp is None:
        base = DEHUMID_DUTY
    elif temp >= 26:
        base = DEHUMID_DUTY
    elif temp <= TEMP_ABSOLUTE_FLOOR:
        base = 0.25
    else:
        base = 0.25 + (DEHUMID_DUTY - 0.25) * (temp - TEMP_ABSOLUTE_FLOOR) / (26 - TEMP_ABSOLUTE_FLOOR)
    if hum is not None:
        if hum >= 85:
            factor = 1.10
        elif hum <= 60:
            factor = 0.70
        else:
            factor = 0.70 + 0.40 * (hum - 60) / 25.0
        base *= factor
    return base


def kwh_est(active_min, duty=1.0):
    """Energy estimate (kWh): input power * duty * duration."""
    p = AC_MEASURED_W or AC_INPUT_W
    return p / 1000.0 * duty * (active_min / 60.0)


def current_price():
    """Price by time: 22:00-6:00 valley, else peak."""
    h = datetime.now().hour
    return ELECTRIC_VALLEY if h >= 22 or h < 6 else ELECTRIC_PEAK


def cost_est(kwh):
    """Cost estimate (CNY) by current time-of-use price."""
    return kwh * current_price()


# ============================================================
# State persistence
# ============================================================
def load_state() -> dict:
    """Load persistent state, return default if missing."""
    default = {"mode": None, "last_on_at": None, "last_off_at": None, "run_start": None}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
            return {**default, **json.load(f)}
    except Exception as e:
        print(f"[ERROR] state load failed: {type(e).__name__}: {e}")
        default["_state_load_failed"] = True
        return default


def save_state(state: dict):
    """Persist state (atomic write: tmp -> rename)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def minutes_since(ts_str):
    """Minutes since an ISO timestamp."""
    if not ts_str:
        return None
    try:
        then = datetime.fromisoformat(ts_str)
        now = datetime.now(tz=then.tzinfo if then.tzinfo else None)
        return (now - then).total_seconds() / 60.0
    except Exception:
        return None


# ============================================================
# Weather API (QW CMA)
# ============================================================
WEATHER_MAP = {
    0: "☀️ 晴", 1:"🌤 少云", 2:"⛅ 多云", 3:"☁️ 阴",
    45:"🌫 雾",
    51:"🌦 毛毛雨",61:"🌧 小雨",63:"🌧 中雨",65:"🌧 大雨",
    71:"🌨 小雪",73:"🌨 中雪",75:"🌨 大雪",
    80:"🌦 阵雨",81:"🌦 小阵雨",82:"🌦 大阵雨",95:"⛈ 雷暴",
}


def weather_cn(code):
    return WEATHER_MAP.get(code, f"☁️ {code}")


def _qw_get(endpoint: str) -> dict:
    """Call QW API v2, auto-decompress gzip, return JSON.

    Raises when the key is missing instead of returning empty: fetch_weather
    turns it into {"error": ...} and every caller already has a degraded path,
    so a missing .env degrades the forecast without stopping AC control."""
    if not QW_KEY:
        raise RuntimeError("QW_API_KEY not configured (expected in .env next to this script)")
    url = f"https://{QW_HOST}/weather/v1/{endpoint}/{LAT}/{LON}"
    req = urllib.request.Request(url, headers={"X-QW-Api-Key": QW_KEY, "Accept-Encoding": "identity"})
    resp = urllib.request.urlopen(req, timeout=15)
    body = resp.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def fetch_weather() -> dict:
    """Fetch weather (QW CMA), return Open-Meteo compatible format with wind."""
    try:
        CST = timezone(timedelta(hours=8))
        cur = _qw_get("current")
        dai = _qw_get("daily")["days"][0]
        hrs = _qw_get("hourly")["hours"]

        QW2WMO = {100:0, 101:2, 102:2, 103:3, 104:3,
                  200:0, 201:0, 202:0, 203:0,
                  300:0, 301:1, 302:2, 303:95, 304:95,
                  400:0, 401:0, 402:0, 403:0,
                  500:45, 501:45, 502:45, 503:45, 504:45,
                  507:45, 508:45, 509:45,
                  510:51, 511:51, 512:51, 513:51, 514:51,
                  600:61, 601:61, 602:63, 603:65,
                  305:61, 306:63, 307:65,
                  610:80, 611:80, 612:80, 613:80,
                  700:45, 701:45, 702:45, 703:45, 704:45,
                  800:95, 801:95, 802:95, 803:95, 804:95}
        wmo = QW2WMO.get(int(cur.get("condition",{}).get("code", 0)), 0)

        day_prec = dai.get("daytime", {}).get("precipitation", {})
        rain_prob = day_prec.get("probability", 0) if isinstance(day_prec, dict) else 0

        times, rh, pp, prec, temp, ws, wd, gusts = [], [], [], [], [], [], [], []
        for h in hrs:
            t_utc = datetime.fromisoformat(h["forecastTime"].replace("Z", "+00:00"))
            t_local = t_utc.astimezone(CST)
            times.append(t_local.strftime("%Y-%m-%dT%H:%M"))
            rh.append(round(h["humidity"] * 100))
            temp.append(h["temperature"]["value"])
            prec_obj = h.get("precipitation", {})
            pp.append(float(prec_obj.get("probability", 0)) * 100)
            prec.append(float(prec_obj.get("intensity", {}).get("value", 0)))
            wind_obj = h.get("wind", {})
            ws.append(float(wind_obj.get("speed", {}).get("value", 0)))
            wd.append(float(wind_obj.get("direction", {}).get("degree", 0)))
            gusts.append(float(h.get("windGust", {}).get("value", 0)))

        return {
            "current": {
                "temperature_2m": cur["temperature"]["value"],
                "apparent_temperature": cur["feelsLike"]["value"],
                "relative_humidity_2m": round(cur["humidity"] * 100),
                "weather_code": wmo,
            },
            "daily": {
                "temperature_2m_max": [dai["temperatureMax"]["value"]],
                "temperature_2m_min": [dai["temperatureMin"]["value"]],
                "precipitation_probability_max": [round(rain_prob * 100)],
            },
            "hourly": {
                "time": times,
                "relative_humidity_2m": rh,
                "precipitation_probability": pp,
                "precipitation": prec,
                "temperature_2m": temp,
                "wind_speed_10m": [w * 3.6 for w in ws],
                "wind_gusts_10m": [g * 3.6 for g in gusts],
                "wind_direction_10m": wd,
            },
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# Indoor sensor (Miio Purifier 4 Lite)
# ============================================================
def read_indoor(timeout=3.0):
    """Read indoor temp/humidity from Miio Purifier 4 Lite.
    Returns (temp, hum) or (None, None).
    Auto-reconnect on session deadlock."""
    if not os.path.exists(CONFIG_FILE):
        return None, None
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return None, None

    ip = cfg.get("ip")
    token = cfg.get("token")
    if not ip or not token:
        return None, None

    temp, hum = _read_indoor_once(ip, token, timeout=timeout)
    if temp is not None and hum is not None:
        return temp, hum

    temp, hum = _read_indoor_once(ip, token, timeout=5)
    if temp is not None and hum is not None:
        return temp, hum

    return None, None


def _read_indoor_once(ip, token, timeout):
    """Single read attempt, returns (None, None) on failure."""
    try:
        from miio import Device
        d = Device(ip, token, timeout=timeout)
        r = d.send("get_properties", [
            {"siid": 3, "piid": 7},   # temp
            {"siid": 3, "piid": 1},   # hum
        ])
        if isinstance(r, list) and len(r) >= 2:
            temp = r[0].get("value") if isinstance(r[0], dict) else None
            hum = r[1].get("value") if isinstance(r[1], dict) else None
            if temp is not None and hum is not None:
                return round(temp, 1), round(hum, 0)
        r2 = d.send("get_prop", ["temp_dec", "humidity"])
        if isinstance(r2, list) and len(r2) >= 2:
            return round(r2[0] / 10, 1), round(r2[1], 0)
    except Exception:
        pass
    return None, None


# ============================================================
# AC control
def filter_clean_reminder():
    """Filter cleaning reminder: dirty filter = less airflow = slower dehumid = waste."""
    try:
        with open(FILTER_STATE_FILE, encoding="utf-8") as f:
            last = json.load(f).get("last_clean")
        if not last:
            return "  💡 记得每 15~30 天洗一次滤网"
        days = (datetime.now() - datetime.fromisoformat(last)).days
        if days > FILTER_CLEAN_INTERVAL:
            return f"  ⚠️ 该洗滤网了（距上次清洗 {days} 天）"
        return None
    except Exception:
        return None

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
    """Magnus formula approximate dewpoint (C).
    Returns None if temp/rh missing or out of range (rh<=0 or >100)."""
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
    """95% ventilation duration (minutes), with safety factor.
    Stack effect floor: ~45 min * 1.2 minimum."""
    a = ach(w_kmh, dt)
    return 3.0 / a * 60.0 * SAFETY if a >= 0.5 else 999.0


def gate_check(rh, pp, rain_mm, temp, wind_ms, gust_ms, pm25,
               indoor_temp, indoor_rh):
    """Unified decision gate. Returns (ok: bool, reason: str|None).
    Shared by daily report + alert modes."""
    if pp is not None and pp >= RAIN_PP_MAX:
        return False, f"降雨概率{pp}%≥{RAIN_PP_MAX}%"
    if rain_mm is not None and rain_mm > RAIN_MM_MAX:
        return False, f"降水{rain_mm:.1f}mm/h>{RAIN_MM_MAX}"
    if wind_ms is not None and wind_ms >= WIND_MAX_MS:
        return False, f"风速{wind_ms:.0f}m/s≥{WIND_MAX_MS:.0f}"
    if gust_ms is not None and gust_ms >= GUST_MAX_MS:
        return False, f"阵风{gust_ms:.0f}m/s≥{GUST_MAX_MS:.0f}"
    if rh is not None and rh >= 85:
        return False, f"RH{rh}%≥85%"
    # Dew-point delta gate
    if indoor_temp is not None and indoor_rh is not None and temp is not None:
        dp_in = dew_point(indoor_temp, indoor_rh)
        dp_out = dew_point(temp, rh)
        if dp_in is not None and dp_out is not None:
            ddp = dp_out - dp_in
            if ddp >= DEW_DELTA_MAX:
                return False, f"露点差{ddp:.1f}°C(室外{dp_out:.1f}→室内{dp_in:.1f})灌湿气"
    else:
        if indoor_rh is not None and rh is not None and (rh - indoor_rh) >= 10:
            return False, f"室外比室内潮{rh - indoor_rh:.0f}pp≥10(无室内温度,按RH差回退)"
    if pm25 is not None and pm25 >= PM25_MAX:
        return False, f"PM2.5={pm25:.0f}≥{PM25_MAX}"
    return True, None


def vent_advice(now_rh, out_rh, now_temp=None, out_temp=None):
    """Indoor/outdoor comparison + AC linkage advice.
    Priority: dew-point delta; fallback to RH delta if indoor temp unavailable."""
    lines = []
    ac_mode = read_ac_state()
    if now_rh is not None and out_rh is not None:
        dp_in = dew_point(now_temp, now_rh)
        dp_out = dew_point(out_temp, out_rh)
        if dp_in is not None and dp_out is not None:
            ddp = dp_out - dp_in
            if ddp <= -1.5:
                lines.append(f"🌫 室外露点{dp_out:.1f}°C < 室内{dp_in:.1f}°C → 开窗能顺带除湿 🟢（露点差{abs(ddp):.1f}°C）")
            elif ddp <= -0.5:
                lines.append(f"🌫 室外露点略低（{abs(ddp):.1f}°C）→ 开窗有利")
            elif ddp >= 1.5:
                lines.append(f"⚠️ 室外露点{dp_out:.1f}°C > 室内{dp_in:.1f}°C → 开窗会灌湿气，收益低（露点差{ddp:.1f}°C）")
            elif ddp >= 0.5:
                lines.append(f"⚠️ 室外露点略高（{ddp:.1f}°C）→ 开窗换气请短促，主要靠空调/除湿")
            else:
                lines.append(f"ℹ️ 室内外露点接近（室内{dp_in:.1f}°C/室外{dp_out:.1f}°C）→ 开窗只为换新鲜空气")
        else:
            diff = out_rh - now_rh
            if diff <= -10:
                lines.append(f"💡 室外比室内干 {abs(diff):.0f}pp（室内{now_rh:.0f}%→室外{out_rh}%）→ 开窗可顺带除湿 🟢")
            elif diff <= -3:
                lines.append(f"💡 室外略干于室内（{abs(diff):.0f}pp）→ 开窗有利")
            elif diff >= 10:
                lines.append(f"⚠️ 室外比室内潮 {diff:.0f}pp（室内{now_rh:.0f}%→室外{out_rh}%）→ 开窗会把湿气灌进来")
            elif diff >= 3:
                lines.append(f"⚠️ 室外比室内潮 {diff:.0f}pp → 开窗换气请短促")
            else:
                lines.append(f"ℹ️ 室内外湿度接近（室内{now_rh:.0f}%/室外{out_rh}%）→ 开窗只为换新鲜空气")
    if ac_mode:
        if ac_mode in AC_BLOCK_MODES:
            label = '制冷' if ac_mode == 'cooling' else '除湿'
            lines.append(f"❄️ 空调运行中（顾问建议{label}，非实测模式）→ 开窗会抵消效果，换完即关")
        elif ac_mode == "fan":
            lines.append("🍃 空调顾问建议=风扇通风 → 开窗与风扇同向，效果叠加")
        elif ac_mode == "off":
            lines.append("🌡 空调顾问建议=关 → 开窗无冲突")
    return lines


def verdict(rh):
    """Ventilation verdict from RH."""
    if rh < 60:  return ("极佳", "🟢")
    if rh < 70:  return ("好", "🟢")
    if rh < 78:  return ("一般", "🟡")
    if rh < 85:  return ("谨慎", "🟠")
    return ("不推荐", "🔴")


WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def wind_dir_cn(deg):
    """Wind direction degrees -> Chinese compass."""
    if deg is None:
        return None
    dirs = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return dirs[int((deg % 360) / 45) % 8] + "风"


def build_rows(h, today):
    """Build candidate rows from hourly forecast for today."""
    rows = []
    for i, t in enumerate(h["time"]):
        if t[:10] != today:
            continue
        hr = int(t[11:13])
        rows.append({
            "hr": hr,
            "rh": h["relative_humidity_2m"][i] or 0,
            "pp": h["precipitation_probability"][i] or 0,
            "rain_mm": h["precipitation"][i] or 0,
            "temp": h["temperature_2m"][i],
            "wind_kmh": h["wind_speed_10m"][i] or 0,
            "gust_ms": (h["wind_gusts_10m"][i] or 0) / 3.6,
            "wind_dir": h.get("wind_direction_10m", [None] * len(h["time"]))[i],
        })
    return rows


def pick_best(rows, indoor_temp, indoor_rh, ac_mode, aqi):
    """Unified gate filter + dew-point delta sort.
    Returns (best_row, blocked_reason or None).
    Sort key: outdoor dewpoint ascending (drier is better), then rain prob ascending."""
    cand = []
    blocked = None
    ac_blocking = ac_mode in AC_BLOCK_MODES
    now_hr = datetime.now().hour
    for r in rows:
        # AC linkage: skip current/past windows while the advisor is actively
        # cooling/dehumidifying (opening now would undermine the run).
        if ac_blocking and r["hr"] <= now_hr:
            if blocked is None:
                blocked = "空调制冷/除湿运行中，先不开窗"
            continue
        dp_out = dew_point(r["temp"], r["rh"])
        pm25 = aqi.get(f"{date.today().isoformat()}T{r['hr']:02d}:00") if aqi else None
        ok, reason = gate_check(r["rh"], r["pp"], r["rain_mm"], r["temp"],
                                r["wind_kmh"] / 3.6, r["gust_ms"], pm25,
                                indoor_temp, indoor_rh)
        if not ok:
            if blocked is None:
                blocked = reason
            continue
        cand.append(r)
    if not cand:
        return None, blocked or "今日无通过闸门的窗口"
    cand.sort(key=lambda r: (dew_point(r["temp"], r["rh"]) or 99.0, r["pp"]))
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
        # 每日最低换气保障：CO2 每天都得降一次
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
    try:
        with open(VENT_CYCLE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


    except Exception as e:
        return f"（⚠️ 停空调异常: {e}，请手动关）"


def in_quiet_hours(now=None):
    """True inside the no-disturb window (default 22:00-07:00, wraps midnight)."""
    if now is None:
        now = datetime.now()
    hr = now.hour
    if VENT_QUIET_START <= VENT_QUIET_END:
        return VENT_QUIET_START <= hr < VENT_QUIET_END
    return hr >= VENT_QUIET_START or hr < VENT_QUIET_END


def vent_cycle_step(data, indoor_temp, indoor_hum, now=None):
    """Full auto vent cycle.
    Idle   -> pick best upcoming window; one heads-up per window; at window start
              stop AC + remind to open windows with planned end time.
    Venting-> silent until planned end, then remind to close windows.
    Returns alert text or '' (silent)."""
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
        # Real-time weather check: if this vent hour turns bad, warn to close early (once)
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
    # Placed after the in-progress branch on purpose: a running cycle must still
    # be able to tell you to close the windows even past 22:00.
    if in_quiet_hours(now):
        return ""

    h = data.get("hourly", {}) or {}
    if not h or not h.get("time"):
        return ""
    rows = build_rows(h, today)
    if not rows:
        return ""
    # Candidate windows must be actionable: drop hours that fall inside quiet
    # hours, otherwise 21:30 would announce a 22:00 window the quiet-hours gate
    # then refuses to start.
    upcoming = [r for r in rows
                if now_min <= r["hr"] * 60 <= now_min + 90
                and not in_quiet_hours(now.replace(hour=r["hr"], minute=0))]
    if not upcoming:
        return ""
    ac_mode = read_ac_state()
    aqi = fetch_aqi(1)
    best, _ = pick_best(upcoming, indoor_temp, indoor_hum, ac_mode, aqi)
    if best is None:
        return ""
    # Same-day window already completed -> skip until next window
    if (st.get("date") == today and st.get("window_hr") == best["hr"]
            and st.get("notified_end")):
        return ""
    start_min = best["hr"] * 60
    dt_delta = (best["temp"] - indoor_temp) if indoor_temp is not None else 0
    dur = t95(best["wind_kmh"], dt_delta)
    dur = max(10, min(90, int(round(dur))))
    tag = f"{today}|{best['hr']}"

    if now_min < start_min:
        # Not yet window start -> one heads-up per window (dedup)
        if st.get("last_pre") == tag:
            return ""
        st["last_pre"] = tag
        save_vent_cycle(st)
        vv, emoji = verdict(best["rh"])
        rain = "☔降雨" if best["pp"] >= 40 else ("🌦" if best["pp"] >= 20 else "☀")
        _wd = wind_dir_cn(best.get("wind_dir"))
        wd_s = f"  🧭 {_wd}" if _wd else ""
        return (f"⏰ 换气提醒: {best['hr']:02d}:00 是好窗口，到点自动停空调并提醒你开窗\n"
                f"   RH{best['rh']}% {rain}{best['pp']}% 温度{best['temp']}°C "
                f"风{best['wind_kmh']:.0f}km/h → {emoji}{vv}{wd_s}\n"
                f"   ⏱ 预计换气 {dur} 分钟，到点开始计时")

    if now_min > start_min + 30:
        return ""  # window passed, wait for next

    # -- Window began -> start cycle: stop AC + remind open --
    state = load_state()
    now_ts = now.isoformat(timespec="seconds")
    ac_note = "（请先关空调再开窗）"  # home_living 不控空调，仅提示
    end_dt = now + timedelta(minutes=dur)
    save_vent_cycle({
        "date": today,
        "window_hr": best["hr"],
        "started_ts": now_ts,
        "dur_min": dur,
        "end_ts": end_dt.isoformat(timespec="seconds"),
        "notified_start": True,
        "notified_end": False,
        "start_hum": indoor_hum,
    })
    return (f"🪟 通风时间到（{best['hr']:02d}:00 窗口）！{ac_note}\n"
            f"   请开窗换气约 {dur} 分钟，到 {end_dt.strftime('%H:%M')} 提醒你关窗")


def alert_check():
    """Alert mode: full auto vent cycle (heads-up -> stop AC + open -> timed close)."""
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
        st = {}
        if os.path.exists(ERR_STATE_FILE):
            with open(ERR_STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
        last = st.get(key, 0)
        if time.time() - last < 86400:
            return ""
        st[key] = time.time()
        with open(ERR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass
    return f"⚠️ 换气提醒数据异常（{detail[:100]}）→ 今日暂停，明早 08:00 恢复"


def notify_windows(title, text):
    """Windows toast notification (parallel with WeChat)."""
    try:
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$txt = $t.GetElementsByTagName('text');"
            "$txt.Item(0).AppendChild($t.CreateTextNode('{0}')) > $null;"
            "$txt.Item(1).AppendChild($t.CreateTextNode('{1}')) > $null;"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PiAgent').Show($toast)"
        ).format(title, text.replace("'", "").replace("\"", ""))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=10, capture_output=True)
    except Exception:
        pass


# ============================================================
# Unified decision engine (v11.0 new)
# ============================================================


# ============================================================
# Main
# ============================================================
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
    lines.append(f"🏠 上海闵行 · 家居生活顾问 v11.1")
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