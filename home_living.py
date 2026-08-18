#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Home Living Unified Advisor v11.0 - Shanghai Minhang
Merges ac_advisor v10.1 + vent_reminder v2.2 into a single decision engine.

v11.0 changes:
- Unified AC + window + fan decision engine
- Wet-bulb temperature (Stull 2011) for heat stress assessment
- Comfort index humidity weight updated to 0.02
- Ventilation ACH model: wind pressure + stack effect, mix efficiency 0.7
- Unified gate_check() shared by daily report + alert modes
- Dew-point delta sorting for window selection
- PM2.5 / AQI data source (Open-Meteo Air Quality)
- Windows toast notification fallback
- ASCII-safe docstrings throughout

Retained from v9.0:
- Indoor sensor reading (Miio Purifier 4 Lite)
- Weather fetch (QW CMA data source)
- Manual off anchor (2h no-auto-start)
- Filter reminder
- Night cost comparison
- TTS broadcast
- WeChat/desktop notification
- All threshold constants
"""

import gzip
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date, timezone, timedelta
from enum import Enum

# -- Ensure miio is findable (cron may use python3.11, miio installed in 3.12) --
_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]
for _p in _MIIO_PATHS:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# -- Threshold constants (unified header, configurable) --
TEMP_COOLING = 28       # HI >= 28 -> cooling
TEMP_DEHUMID_LOW = 26   # dehumid temp lower bound
TEMP_DEHUMID_HIGH = 28  # dehumid temp upper bound
HUM_DEHUMID_ON = 65     # dehumid ON threshold
HUM_DEHUMID_OFF = 55    # dehumid OFF threshold (10% hysteresis)
WETBULB_DEHUMID_ON = 20.5  # dehumid when wet-bulb >= 20.5C (~26C/65%RH, 24C/72%RH)
TEMP_ABSOLUTE_FLOOR = 24# dehumid absolute floor (OR escape hatch)
MIN_RUN = 40            # min run time once on
MIN_OFF = 30            # min off time at night before restart
DAY_MIN_OFF = 15        # min off time daytime before restart
MAX_RUN = 180           # max continuous run before switch/off

# -- AC power (Chuan KFRd-35GW fixed freq 1.5P) --
AC_INPUT_W = 1076     # input power W
AC_COP = 3.25          # COP
ELECTRIC_PEAK = 0.617   # Shanghai peak price (6:00-22:00)
ELECTRIC_VALLEY = 0.307 # Shanghai valley price (22:00-6:00)
ELECTRIC_PRICE = ELECTRIC_PEAK
DEHUMID_DUTY = 0.60    # fixed freq dehumid duty cycle
COOL_DUTY = 0.70       # fixed freq cooling duty cycle
DEHUMID_MIN = 60       # dehumid typical minutes
COOL_BURST_MIN = 40    # cooling burst minutes

# -- Room / AC capacity factor (65 sqm, 1.5P fixed frequency) --
AREA_SQM = 65                     # 70平 - 5平厕所 = 65平实际制冷
AREA_FACTOR = AREA_SQM / 35.0   # 35 sqm = 1.5P design coverage
AC_RATED_W = 3500              # 1.5P rated cooling capacity (W)

# -- Vent reminder constants --
LAT, LON = 31.11, 121.38  # Shanghai Minhang
BASE_ACH = 47.0
BASE_WIND = 6.9  # 4-window cross-vent: 6.9 m/s -> ACH 47
MIX_EFF = 0.7    # cross-vent short-circuit loss
SAFETY = 1.2     # duration safety factor

# Hard gate thresholds
RAIN_PP_MAX = 45      # rain prob >= 45% -> no open
RAIN_MM_MAX = 1.0     # rain intensity > 1mm/h -> no open
DEW_DELTA_MAX = 1.5   # outdoor dewpoint - indoor >= 1.5C -> moisture ingress
PM25_MAX = 75         # PM2.5 >= 75 ug/m3
WIND_MAX_MS = 10.8    # sustained wind >= 6级 (10.8 m/s)
GUST_MAX_MS = 15.0    # gust >= 15 m/s
AC_BLOCK_MODES = ("cooling", "dehumid", "dehumid_alert")

# -- Shared state files --
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "home_state.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")
LEARN_FILE = os.path.join(SCRIPT_DIR, "ac_learned.json")
ERR_STATE_FILE = os.path.join(SCRIPT_DIR, "vent_error_state.json")

# -- Weather API (QW CMA) --
QW_HOST = "kf54e6wb7f.re.qweatherapi.com"
QW_KEY = "e630a3166d6f4146be43fa822cea63a1"

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
    return temp + 0.02 * (hum - 10)

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
def load_learned() -> dict:
    """Load learned results: adjusted_thresholds + decision_log."""
    default = {"adjusted_thresholds": {}, "decision_log": []}
    try:
        if os.path.exists(LEARN_FILE):
            with open(LEARN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_learned(learned: dict):
    """Persist learned results (atomic write)."""
    tmp = LEARN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEARN_FILE)


def evaluate_and_learn(state, now_ts):
    """Post-decision review: on -> temp dropped? off -> stuffy?
    Success -> threshold unchanged; failure -> threshold +/- 1C.

    v11.1 (2026-08-18) fixes:
    - Power gate: if the compressor is actually running (socket power
      >300W), a slow temp drop is NOT a decision failure. 65sqm on 1.5P
      is chronically underpowered; without the gate, every cooling run
      gets misjudged as failure and temp_cooling drifts to -8.
    - Threshold clamp: temp_cooling bounded to [-2, +2] (was unbounded).
    """
    learned = load_learned()
    log = learned.get("decision_log", [])
    cutoff = (datetime.now() - timedelta(minutes=30)).isoformat()
    adjusted = learned.get("adjusted_thresholds", {})
    # Power gate: is the compressor actually drawing power right now?
    comp_power = state.get("_prev_power") or state.get("measured_w")
    comp_running = (comp_power or 0) > 300
    for entry in log:
        if entry.get("evaluated") or entry.get("time", "") < cutoff:
            continue
        pre_temp = entry.get("pre_temp")
        pre_hum = entry.get("pre_hum")
        action = entry.get("action")
        cur_temp = state.get("last_temp")
        cur_hum = state.get("last_hum")
        if pre_temp is None or cur_temp is None:
            entry["evaluated"] = True
            continue
        success = True
        if action in ("cooling", "dehumid"):
            temp_drop = pre_temp - cur_temp
            hum_drop = (pre_hum or 0) - (cur_hum or 0)
            # Power gate: compressor running = the decision is being
            # executed; slow cooldown is physics, not a bad threshold.
            if comp_running:
                success = True
            elif temp_drop < 0.3 and hum_drop < 3:
                success = False
        elif action in ("off", "fan"):
            temp_rise = cur_temp - pre_temp
            if temp_rise > 2.0 or (cur_hum is not None and cur_hum > 80):
                success = False
        if not success:
            if action in ("cooling", "dehumid"):
                cur_adj = adjusted.get("temp_cooling", 0)
                adjusted["temp_cooling"] = max(-2, min(2, cur_adj - 1))
            elif action in ("off", "fan"):
                cur_adj = adjusted.get("temp_cooling", 0)
                adjusted["temp_cooling"] = max(-2, min(2, cur_adj - 1))
        entry["evaluated"] = True
    learned["adjusted_thresholds"] = adjusted
    learned["decision_log"] = log[-50:]
    save_learned(learned)


def log_decision(state, action, pre_temp, pre_hum, now_ts):
    """Record a decision for later review."""
    learned = load_learned()
    log = learned.get("decision_log", [])
    log.append({
        "time": now_ts,
        "action": action,
        "pre_temp": pre_temp,
        "pre_hum": pre_hum,
        "evaluated": False,
    })
    learned["decision_log"] = log[-50:]
    save_learned(learned)


# ============================================================
# Thermal event learning (persistent, ac_thermal.json)
# ============================================================
THERMAL_FILE = os.path.join(SCRIPT_DIR, "ac_thermal.json")


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
    temp_after/duration_min may be None (filled when the cycle completes);
    incomplete events are excluded from model fitting."""
    data = load_thermal_data()
    events = data.get("events", [])
    events.append({
        "type": event_type,
        "temp_before": temp_before,
        "temp_after": temp_after,
        "duration_min": duration_min,
        "outdoor_temp": outdoor_temp,
        "timestamp": datetime.now().isoformat(),
    })
    data["events"] = events[-100:]
    data["thermal_model"] = fit_thermal_model(data["events"])
    save_thermal_data(data)


