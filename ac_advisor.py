#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定频空调省电顾问 v11.0 · 上海闵行
RC 热模型 + DP 最优调度 + 自学习闭环 + TTS 语音
"""
import json, math, os, sys, urllib.request
from datetime import datetime, timezone, timedelta
from enum import Enum

_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]
for p in _MIIO_PATHS:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

TEMP_COOLING = 27; TEMP_DEHUMID_LOW = 26; HUM_DEHUMID_ON = 65
TEMP_ABSOLUTE_FLOOR = 24; MIN_RUN = 40; MIN_OFF = 15; DAY_MIN_OFF = 10
AC_INPUT_W = 1076; ELECTRIC_PEAK = 0.617; ELECTRIC_VALLEY = 0.307
DEHUMID_DUTY = 0.60; COOL_DUTY = 0.70

class ACState(Enum):
    OFF = "off"; COOLING = "cooling"; COOLING_MAINTAIN = "cooling_maintain"
    DEHUMID = "dehumid"; FAN = "fan"; FAN_LOCKED = "fan_locked"

TRANSITIONS = {
    ACState.OFF: {ACState.COOLING, ACState.DEHUMID, ACState.FAN},
    ACState.COOLING: {ACState.COOLING_MAINTAIN, ACState.OFF, ACState.FAN},
    ACState.COOLING_MAINTAIN: {ACState.OFF, ACState.COOLING},
    ACState.DEHUMID: {ACState.OFF, ACState.FAN},
    ACState.FAN: {ACState.OFF, ACState.COOLING, ACState.DEHUMID},
    ACState.FAN_LOCKED: {ACState.OFF, ACState.FAN},
}

def transition(current, target):
    return target if target in TRANSITIONS.get(current, set()) else current

def comfort_index(temp, hum):
    return temp if hum is None else temp + 0.05 * (hum - 10)

def dew_point(temp, hum):
    if hum is None or hum <= 0: return None
    a, b = 17.27, 237.7
    alpha = (a * temp) / (b + temp) + math.log(hum / 100.0)
    return (b * alpha) / (a - alpha)

def muggy_level(temp, hum):
    dp = dew_point(temp, hum)
    if dp is None: return 0
    if dp < 12: return 0
    elif dp < 16: return 1
    elif dp < 18: return 2
    else: return 3

def seasonal_adjustments():
    m = datetime.now().month
    if m in (7, 8): return 0, 0, "盛夏制冷"
    elif m == 6: return 1, -5, "梅雨除湿优先"
    elif m in (4, 5, 9, 10): return 2, 5, "春秋风扇优先"
    else: return 4, 0, "冬季关窗优先"

# ── 24h 最优调度（v11.0 DP） ──
def compute_optimal_schedule(wx, current_temp, current_hum, learned,
                           comfort_weight=1.0, comfort_target=26.0):
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    hums = hourly.get("relative_humidity_2m", [])
    if not times: return []

    CST = timezone(timedelta(hours=8))
    local_times = []
    for t in times:
        try:
            t_utc = datetime.fromisoformat(t)
            t_local = t_utc.astimezone(CST)
            local_times.append(t_local.strftime("%Y-%m-%dT%H:%M"))
        except: local_times.append(t)

    thermal_data = load_thermal_data()
    rc = thermal_data.get("thermal_model", {})
    a = rc.get("thermal_conductance", 0.003)
    c = rc.get("baseline_cooling", -0.035)
    T_MIN, T_MAX, T_STEP = 22.0, 32.0, 0.5
    n_temps = int((T_MAX - T_MIN) / T_STEP) + 1

    def temp_to_idx(t): return min(n_temps - 1, max(0, round((t - T_MIN) / T_STEP)))
    def idx_to_temp(i): return T_MIN + i * T_STEP

    def next_temp(t_in, t_out, action, dt_min=60):
        t = t_in
        for _ in range(dt_min):
            dt = a * (t_out - t) + c if action == "cool" else a * (t_out - t)
            t += dt
        return t

    def hour_cost(hour, t_in, action):
        price = ELECTRIC_VALLEY if hour >= 22 or hour < 6 else ELECTRIC_PEAK
        elec_cost = kwh_est(60, COOL_DUTY) * price if action == "cool" else 0
        comfort_penalty = comfort_weight * max(0, t_in - comfort_target) ** 2
        return elec_cost + comfort_penalty

    V = [[float('inf')] * n_temps for _ in range(25)]
    policy = [['off'] * n_temps for _ in range(24)]
    for ti in range(n_temps): V[24][ti] = 0

    for h in range(23, -1, -1):
        t_out = temps[h] if h < len(temps) else 28.0
        for ti in range(n_temps):
            t_in = idx_to_temp(ti)
            best_cost, best_action = float('inf'), 'off'
            for action in ['off', 'cool']:
                imm_cost = hour_cost(h, t_in, action)
                t_next = next_temp(t_in, t_out, action)
                ti_next = temp_to_idx(t_next)
                total = imm_cost + V[h + 1][ti_next]
                if total < best_cost:
                    best_cost, best_action = total, action
            V[h][ti] = best_cost
            policy[h][ti] = best_action

    schedule = []
    t_current = current_temp
    for h in range(min(24, len(local_times))):
        ti = temp_to_idx(t_current)
        action = policy[h][ti]
        t_out = temps[h] if h < len(temps) else 28.0
        cost = hour_cost(h, t_current, action)
        schedule.append((h, action, cost, t_current))
        t_current = next_temp(t_current, t_out, action)
    return schedule

def find_pre_cool_window(schedule, current_hour):
    hot_start = None
    for i, (h, action, cost) in enumerate(schedule):
        if action == "cool" and 6 <= h <= 21:
            hot_start = h; break
    if hot_start is None: return None
    all_valley = [22, 23, 0, 1, 2, 3, 4, 5]
    hours_to_hot = []
    for v in all_valley:
        hours_ago = (hot_start + 24 - v) if v >= 22 else (hot_start - v)
        if 1 <= hours_ago <= 16: hours_to_hot.append(v)
    if not hours_to_hot: return None
    pc_start, pc_end = hours_to_hot[0], hours_to_hot[-1]
    n_hours = len(hours_to_hot)
    valley_cost = ELECTRIC_VALLEY * kwh_est(40, COOL_DUTY) * n_hours
    peak_cost = ELECTRIC_PEAK * kwh_est(40, COOL_DUTY) * n_hours
    return (pc_start, pc_end, peak_cost - valley_cost)

def predict_dehumidify_need(wx, current_hum, current_temp):
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    hums = hourly.get("relative_humidity_2m", [])
    if not times or not hums: return False, None, None
    future_rh = []
    for i, t in enumerate(times):
        try:
            t_dt = datetime.fromisoformat(t)
            hours_ahead = (t_dt - datetime.now()).total_seconds() / 3600
            if 6 <= hours_ahead <= 30:
                future_rh.append(hums[i] if i < len(hums) else None)
        except: continue
    if not future_rh: return False, None, None
    max_future_rh = max(r for r in future_rh if r is not None)
    avg_future_rh = sum(r for r in future_rh if r is not None) / len([r for r in future_rh if r is not None])
    if max_future_rh > 70 or avg_future_rh > 65:
        if current_hum and current_hum > 55:
            return True, 55, f"谷电预湿：明日最高RH{max_future_rh:.0f}%，当前{current_hum:.0f}%，预除湿至55%"
    return False, None, None

# ── 自适应阈值学习 ──
LEARN_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ac_learned.json")
EVAL_DELAY_MIN = 30; EVAL_STALE_MIN = 120

def load_learned():
    default = {"adjusted_thresholds": {}, "decision_log": []}
    try:
        if os.path.exists(LEARN_FILE):
            with open(LEARN_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: pass
    return default

def save_learned(learned):
    tmp = LEARN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(learned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEARN_FILE)

def evaluate_and_learn(state, now_ts):
    learned = load_learned()
    log = learned.get("decision_log", [])
    cutoff = (datetime.now() - timedelta(minutes=EVAL_DELAY_MIN)).isoformat()
    stale = (datetime.now() - timedelta(minutes=EVAL_STALE_MIN)).isoformat()
    adjusted = learned.get("adjusted_thresholds", {})
    for entry in log:
        ts = entry.get("time", "")
        if entry.get("evaluated"): continue
        if ts > cutoff: continue
        if ts < stale: entry["evaluated"] = True; continue
        pre_temp = entry.get("pre_temp"); pre_hum = entry.get("pre_hum")
        action = entry.get("action")
        cur_temp = state.get("last_temp"); cur_hum = state.get("last_hum")
        if pre_temp is None or cur_temp is None: entry["evaluated"] = True; continue
        success = True
        if action in ("cooling", "dehumid"):
            comp_running = (entry.get("power_at_decision") or 0) > 300
            if comp_running: success = True
            elif (pre_temp - cur_temp) < 0.3 and ((pre_hum or 0) - (cur_hum or 0)) < 3: success = False
        elif action in ("off", "fan"):
            if (cur_temp - pre_temp) > 2.0 or (cur_hum is not None and cur_hum > 80): success = False
        cur_adj = adjusted.get("temp_cooling", 0)
        if not success: adjusted["temp_cooling"] = max(-2, min(2, cur_adj - 1))
        elif cur_adj < 0: adjusted["temp_cooling"] = cur_adj + 1
        elif cur_adj > 0: adjusted["temp_cooling"] = cur_adj - 1
        entry["evaluated"] = True
    # v8.29 fix: 日预算学习按"当日"用电判断，且偏移只能回落不能因超预算单向顶死。
    # 旧逻辑用累计 kWh 对比日预算 → 永远超支 → 偏移被持续 +0.5 顶到上限，
    # 启动线被推到 29°C，8/24 下午室温 30°C 都不开机。改为当日值+上限放宽到 +3，
    # 超预算最多把启动线推到 30°C（极端热天用户可手动干预）。
    daily_kwh = state.get("_daily_kwh", 0)
    daily_budget = 8.0
    _today_str = now_ts[:10] if isinstance(now_ts, str) else datetime.now().strftime("%Y-%m-%d")
    if state.get("_budget_prediction", {}).get("date") == _today_str:
        daily_budget = max(4.0, (state["_budget_prediction"].get("predicted_kwh") or 8.0) * 1.3)
    if daily_kwh > daily_budget and adjusted.get("temp_cooling", 0) < 3:
        adjusted["temp_cooling"] = min(3, adjusted.get("temp_cooling", 0) + 0.5)
    elif daily_kwh < daily_budget * 0.5 and adjusted.get("temp_cooling", 0) > -2:
        adjusted["temp_cooling"] = max(-2, adjusted.get("temp_cooling", 0) - 0.5)
    # 偏移健康护栏：白天(8-21点)若室温≥启动线-0.5 且空调未运行超过20分钟，
    # 说明启动线过高，强制回落 0.5（自愈，防止再次出现 30°C 不开机）
    if adjusted.get("temp_cooling", 0) > 0 and 8 <= datetime.now().hour < 21:
        if (state.get("last_temp") or 0) >= TEMP_COOLING + adjusted.get("temp_cooling", 0) - 0.5:
            _off_min = minutes_since(state.get("last_off_at"))
            if _off_min is not None and _off_min > 20:
                adjusted["temp_cooling"] = round(max(-2, adjusted.get("temp_cooling", 0) - 0.5), 2)
    # 每日用电预算预测
    _budget_pred = state.get("_budget_prediction", {})
    _today = datetime.now().strftime("%Y-%m-%d")
    if not _budget_pred.get("date") == _today:
        try:
            wx_data = fetch_weather()
            if "error" not in wx_data:
                hourly = wx_data.get("hourly", {})
                temps = hourly.get("temperature_2m", [])
                if temps:
                    _total_kwh = 0
                    for i, t_out in enumerate(temps[:24]):
                        _h = int(hourly["time"][i][11:13]) if i < len(hourly.get("time", [])) else i
                        if t_out > 26:
                            _hours_cooling = min(1, (t_out - 26) / 6)
                            _kwh = kwh_est(60 * _hours_cooling, COOL_DUTY)
                            _total_kwh += _kwh
                    state["_budget_prediction"] = {
                        "date": _today, "predicted_kwh": round(_total_kwh, 2),
                        "predicted_cost": round(_total_kwh * 0.5, 2),
                        "max_temp": max(temps) if temps else None,
                    }
        except: pass
    # 压缩机健康监控
    _cycle_log = state.get("_cycle_log", [])
    if len(_cycle_log) >= 5:
        _recent = [c.get("duration_min", 0) for c in _cycle_log[-5:]]
        _avg = sum(_recent) / len(_recent)
        if _avg < 15: state["_compressor_health"] = "short_cycling"
        elif _avg > 40: state["_compressor_health"] = "long_running"
        else: state["_compressor_health"] = "normal"
    learned["adjusted_thresholds"] = adjusted
    learned["decision_log"] = log[-50:]
    save_learned(learned)

def log_decision(state, action, pre_temp, pre_hum, now_ts):
    learned = load_learned()
    log = learned.get("decision_log", [])
    power_at_decision = state.get("_prev_power") or state.get("measured_w")
    log.append({"time": now_ts, "action": action, "pre_temp": pre_temp,
                "pre_hum": pre_hum, "evaluated": False, "power_at_decision": power_at_decision})
    learned["decision_log"] = log[-50:]
    save_learned(learned)

# ── 热质量学习 ──
THERMAL_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ac_thermal.json")

def load_thermal_data():
    default = {"events": [], "thermal_model": {"cooling_rate_per_min": 0.05, "warmup_rate_per_min": 0.02, "time_constant_min": 120}}
    try:
        if os.path.exists(THERMAL_FILE):
            with open(THERMAL_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: pass
    return default

def save_thermal_data(data):
    tmp = THERMAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, THERMAL_FILE)

def _thermal_event_usable(e):
    if not isinstance(e, dict): return False
    for k in ("temp_before", "temp_after", "duration_min"):
        v = e.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool): return False
    return e["duration_min"] > 0

def record_thermal_event(event_type, temp_before, temp_after, duration_min, outdoor_temp):
    if temp_before is None: return False
    data = load_thermal_data()
    events = data.get("events", [])
    events.append({"type": event_type, "temp_before": temp_before, "temp_after": temp_after,
                    "duration_min": duration_min, "outdoor_temp": outdoor_temp,
                    "timestamp": datetime.now().isoformat()})
    data["events"] = events[-100:]
    _last_fit = data.get("_last_fit_ts")
    _new_count = data.get("_new_event_count", 0) + 1
    data["_new_event_count"] = _new_count
    _should_fit = _new_count >= 5
    if not _should_fit and _last_fit:
        try:
            if (datetime.now() - datetime.fromisoformat(_last_fit)).total_seconds() > 86400: _should_fit = True
        except: _should_fit = True
    elif not _last_fit: _should_fit = True
    if _should_fit:
        data["thermal_model"] = fit_thermal_model(data["events"])
        data["_last_fit_ts"] = datetime.now().isoformat()
        data["_new_event_count"] = 0
    save_thermal_data(data)
    return True

def fit_thermal_model(events):
    usable = [e for e in (events or []) if _thermal_event_usable(e)]
    cooling = [e for e in usable if e.get("type") == "cooling"]
    warming = [e for e in usable if e.get("type") == "warming"]
    model = {"thermal_conductance": 0.003, "baseline_cooling": -0.035, "time_constant_min": 120}
    if len(cooling) >= 3:
        X, y = [], []
        for e in cooling[-30:]:
            t_in = e["temp_before"]; t_out = e["outdoor_temp"] or t_in
            rate = (e["temp_after"] - t_in) / e["duration_min"]
            X.append([t_out - t_in, 1.0]); y.append(rate)
        if len(X) >= 3:
            import numpy as np
            coeffs, _, _, _ = np.linalg.lstsq(np.array(X), np.array(y), rcond=None)
            a, c = coeffs
            if -0.01 < a < 0.1 and -0.2 < c < 0.05:
                model["thermal_conductance"] = float(a)
                model["baseline_cooling"] = float(c)
                if abs(a) > 0.0001: model["time_constant_min"] = round(1.0 / abs(a), 1)
    if len(warming) >= 3:
        rates = [(e["temp_after"] - e["temp_before"]) / max(e["duration_min"], 1) for e in warming[-20:]]
        rates = [r for r in rates if r > 0]
        if rates: model["warmup_rate_per_min"] = sum(rates) / len(rates)
    return model

def predict_cooling_time(temp_current, temp_target, outdoor_temp, thermal_model):
    a = thermal_model.get("thermal_conductance", 0.003)
    c = thermal_model.get("baseline_cooling", -0.035)
    diff = temp_current - temp_target
    if diff <= 0: return 0
    t, minutes = temp_current, 0
    while t > temp_target and minutes < 600:
        t += a * (outdoor_temp - t) + c; minutes += 1
    return minutes

def dehumid_duty(temp, hum=None):
    if temp is None: base = DEHUMID_DUTY
    elif temp >= 26: base = DEHUMID_DUTY
    elif temp <= TEMP_ABSOLUTE_FLOOR: base = 0.25
    else: base = 0.25 + (DEHUMID_DUTY - 0.25) * (temp - TEMP_ABSOLUTE_FLOOR) / (26 - TEMP_ABSOLUTE_FLOOR)
    if hum is not None:
        if hum >= 85: factor = 1.10
        elif hum <= 60: factor = 0.70
        else: factor = 0.70 + 0.40 * (hum - 60) / 25.0
        base *= factor
    return base

def kwh_est(active_min, duty=1.0):
    p = AC_MEASURED_W or AC_INPUT_W
    return p / 1000.0 * duty * (active_min / 60.0)

def current_price():
    h = datetime.now().hour
    return ELECTRIC_VALLEY if h >= 22 or h < 6 else ELECTRIC_PEAK

def cost_est(kwh): return kwh * current_price()

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "ac_state.json")
LAT, LON = 31.11, 121.38

def _load_env():
    f = os.path.join(SCRIPT_DIR, ".env")
    try:
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
    except OSError: pass

_load_env()
QW_HOST = os.environ.get("QW_HOST", "kf54e6wb7f.re.qweatherapi.com")
QW_KEY = os.environ.get("QW_API_KEY", "")

def _qw_get(endpoint):
    import gzip
    if not QW_KEY: raise RuntimeError("QW_API_KEY 未配置")
    url = f"https://{QW_HOST}/weather/v1/{endpoint}/{LAT}/{LON}"
    req = urllib.request.Request(url, headers={"X-QW-Api-Key": QW_KEY, "Accept-Encoding": "identity"})
    resp = urllib.request.urlopen(req, timeout=15)
    body = resp.read()
    if body[:2] == b"\x1f\x8b": body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))

CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")

WEATHER_MAP = {0: "☀️ 晴", 1:"🌤 少云", 2:"⛅ 多云", 3:"☁️ 阴",
    45:"🌫 雾", 51:"🌦 毛毛雨",61:"🌧 小雨",63:"🌧 中雨",65:"🌧 大雨",
    71:"🌨 小雪",73:"🌨 中雪",75:"🌨 大雪",
    80:"🌦 阵雨",81:"🌦 小阵雨",82:"🌦 大阵雨",95:"⛈ 雷暴"}

def weather_cn(code): return WEATHER_MAP.get(code, f"☁️ {code}")

def load_state():
    default = {"mode": None, "last_on_at": None, "last_off_at": None, "run_start": None}
    if not os.path.exists(STATE_FILE): return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8-sig") as f: return {**default, **json.load(f)}
    except Exception as e:
        default["_state_load_failed"] = True; return default

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def minutes_since(ts_str):
    if not ts_str: return None
    try:
        then = datetime.fromisoformat(ts_str)
        now = datetime.now(tz=then.tzinfo if then.tzinfo else None)
        return (now - then).total_seconds() / 60.0
    except: return None

def fetch_weather():
    try:
        CST = timezone(timedelta(hours=8))
        cur = _qw_get("current"); dai = _qw_get("daily")["days"][0]; hrs = _qw_get("hourly")["hours"]
        QW2WMO = {100:0, 101:2, 102:2, 103:3, 104:3, 200:0, 201:0, 202:0, 203:0,
                  300:0, 301:1, 302:2, 303:95, 304:95, 400:0, 401:0, 402:0, 403:0,
                  500:45, 501:45, 502:45, 503:45, 504:45, 507:45, 508:45, 509:45,
                  510:51, 511:51, 512:51, 513:51, 514:51, 600:61, 601:61, 602:63, 603:65,
                  305:61, 306:63, 307:65, 610:80, 611:80, 612:80, 613:80,
                  700:45, 701:45, 702:45, 703:45, 704:45, 800:95, 801:95, 802:95, 803:95, 804:95}
        wmo = QW2WMO.get(int(cur.get("condition",{}).get("code", 0)), 0)
        day_prec = dai.get("daytime", {}).get("precipitation", {})
        rain_prob = day_prec.get("probability", 0) if isinstance(day_prec, dict) else 0
        times = []
        for h in hrs:
            t_utc = datetime.fromisoformat(h["forecastTime"].replace("Z", "+00:00"))
            t_local = t_utc.astimezone(CST)
            times.append(t_local.strftime("%Y-%m-%dT%H:%M"))
        return {
            "current": {"temperature_2m": cur["temperature"]["value"],
                        "apparent_temperature": cur["feelsLike"]["value"],
                        "relative_humidity_2m": round(cur["humidity"] * 100), "weather_code": wmo},
            "daily": {"temperature_2m_max": [dai["temperatureMax"]["value"]],
                        "temperature_2m_min": [dai["temperatureMin"]["value"]],
                        "precipitation_probability_max": [round(rain_prob * 100)]},
            "hourly": {"time": times,
                        "temperature_2m": [h["temperature"]["value"] for h in hrs],
                        "relative_humidity_2m": [round(h["humidity"] * 100) for h in hrs],
                        "precipitation_probability": [
                            round(h["precipitation"]["probability"] * 100) if isinstance(h.get("precipitation"), dict) else 0
                            for h in hrs]},
        }
    except Exception as e: return {"error": str(e)}

def read_indoor(timeout=3.0):
    if not os.path.exists(CONFIG_FILE): return None, None
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f: cfg = json.load(f)
    except: return None, None
    ip, token = cfg.get("ip"), cfg.get("token")
    if not ip or not token: return None, None
    temp, hum = _read_indoor_once(ip, token, timeout=timeout)
    if temp is not None: return temp, hum
    return _read_indoor_once(ip, token, 5)

def _read_indoor_once(ip, token, timeout):
    try:
        from miio import Device
        d = Device(ip, token, timeout=timeout)
        r = d.send("get_properties", [{"siid": 3, "piid": 7}, {"siid": 3, "piid": 1}])
        if isinstance(r, list) and len(r) >= 2:
            temp = r[0].get("value") if isinstance(r[0], dict) else None
            hum = r[1].get("value") if isinstance(r[1], dict) else None
            if temp is not None and hum is not None: return round(temp, 1), round(hum, 0)
    except: pass
    return None, None

AC_MEASURED_W = None; AC_SOCKET = None

def ac_control_init():
    global AC_CTRL; AC_CTRL = None
    try:
        with open(CONFIG_FILE) as f: cfg = json.load(f)
        ap = cfg.get("ac_partner") or {}
        if ap.get("ip") and ap.get("token") and cfg.get("ac_control", True):
            from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02
            AC_CTRL = AirConditioningCompanionMcn02(ap["ip"], ap["token"])
    except: AC_CTRL = None

def ac_apply(new_mode, target_temp=None):
    if new_mode == "dehumid_alert": return {"status": "no_action", "action": "", "reason": "alert_only"}
    if AC_CTRL is None: return {"status": "failed", "action": "", "reason": "control_unavailable"}
    try: st = AC_CTRL.status()
    except Exception as e: return {"status": "failed", "action": "", "reason": f"status_read_failed: {e}"}
    on = st.is_on; act = []
    if new_mode in ("cooling", "dehumid"):
        want_mode = "cool" if new_mode == "cooling" else "dry"
        if not on:
            try: AC_CTRL.send_command("set_power", ["on"]); act.append("开机"); on = True
            except Exception as e: return {"status": "failed", "action": "开机", "reason": f"power_on_failed: {e}"}
        try:
            if st.mode is not None and st.mode.value != want_mode:
                AC_CTRL.send_command("set_mode", [want_mode]); act.append(f"模式{want_mode}")
        except: pass
        if want_mode == "dry": pass
        else:
            try:
                if target_temp and st.target_temperature != target_temp:
                    AC_CTRL.send_command("set_tar_temp", [target_temp]); act.append(f"设定{target_temp}°C")
            except: pass
    elif new_mode == "fan_locked": pass
    elif new_mode in ("fan", "off"):
        if on:
            try: AC_CTRL.send_command("set_power", ["off"]); act.append("关机")
            except Exception as e: return {"status": "failed", "action": "关机", "reason": f"power_off_failed: {e}"}
    return {"status": "action" if act else "no_action", "action": "，".join(act), "reason": ""}

def reconcile_state(state, now_ts):
    if AC_SOCKET == "off" and state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
        sys_off = state.get("_system_off_at"); is_system_off = False
        if sys_off:
            try:
                sys_off_dt = datetime.fromisoformat(sys_off) if isinstance(sys_off, str) else sys_off
                now_dt = datetime.fromisoformat(now_ts) if isinstance(now_ts, str) else now_ts
                if (now_dt - sys_off_dt).total_seconds() < 180: is_system_off = True
            except: pass
        never_ran = not state.get("run_start")
        if not is_system_off and not never_ran: state["manual_off_at"] = now_ts
        state["mode"] = "off"; state["last_off_at"] = now_ts; state["run_start"] = None
        state.pop("_system_off_at", None); return
    if state.get("_system_off_at"): state.pop("_system_off_at", None)
    if AC_SOCKET == "on" and state.get("mode") not in ("cooling", "dehumid", "dehumid_alert"):
        state["manual_on_at"] = now_ts; state["mode"] = "cooling"
        state["run_start"] = now_ts; state["_fake_run_count"] = 0
        _learn_from_manual(state, now_ts)

def verify_socket():
    if AC_CTRL is None: return None
    try: s = AC_CTRL.status(); return "on" if s.is_on else "off"
    except: return None

def apply_state_from_verify(state, new_mode, real, now_ts):
    was_on = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    if real == "on":
        if new_mode in ("cooling", "dehumid", "dehumid_alert"):
            if not was_on: state["run_start"] = now_ts; state["last_on_at"] = now_ts
            state["mode"] = new_mode; return None
        state["mode"] = "cooling"; state.pop("last_off_at", None); return True
    if new_mode in ("fan", "fan_locked", "off"):
        if was_on: state["last_off_at"] = now_ts; state["_system_off_at"] = now_ts
        state["mode"] = new_mode; state["run_start"] = None; return None
    state["mode"] = "off"; state["run_start"] = None; return True

def apply_and_commit(new_mode, target_temp, state, now_ts=None, meta=None, tts_reason=None):
    if now_ts is None: now_ts = datetime.now().isoformat(timespec="seconds")
    ctrl = ac_apply(new_mode, target_temp)
    if ctrl["status"] == "failed": save_state(state); return ctrl
    real = verify_socket()
    if real is None:
        ctrl = {"status": "failed", "action": ctrl.get("action", ""), "reason": "verify_unreachable"}
        save_state(state); return ctrl
    contradict = apply_state_from_verify(state, new_mode, real, now_ts)
    if contradict:
        ctrl = {"status": "failed", "action": ctrl.get("action", ""),
                "reason": "verify_on_after_off" if real == "on" else "verify_off_after_on"}
    if meta and not contradict:
        for k, v in meta.items(): state[k] = v
    if not contradict and target_temp is not None: state["target_temp"] = target_temp
    save_state(state)
    if tts_reason and not contradict:
        try:
            from ac_watch import tts_speak
            tts_speak(tts_reason)
        except: pass
    return ctrl

def read_ac_power(timeout=4.0):
    global AC_MEASURED_W, AC_SOCKET; AC_SOCKET = None
    try:
        with open(CONFIG_FILE) as f: cfg = json.load(f)
        ap = cfg.get("ac_partner") or {}
        if not ap.get("ip") or not ap.get("token"): return None
        from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02
        d = AirConditioningCompanionMcn02(ap["ip"], ap["token"])
        st = d.status(); AC_SOCKET = "on" if st.is_on else "off"; AC_MEASURED_W = None
        if st.is_on and st.load_power and st.load_power > 0:
            AC_MEASURED_W = round(st.load_power); return AC_MEASURED_W
    except: pass
    return None

NIGHT_HOURS = 6; DAYS_PER_MONTH = 30

def _learn_from_manual(state, now_ts):
    try:
        rh_hist = state.get("rh_history", [])
        if not rh_hist: return
        last_rh = rh_hist[-1][1] if rh_hist else None
        if last_rh is None: return
        pref = state.get("user_pref", {})
        manual_log = pref.get("manual_on_log", [])
        manual_log.append({"ts": now_ts, "rh": last_rh, "mode": state.get("mode")})
        if len(manual_log) > 20: manual_log = manual_log[-20:]
        pref["manual_on_log"] = manual_log
        low_rh_manual = [m for m in manual_log if 60 <= m.get("rh", 0) < 65]
        if len(low_rh_manual) >= 3: pref["hum_threshold"] = 60
        else: pref.pop("hum_threshold", None)
        recent = manual_log[-10:]
        manual_on_count = sum(1 for m in recent if m.get("mode") == "cooling")
        manual_off_count = sum(1 for m in recent if m.get("mode") == "off")
        try:
            with open(os.path.join(SCRIPT_DIR, "ac_user_pref.json"), "r") as f: user_pref = json.load(f)
            current_cw = user_pref.get("comfort_weight", 0.5)
        except: current_cw = 0.5
        if manual_on_count >= 3 and current_cw > 0.1:
            user_pref["comfort_weight"] = round(max(0.1, current_cw - 0.1), 1)
            try:
                with open(os.path.join(SCRIPT_DIR, "ac_user_pref.json"), "w") as f: json.dump(user_pref, f, indent=2, ensure_ascii=False)
            except: pass
        elif manual_off_count >= 3 and current_cw < 1.0:
            user_pref["comfort_weight"] = round(min(1.0, current_cw + 0.1), 1)
            try:
                with open(os.path.join(SCRIPT_DIR, "ac_user_pref.json"), "w") as f: json.dump(user_pref, f, indent=2, ensure_ascii=False)
            except: pass
        state["user_pref"] = pref
    except: pass

def night_cost_lines(indoor_temp, indoor_hum):
    h = datetime.now().hour
    if not (h >= 20 or h < 6): return []
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
    lines.append(f"   2️⃣ 睡眠+制冷24°C整夜:  {k24:.2f}度 ≈ {k24 * p:.2f}元")
    lines.append(f"   3️⃣ 除湿模式整夜:        {kd:.2f}度 ≈ {kd * p:.2f}元")
    if indoor_hum is not None and indoor_hum > 70: lines.append("   💡 湿度偏高：先压轮24°C到60%再睡")
    else: lines.append("   💡 湿度不高：压轮收工最省")
    return lines

def filter_clean_reminder():
    FILTER_STATE_FILE = os.path.join(SCRIPT_DIR, "filter_state.json")
    FILTER_CLEAN_INTERVAL = 30
    try:
        with open(FILTER_STATE_FILE, encoding="utf-8") as f: last = json.load(f).get("last_clean")
        if not last: return "  💡 记得每 15~30 天洗一次滤网"
        days = (datetime.now() - datetime.fromisoformat(last)).days
        if days > FILTER_CLEAN_INTERVAL: return f"  ⚠️ 该洗滤网了（距上次 {days} 天）"
        return None
    except: return None

if __name__ == "__main__":
    print("⚠️ ac_advisor.py 已合并为纯库，不再独立运行。")
    sys.exit(0)