def fit_thermal_model(events):
    """Fit thermal model from completed events (temp_after + duration known).
    Returns {"cooling_rate_per_min": x, "warmup_rate_per_min": y, "time_constant_min": z}."""
    cooling = [e for e in events if e.get("type") == "cooling"
               and e.get("temp_after") is not None and e.get("duration_min")]
    warming = [e for e in events if e.get("type") == "warming"
               and e.get("temp_after") is not None and e.get("duration_min")]
    model = {
        "cooling_rate_per_min": 0.05,
        "warmup_rate_per_min": 0.02,
        "time_constant_min": 120,
    }
    if cooling:
        rates = [(e["temp_before"] - e["temp_after"]) / max(e["duration_min"], 1)
                 for e in cooling[-20:]]
        model["cooling_rate_per_min"] = sum(rates) / len(rates)
    if warming:
        rates = [(e["temp_after"] - e["temp_before"]) / max(e["duration_min"], 1)
                 for e in warming[-20:]]
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
    """Call QW API v2, auto-decompress gzip, return JSON."""
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
# ============================================================
def ac_control_init():
    """Init AC control handle. miio_config.json ac_control=False disables."""
    global AC_CTRL
    AC_CTRL = None
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        ap = cfg.get("ac_partner") or {}
        if ap.get("ip") and ap.get("token") and cfg.get("ac_control", True):
            from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02
            AC_CTRL = AirConditioningCompanionMcn02(ap["ip"], ap["token"])
    except Exception:
        AC_CTRL = None


def ac_apply(new_mode, target_temp=None):
    """Execute decision to AC socket via IR.
    Returns {"status": "action"|"no_action"|"failed", "action": str, "reason": str}."""
    if new_mode == "dehumid_alert":
        return {"status": "no_action", "action": "", "reason": "alert_only"}
    if AC_CTRL is None:
        return {"status": "failed", "action": "", "reason": "control_unavailable"}
    try:
        st = AC_CTRL.status()
        on = st.is_on
    except Exception as e:
        return {"status": "failed", "action": "", "reason": f"status_read_failed: {e}"}
    act = []
    if new_mode in ("cooling", "dehumid"):
        want_mode = "cool" if new_mode == "cooling" else "dry"
        if not on:
            try:
                AC_CTRL.send_command("set_power", ["on"])
                act.append("开机")
                on = True
            except Exception as e:
                return {"status": "failed", "action": "开机", "reason": f"power_on_failed: {e}"}
        try:
            if st.mode is not None and st.mode.value != want_mode:
                AC_CTRL.send_command("set_mode", [want_mode])
                act.append(f"模式{want_mode}")
        except Exception:
            pass
        if want_mode == "dry":
            pass
        else:
            try:
                if target_temp and st.target_temperature != target_temp:
                    AC_CTRL.send_command("set_tar_temp", [target_temp])
                    act.append(f"设定{target_temp}°C")
            except Exception:
                pass
    elif new_mode == "fan_locked":
        pass
    elif new_mode in ("fan", "off"):
        if on:
            try:
                AC_CTRL.send_command("set_power", ["off"])
                act.append("关机")
            except Exception as e:
                return {"status": "failed", "action": "关机", "reason": f"power_off_failed: {e}"}
    return {"status": "action" if act else "no_action", "action": "，".join(act), "reason": ""}


def reconcile_state(state, now_ts):
    """Reconcile persistent state with socket reality (P2).
    Socket reachable: real device state wins.
    System-initiated off (marked _system_off_at) not treated as manual."""
    if AC_SOCKET == "off" and state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
        sys_off = state.get("_system_off_at")
        is_system_off = False
        if sys_off:
            try:
                sys_off_dt = datetime.fromisoformat(sys_off) if isinstance(sys_off, str) else sys_off
                now_dt = datetime.fromisoformat(now_ts) if isinstance(now_ts, str) else now_ts
                if (now_dt - sys_off_dt).total_seconds() < 180:
                    is_system_off = True
            except Exception:
                pass
        if not is_system_off:
            state["manual_off_at"] = now_ts
        state["mode"] = "off"
        state["last_off_at"] = now_ts
        state["run_start"] = None
        state.pop("_system_off_at", None)
        return

    if state.get("_system_off_at"):
        state.pop("_system_off_at", None)
    if AC_SOCKET == "on" and state.get("mode") not in ("cooling", "dehumid", "dehumid_alert"):
        state["manual_on_at"] = now_ts
        state["mode"] = "cooling"
        state["run_start"] = now_ts
        state["_fake_run_count"] = 0
        _learn_from_manual(state, now_ts)


def verify_socket():
    """Post-command verify: read real socket state. Returns "on"|"off"|None."""
    if AC_CTRL is None:
        return None
    try:
        s = AC_CTRL.status()
        return "on" if s.is_on else "off"
    except Exception:
        return None


def apply_state_from_verify(state, new_mode, real, now_ts):
    """Update run/stop anchors from socket reality. Returns None=consistent, True=contradict."""
    was_on = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    if real == "on":
        if new_mode in ("cooling", "dehumid", "dehumid_alert"):
            if not was_on:
                state["run_start"] = now_ts
                state["last_on_at"] = now_ts
            state["mode"] = new_mode
            return None
        state["mode"] = "cooling"
        state.pop("last_off_at", None)
        return True
    # real == "off"
    if new_mode in ("fan", "fan_locked", "off"):
        if was_on:
            state["last_off_at"] = now_ts
            state["_system_off_at"] = now_ts
        state["mode"] = new_mode
        state["run_start"] = None
        return None
    state["mode"] = "off"
    state["run_start"] = None
    return True


def apply_and_commit(new_mode, target_temp, state, now_ts=None, meta=None):
    """Single execution interface (P2 ownership): ac_apply -> verify -> update state -> commit."""
    if now_ts is None:
        now_ts = datetime.now().isoformat(timespec="seconds")
    ctrl = ac_apply(new_mode, target_temp)
    if ctrl["status"] == "failed":
        save_state(state)
        return ctrl
    real = verify_socket()
    if real is None:
        ctrl = {"status": "failed", "action": ctrl.get("action", ""), "reason": "verify_unreachable"}
        save_state(state)
        return ctrl
    contradict = apply_state_from_verify(state, new_mode, real, now_ts)
    if contradict:
        ctrl = {"status": "failed", "action": ctrl.get("action", ""),
                "reason": "verify_on_after_off" if real == "on" else "verify_off_after_on"}
    if meta and not contradict:
        for k, v in meta.items():
            state[k] = v
    if not contradict and target_temp is not None:
        state["target_temp"] = target_temp
    save_state(state)
    return ctrl


def read_ac_power(timeout=4.0):
    """Read AC socket measured power (W) and on/off state.
    Returns measured watts (on + readable), else None."""
    global AC_MEASURED_W, AC_SOCKET
    AC_SOCKET = None
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        ap = cfg.get("ac_partner") or {}
        if not ap.get("ip") or not ap.get("token"):
            return None
        from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02
        d = AirConditioningCompanionMcn02(ap["ip"], ap["token"])
        st = d.status()
        AC_SOCKET = "on" if st.is_on else "off"
        AC_MEASURED_W = None
        if st.is_on and st.load_power and st.load_power > 0:
            AC_MEASURED_W = round(st.load_power)
            return AC_MEASURED_W
    except Exception:
        pass
    return None


# ============================================================
# User habit learning
# ============================================================
NIGHT_HOURS = 6
DAYS_PER_MONTH = 30


def _learn_from_manual(state, now_ts):
    """Learn user habits: 3+ manual interventions -> auto-adjust threshold."""
    try:
        rh_hist = state.get("rh_history", [])
        if not rh_hist:
            return
        last_rh = rh_hist[-1][1] if rh_hist else None
        if last_rh is None:
            return
        pref = state.get("user_pref", {})
        manual_log = pref.get("manual_on_log", [])
        manual_log.append({
            "ts": now_ts,
            "rh": last_rh,
            "mode": state.get("mode"),
        })
        if len(manual_log) > 20:
            manual_log = manual_log[-20:]
        pref["manual_on_log"] = manual_log
        low_rh_manual = [m for m in manual_log if 60 <= m.get("rh", 0) < 65]
        if len(low_rh_manual) >= 3:
            pref["hum_threshold"] = 60
        else:
            pref.pop("hum_threshold", None)
        state["user_pref"] = pref
    except Exception:
        pass


def night_cost_lines(indoor_temp, indoor_hum):
    """Night cost comparison (20:00-06:00 valley window)."""
    h = datetime.now().hour
    if not (h >= 20 or h < 6):
        return []
    p = ELECTRIC_VALLEY
    kb = kwh_est(COOL_BURST_MIN, COOL_DUTY)
    dd = dehumid_duty(indoor_temp if indoor_temp is not None else 26.5, indoor_hum)
    duty_26 = dehumid_duty(26, indoor_hum) if indoor_hum else 0.45
    duty_24 = dehumid_duty(24, indoor_hum) if indoor_hum else 0.55
    k26 = kwh_est(NIGHT_HOURS * 60, duty_26)
    k24 = kwh_est(NIGHT_HOURS * 60, duty_24)
    kd = kwh_est(NIGHT_HOURS * 60, dd)
    lines = [f"🌙 夜间方案对比（谷电 {p:.3f} 元/度）:"]
    lines.append(f"   0️⃣ 压一轮24°C×40~60min: {kb:.2f}度 ≈ {kb * p:.2f}元 ← 最省")
    lines.append(f"   1️⃣ 睡眠+制冷26°C整夜:  {k26:.2f}度 ≈ {k26 * p:.2f}元")
    lines.append(f"   2️⃣ 睡眠+制冷24°C整夜:  {k24:.2f}度 ≈ {k24 * p:.2f}元（贵 {(k24 - k26) * p * DAYS_PER_MONTH:.1f}元/月）")
    lines.append(f"   3️⃣ 除湿模式整夜:        {kd:.2f}度 ≈ {kd * p:.2f}元")
    if indoor_hum is not None and indoor_hum > 70:
        lines.append("   💡 湿度偏高：先压轮24°C到60%再睡")
    else:
        lines.append("   💡 湿度不高：压轮收工最省")
    return lines


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
# Ventilation functions (from vent_reminder v2.2)
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
        lines.append("🚫 今日无推荐开窗窗口")
        lines.append(f"   原因: {blocked}")
        if indoor_temp is not None:
            lines.append(f"   📍 室内实测: {indoor_temp}°C / {indoor_hum:.0f}%")
        for x in vent_advice(indoor_hum, None, indoor_temp, None):
            lines.append(f"   {x}")
        lines.append("─" * 18)
        if blocked and ("降雨" in blocked or "降水" in blocked):
            lines.append("🌧 今天天气不配合，关窗靠空调/除湿机")
        else:
            lines.append("🔒 今天不宜开窗，关窗保环境")
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


def _vent_off_ac(state, now_ts):
    """Stop AC (cooling/dehumid) before window opens. Returns a note str."""
    if "--vent-dry-run" in sys.argv:
        return "（DRY-RUN：本应自动停空调）"
    try:
        ac_control_init()
        if AC_CTRL is None:
            return "（⚠️ 空调控制不可用，请手动关空调）"
        ctrl = apply_and_commit("off", None, state, now_ts, meta={"_vent_off_at": now_ts})
        if ctrl["status"] == "failed":
            return f"（⚠️ 停空调失败: {ctrl.get('reason', '')}，请手动关）"
        if ctrl.get("action"):
            return f"（已自动关空调: {ctrl['action']}）"
        return "（空调已是关闭状态）"
    except Exception as e:
        return f"（⚠️ 停空调异常: {e}，请手动关）"


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

    h = data.get("hourly", {}) or {}
    if not h or not h.get("time"):
        return ""
    rows = build_rows(h, today)
    if not rows:
        return ""
    upcoming = [r for r in rows if now_min <= r["hr"] * 60 <= now_min + 90]
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
    ac_note = _vent_off_ac(state, now_ts)
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
def unified_decision(wx, indoor_temp, indoor_hum, state, now_ts):
    """Unified AC + window + fan decision.
    Room-aware: AREA_FACTOR scales MIN_RUN/MIN_OFF/MAX_RUN for
    underpowered 1.5P AC on 65 sqm.
    """
    # -- Room size adaptation (65 sqm, 1.5P = underpowered) --
    eff_min_run = int(MIN_RUN * AREA_FACTOR)   # 40 * 1.86 = 74 min
    eff_min_off = int(MIN_OFF * AREA_FACTOR)   # 30 * 1.86 = 56 min
    eff_max_run = int(MAX_RUN * AREA_FACTOR)   # 180 * 1.86 = 335 min
    pre_offset = 2 if AREA_FACTOR <= 1.2 else int(2 * (AREA_FACTOR / 1.2))

    # Seasonal + learned offsets
    temp_offset, hum_offset, strategy_label = seasonal_adjustments()
    learned = load_learned()
    learned_temp_adj = learned.get("adjusted_thresholds", {}).get("temp_cooling", 0)
    effective_cooling_threshold = TEMP_COOLING + temp_offset + learned_temp_adj
    # v11.1: user habit learning output was dead code (written to
    # user_pref.hum_threshold but never read). Wire it in: user prefers
    # dehumid earlier -> lower the ON threshold (never below 60).
    user_hum_pref = state.get("user_pref", {}).get("hum_threshold")
    if isinstance(user_hum_pref, (int, float)):
        effective_hum_threshold = min(HUM_DEHUMID_ON + hum_offset, user_hum_pref)
    else:
        effective_hum_threshold = HUM_DEHUMID_ON + hum_offset

    # Signal selection
    cur = wx.get("current", {})
    temp = cur.get("apparent_temperature", cur.get("temperature_2m", 0))
    hum_out = cur.get("relative_humidity_2m")
    signal = indoor_temp if indoor_temp is not None else temp
    hum_sig = indoor_hum if indoor_hum is not None else None

    hi = comfort_index(signal, hum_sig)

    # Humidity trigger: wet-bulb (T+RH combined) replaces raw RH
    # for dehumid decisions; falls back to raw RH when wet-bulb is unavailable.
    tw = wet_bulb_temp(signal, hum_sig)
    humid_high = tw >= WETBULB_DEHUMID_ON if tw is not None else (
        hum_sig is not None and hum_sig > effective_hum_threshold)

    # Ventilation check
    hourly = wx.get("hourly", {})
    h_time = hourly.get("time", [])
    h_hum = hourly.get("relative_humidity_2m", [])
    h_precip = hourly.get("precipitation_probability", [])
    h_ws = hourly.get("wind_speed_10m", [])
    h_wd = hourly.get("wind_direction_10m", [])
    h_gust = hourly.get("wind_gusts_10m", [])
    h_temp = hourly.get("temperature_2m", [])

    vent_ok = False
    vent_reason = None
    best_window = None
    today = date.today().isoformat()
    rows = build_rows(hourly, today) if hourly else []
    aqi = fetch_aqi(1)
    ac_mode_state = state.get("mode")
    best_win, blocked = pick_best(rows, indoor_temp, indoor_hum, ac_mode_state, aqi) if rows else (None, None)

    if best_win is not None:
        pm25 = aqi.get(f"{today}T{best_win['hr']:02d}:00") if aqi else None
        ok, reason = gate_check(best_win["rh"], best_win["pp"], best_win["rain_mm"],
                                best_win["temp"], best_win["wind_kmh"] / 3.6,
                                best_win["gust_ms"], pm25,
                                indoor_temp, indoor_hum)
        vent_ok = ok
        vent_reason = reason

    # AC decision (same tree as v9.0 main)
    decision = None
    reason = ""
    new_mode = None
    burst_set = None

    precool, max_future_hi, _ = should_precool(wx, hi, effective_cooling_threshold)

    if hi >= effective_cooling_threshold:
        reco = round(max(26, min(28, signal - 2)))
        burst_set = reco
        decision = f"制冷模式 {reco}°C + 自动风速"
        reason = f"HI={hi:.1f}（{signal:.1f}°C/{hum_sig}%）≥ {effective_cooling_threshold}°C"
        new_mode = "cooling"
    elif (TEMP_ABSOLUTE_FLOOR <= signal < TEMP_DEHUMID_LOW
          and hum_sig is not None and hum_sig > effective_hum_threshold):
        burst_set = 23
        decision = "制冷 23°C 强制除湿一轮"
        reason = f"低温高湿：{signal:.1f}°C / 湿度{hum_sig:.0f}%"
        new_mode = "cooling"
    elif (TEMP_DEHUMID_LOW <= signal < TEMP_DEHUMID_HIGH
          and hum_sig is not None and hum_sig > effective_hum_threshold):
        running = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
        since_on = minutes_since(state.get("run_start"))
        over_max = running and since_on is not None and since_on >= eff_max_run
        if over_max:
            decision = "建议切换制冷或关"
            reason = f"除湿已连续运行≥{MAX_RUN}分钟"
            new_mode = "dehumid_alert"
        else:
            burst_set = 24
            decision = "制冷 24°C 集中除湿一轮"
            reason = f"湿度{hum_sig:.0f}% > {effective_hum_threshold}%"
            new_mode = "cooling"
    elif signal >= TEMP_DEHUMID_LOW:
        decision = "风扇够用，不用开空调"
        reason = f"{signal:.1f}°C，不算热"
        new_mode = "fan"
    else:
        if signal < TEMP_ABSOLUTE_FLOOR:
            decision = "关掉除湿！已经过冷"
            reason = f"温度{signal:.1f}°C < {TEMP_ABSOLUTE_FLOOR}°C"
            new_mode = "off"
        else:
            decision = "不用开空调，开窗通风+风扇"
            reason = f"温度{signal:.1f}°C，凉快"
            new_mode = "off"

    if precool and new_mode in ("fan", "off", "fan_locked"):
        decision = f"预冷建议：未来{pre_offset}h HI={max_future_hi:.1f}°C"
        reason = f"当前HI={hi:.1f}°C，未来{pre_offset}h将达{max_future_hi:.1f}°C"
        burst_set = round(max(26, min(28, signal - 2)))
        new_mode = "cooling"

    # State machine validation
    current_state = ACState(state.get("mode", "off") or "off")
    target_state = ACState(new_mode if new_mode in ("cooling", "dehumid", "fan", "fan_locked", "off") else "off")
    validated_state = transition(current_state, target_state)
    if validated_state != target_state:
        new_mode = validated_state.value
        reason += f"（状态机校验：{current_state.value}→{target_state.value} 非法，保持{validated_state.value}）"

    # Min run/off constraints (scaled by area factor)
    since_off = minutes_since(state.get("last_off_at"))
    since_on = minutes_since(state.get("run_start"))
    if new_mode in ("cooling", "dehumid", "dehumid_alert"):
        if since_off is not None and since_off < eff_min_off:
            decision = f"风扇（关后{eff_min_off}分钟内不重开）"
            reason += f"；关后仅{int(since_off)}分钟"
            new_mode = "fan_locked"
    elif new_mode in ("fan", "fan_locked", "off"):
        if since_on is not None and since_on < eff_min_run:
            decision = f"继续开着（开够{eff_min_run}分钟再关）"
            reason += f"；开仅{int(since_on)}分钟"
            new_mode = state.get("mode", "unknown")

    return {
        "ac_mode": new_mode,
        "decision": decision,
        "reason": reason,
        "burst_set": burst_set,
        "vent_ok": vent_ok,
        "vent_reason": vent_reason,
        "best_window": best_window,
        "strategy_label": strategy_label,
        "effective_cooling_threshold": effective_cooling_threshold,
        "hi": hi,
    }


def format_output(result, indoor_temp, indoor_hum, wx, ac_w, ctrl, run_info, ac_alert):
    """Format unified output string."""
    lines = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = wx.get("current", {})
    dai = wx.get("daily", {})
    temp = cur.get("temperature_2m", 0)
    feels = cur.get("apparent_temperature", 0)
    hum_out = cur.get("relative_humidity_2m")
    wcode = cur.get("weather_code", 0)
    max_t = dai.get("temperature_2m_max", [0])[0]
    rain = dai.get("precipitation_probability_max", [0])[0]

    lines.append(f"🏠 上海闵行 · 家居生活统一顾问 v11.0")
    lines.append(f"📅 {now_str} · {weather_cn(wcode)}")
    lines.append("")
    lines.append(f"  室外: {temp:.1f}°C  体感: {feels:.1f}°C  湿度: {hum_out:.0f}%")
    if indoor_temp is not None:
        lines.append(f"  室内: {indoor_temp:.1f}°C  湿度: {indoor_hum:.0f}%")
    else:
        lines.append(f"  室内传感器不可用")
    lines.append(f"  今日最高: {max_t:.1f}°C  降雨: {rain:.0f}%")
    lines.append(f"  舒适度HI: {result['hi']:.1f}（阈值{result['effective_cooling_threshold']:.0f}°C，{result['strategy_label']}）")
    if run_info:
        lines.append(run_info)
    if ac_w:
        lines.append(f"  🔌 空调实测功率: {ac_w}W")
    if ctrl and ctrl.get("status") == "action":
        lines.append(f"  🎛️ 已自动执行: {ctrl['action']}")
    elif ctrl and ctrl.get("status") == "no_action":
        lines.append("  🎛️ 已处目标状态，无需动作")
    elif ctrl:
        lines.append(f"  ⚠️ 自动控制失败（{ctrl.get('reason', '')}）")
    lines.append("")
    lines.append(f"  💡 {result['decision']}")
    lines.append(f"     ({result['reason']})")
    if ac_alert:
        lines.append(ac_alert)
    lines.append("")
    return lines


# ============================================================
# Main
# ============================================================
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # -- 0. Mode dispatch ---------------------------------------------------
    # Alert mode: lightweight "good window ahead" reminder, silent if none.
    if "--alert" in sys.argv:
        _alert_text = alert_check()
        print(_alert_text)   # empty -> silent (no WeChat/Windows delivery)
        if _alert_text:
            notify_windows("🌬 换气提醒", _alert_text)
            try:
                from ac_tts import speak
                speak(_alert_text[:60])
            except Exception:
                pass
        return

    # Daily vent-report mode: dedicated ventilation morning brief.
    if "--daily" in sys.argv or "--report" in sys.argv:
        _report = daily_report()
        print(_report)
        _toast = "\n".join(_report.splitlines()[:3])
        notify_windows("🌬 今日换气", _toast)
        return

    # -- 1. Fetch weather --
    wx = fetch_weather()
    if "error" in wx:
        print(f"⚠️ 天气API失败: {wx['error']}")
        print("🏠 上海闵行 · 家居生活统一顾问 v11.0")
        print("  数据不可用，请稍后再查")
        return

    # -- 2. Read indoor + AC --
    indoor_temp, indoor_hum = read_indoor()
    indoor_ok = indoor_temp is not None and indoor_hum is not None
    ac_w = read_ac_power()
    ac_control_init()

    # -- 3. Load state --
    state = load_state()
    now_ts = datetime.now().isoformat()
    now_dt = datetime.now()
    reconcile_state(state, now_ts)

    # Manual off anchor
    _manual_anchor = False
    _manual_anchor_mins = None
    manual_off = state.get("manual_off_at")
    if manual_off and state.get("mode") in (None, "off"):
        try:
            off_dt = datetime.fromisoformat(manual_off) if isinstance(manual_off, str) else manual_off
            mins = (now_dt - off_dt).total_seconds() / 60
            if 0 <= mins < 30:
                _manual_anchor = True
                _manual_anchor_mins = int(mins)
            if mins >= 720:
                state.pop("manual_off_at", None)
        except Exception:
            pass

    # -- 4. Unified decision --
    result = unified_decision(wx, indoor_temp, indoor_hum, state, now_ts)
    new_mode = result["ac_mode"]
    burst_set = result["burst_set"]

    # Manual anchor override
    if _manual_anchor and new_mode in ("cooling", "dehumid", "dehumid_alert"):
        new_mode = "off"
        result["decision"] = "保持现状（手动关后" + str(_manual_anchor_mins) + "分钟内不自动启动）"
        result["reason"] = "manual_off_anchor"
        burst_set = None

    # -- 5. Log + apply --
    log_decision(state, new_mode, indoor_temp, indoor_hum, now_ts)
    # Record thermal event for learning
    if new_mode in ("cooling", "dehumid"):
        record_thermal_event("cooling", indoor_temp, None, None, wx.get("current", {}).get("temperature_2m"))
    elif new_mode in ("off", "fan") and state.get("mode") in ("cooling", "dehumid"):
        record_thermal_event("warming", indoor_temp, None, None, wx.get("current", {}).get("temperature_2m"))

    state["last_temp"] = indoor_temp
    state["last_hum"] = indoor_hum
    ctrl = apply_and_commit(new_mode, burst_set, state, now_ts)
    evaluate_and_learn(state, now_ts)

    # -- 6. Build output --
    run_info = ""
    if AC_SOCKET == "on":
        run_info = "  🔌 空调运行中（插座实测）"
    elif AC_SOCKET == "off":
        run_info = "  🔌 空调已关（插座实测）"
    elif new_mode in ("cooling", "dehumid", "dehumid_alert"):
        run_now = minutes_since(state.get("run_start"))
        if run_now is not None:
            if run_now < 1:
                run_info = "  本轮刚建议开启"
            else:
                run_info = f"  已运行: {int(run_now)} 分钟"
                if run_now >= MAX_RUN:
                    run_info += f" ⚠️ 超 {MAX_RUN} 分钟"

    # Humidity alert
    ac_alert = ""
    _alert_state = {}
    _alert_file = os.path.join(SCRIPT_DIR, "ac_alert_state.json")
    try:
        if os.path.exists(_alert_file):
            with open(_alert_file, "r", encoding="utf-8") as f:
                _alert_state = json.load(f)
    except Exception:
        pass
    _today = datetime.now().strftime("%Y-%m-%d")
    if (new_mode in ("fan", "fan_locked", "off")
            and indoor_hum is not None and indoor_hum > 78
            and indoor_temp is not None and indoor_temp >= TEMP_ABSOLUTE_FLOOR
            and _alert_state.get("last_alert_day") != _today):
        _alert_state["last_alert_day"] = _today
        _alert_state["updated_at"] = datetime.now().isoformat()
        try:
            with open(_alert_file, "w", encoding="utf-8") as f:
                json.dump(_alert_state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        ac_alert = (f"  ⚠️ 湿度{indoor_hum:.0f}%偏高：就算不热，也该开空调压轮湿度"
                    f"（制冷集中 40~60 分钟，到 60% 关）")

    # Format + print
    out_lines = format_output(result, indoor_temp, indoor_hum, wx, ac_w, ctrl, run_info, ac_alert)

    # Ventilation advice: today's single best window (hourly scan) + instant advice
    try:
        _daily_vent = daily_report()
        if _daily_vent:
            for _vl in _daily_vent.splitlines():
                out_lines.append(f"  {_vl}")
            out_lines.append("")
    except Exception:
        pass
    hum_out = wx.get("current", {}).get("relative_humidity_2m")
    vent_lines = vent_advice(indoor_hum, hum_out, indoor_temp,
                             wx.get("current", {}).get("temperature_2m"))
    if vent_lines:
        out_lines.append("  🌬 即时通风建议:")
        for vl in vent_lines:
            out_lines.append(f"     {vl}")
    out_lines.append("")

    # Window close reminder
    dai = wx.get("daily", {})
    rain = dai.get("precipitation_probability_max", [0])[0]
    if rain >= 45:
        out_lines.append("  ⚠️ 今日有雨，请勿开窗（防潮）")
    elif hum_out is not None and hum_out >= 85:
        out_lines.append(f"  ⚠️ 室外潮湿({hum_out:.0f}%)，请勿开窗（防潮）")

    # AC tips
    if new_mode in ("cooling", "dehumid"):
        out_lines.append(f"  ⏱ 开够 {MIN_RUN} 分钟再关，关后等 {MIN_OFF} 分钟再开")
        if new_mode == "dehumid":
            out_lines.append(f"  ⏱ 温度<{TEMP_ABSOLUTE_FLOOR}°C 或 湿度<{HUM_DEHUMID_OFF}% 可关")
        else:
            out_lines.append(f"  💡 湿度<60%且温度≤27 / 湿度60-70%且≤26 → 可关")
            out_lines.append(f"  🔁 省电：制冷设 {burst_set or TEMP_DEHUMID_LOW}°C 集中 40~60 分钟 → 湿度到60%即关")

    # Filter reminder
    reminder = filter_clean_reminder()
    if reminder:
        out_lines.append(reminder)

    # Night cost comparison
    for nl in night_cost_lines(indoor_temp, indoor_hum):
        out_lines.append(nl)

    out_lines.append("")
    out_lines.append("─" * 40)
    out_lines.append("数据: Open-Meteo + 小米净化器4Lite · 统一决策v11.0")

    print("\n".join(out_lines))

    # TTS broadcast
    try:
        _tts_dir = os.path.join(os.path.expanduser("~"), ".hermes", "scripts")
        if _tts_dir not in sys.path:
            sys.path.insert(0, _tts_dir)
        from ac_tts import speak
        speak(result["decision"][:50])
        if ac_alert:
            speak(ac_alert, force=True)
    except Exception:
        pass

    # Windows toast (parallel to WeChat delivery)
    notify_windows("🏠 家居生活顾问", result["decision"])


if __name__ == "__main__":
    main()
