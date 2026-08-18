"""
🏠 家庭生活系统 v1.0 · 上海闵行
合并 ac_advisor v10.1 + vent_reminder v2.2 → 统一决策引擎

升级内容：
- 空调 + 开窗 + 风扇联合决策（统一决策表）
- 共享状态文件 home_state.json（合并 ac_state.json + vent 状态）
- 空调运行模式联动：制冷/除湿中 → 不开窗；关着 + 室外舒适 → 开窗
- 保留 ac_advisor 全部功能：舒适度指标、季节自适应、预冷、状态机、自适应学习、
  手动锚点、夜间方案对比、滤网提醒、TTS、24h最优调度、热质量学习、露点判据
- 保留 vent_reminder 全部功能：ACH模型、统一闸门、AQI/PM2.5、风向检查、
   vent_advice、pick_best、daily_report、alert_check、Windows toast
- 统一入口：main() 支持 --alert / --daily 模式
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

# ── 确保能找到 miio（cron 可能用 python3.11，miio 装在 3.12） ──
_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]
for _p in _MIIO_PATHS:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# ══════════════════════════════════════════════════════════════
# ── 共享常量（来自 ac_advisor） ──
# ══════════════════════════════════════════════════════════════
# 阈值
TEMP_COOLING = 28       # 体感≥28 → 制冷
TEMP_DEHUMID_LOW = 26   # 除湿温度下限
TEMP_DEHUMID_HIGH = 28  # 除湿温度上限
HUM_DEHUMID_ON = 65     # 除湿开启湿度阈值
HUM_DEHUMID_OFF = 55    # 除湿关闭湿度阈值（滞回 10%）
TEMP_ABSOLUTE_FLOOR = 24# 除湿温度绝对下限（OR 逃生门）
MIN_RUN = 40            # 开一次至少 40 分钟
MIN_OFF = 30            # 夜间关后至少 30 分钟再开
DAY_MIN_OFF = 15        # 白天关后至少 15 分钟再开
MAX_RUN = 180           # 连续运行超 180 分钟建议切换/关

# 空调功率（松川 KFRd-35GW 定频 1.5 匹）
AC_INPUT_W = 1076
AC_COP = 3.25
ELECTRIC_PEAK = 0.617
ELECTRIC_VALLEY = 0.307
ELECTRIC_PRICE = ELECTRIC_PEAK
DEHUMID_DUTY = 0.60
COOL_DUTY = 0.70
DEHUMID_MIN = 60
COOL_BURST_MIN = 40
FILTER_CLEAN_INTERVAL = 30

# 滤网状态文件
FILTER_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter_state.json")

# ══════════════════════════════════════════════════════════════
# ── 开窗通风常量（来自 vent_reminder） ──
# ══════════════════════════════════════════════════════════════
LAT, LON = 31.11, 121.38
BASE_ACH = 47.0
BASE_WIND = 6.9  # 4窗穿堂风: 6.9m/s -> ACH 47
MIX_EFF = 0.7
SAFETY = 1.2

# 硬闸门阈值
RAIN_PP_MAX = 45
RAIN_MM_MAX = 1.0
DEW_DELTA_MAX = 1.5
PM25_MAX = 75
WIND_MAX_MS = 10.8
GUST_MAX_MS = 15.0
AC_BLOCK_MODES = ("cooling", "dehumid", "dehumid_alert")

# ══════════════════════════════════════════════════════════════
# ── 和风天气 API ──
# ══════════════════════════════════════════════════════════════
QW_HOST = "kf54e6wb7f.re.qweatherapi.com"
QW_KEY = "e630a3166d6f4146be43fa822cea63a1"

# 和风天气 code → WMO 近似值
QW2WMO = {
    100:0, 101:2, 102:2, 103:3, 104:3,
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
    800:95, 801:95, 802:95, 803:95, 804:95,
}

WEATHER_MAP = {
    0: "☀️ 晴", 1:"🌤 少云", 2:"⛅ 多云", 3:"☁️ 阴",
    45:"🌫 雾",
    51:"🌦 毛毛雨",61:"🌧 小雨",63:"🌧 中雨",65:"🌧 大雨",
    71:"🌨 小雪",73:"🌨 中雪",75:"🌨 大雪",
    80:"🌦 阵雨",81:"🌦 小阵雨",82:"🌦 大阵雨",95:"⛈ 雷暴",
}

# ══════════════════════════════════════════════════════════════
# ── 文件路径 ──
# ══════════════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "home_state.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")
LEARN_FILE = os.path.join(SCRIPT_DIR, "ac_learned.json")
THERMAL_FILE = os.path.join(SCRIPT_DIR, "ac_thermal.json")
ERR_STATE_FILE = os.path.join(SCRIPT_DIR, "vent_error_state.json")

# ══════════════════════════════════════════════════════════════
# ── 空调状态机 ──
# ══════════════════════════════════════════════════════════════
class ACState(Enum):
    """空调运行状态枚举"""
    OFF = "off"
    COOLING = "cooling"
    COOLING_MAINTAIN = "cooling_maintain"
    DEHUMID = "dehumid"
    FAN = "fan"
    FAN_LOCKED = "fan_locked"

TRANSITIONS = {
    ACState.OFF: {ACState.COOLING, ACState.DEHUMID, ACState.FAN},
    ACState.COOLING: {ACState.COOLING_MAINTAIN, ACState.OFF, ACState.FAN},
    ACState.COOLING_MAINTAIN: {ACState.OFF, ACState.COOLING},
    ACState.DEHUMID: {ACState.OFF, ACState.FAN},
    ACState.FAN: {ACState.OFF, ACState.COOLING, ACState.DEHUMID},
    ACState.FAN_LOCKED: {ACState.OFF, ACState.FAN},
}

def transition(current: ACState, target: ACState) -> ACState:
    """状态转移：合法直接转，非法保持现状"""
    allowed = TRANSITIONS.get(current, set())
    if target in allowed:
        return target
    return current

# ══════════════════════════════════════════════════════════════
# ── 舒适度 / 露点 / 闷度 ──
# ══════════════════════════════════════════════════════════════
def comfort_index(temp, hum):
    """酷度指数 = T + 0.05×(RH-10)"""
    if hum is None:
        return temp
    return temp + 0.05 * (hum - 10)

def dew_point(temp_c, rh):
    """Magnus 公式近似露点（°C）。"""
    if temp_c is None or rh is None or rh <= 0 or rh > 100:
        return None
    a, b = 17.27, 237.7
    alpha = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    td = (b * alpha) / (a - alpha)
    return td

def muggy_level(temp, hum):
    """0=comfort, 1=slight muggy, 2=muggy, 3=very muggy"""
    dp = dew_point(temp, hum)
    if dp is None:
        return 0
    if dp < 12: return 0
    elif dp < 16: return 1
    elif dp < 18: return 2
    else: return 3

# ══════════════════════════════════════════════════════════════
# ── 季节自适应 ──
# ══════════════════════════════════════════════════════════════
def seasonal_adjustments():
    """根据月份自动切换：
    盛夏(7-8): 正常制冷；梅雨(6): 除湿优先；春秋(4-5/9-10): 风扇优先；冬季(11-3): 关窗优先
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

# ══════════════════════════════════════════════════════════════
# ── 预测式预冷 ──
# ══════════════════════════════════════════════════════════════
def should_precool(wx, current_hi, threshold_hi):
    """如果未来 3 小时内 HI 超过阈值 +3°C，建议提前预冷"""
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

# ══════════════════════════════════════════════════════════════
# ── 24h 最优调度 ──
# ══════════════════════════════════════════════════════════════
def compute_optimal_schedule(wx, current_temp, current_hum, learned):
    """Compute optimal AC schedule for next 24h"""
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    hums = hourly.get("relative_humidity_2m", [])
    if not times:
        return []
    schedule = []
    for i, t in enumerate(times):
        hour = int(t[11:13]) if len(t) >= 13 else i
        temp = temps[i] if i < len(temps) else None
        hum = hums[i] if i < len(hums) else None
        if temp is None:
            continue
        hi = comfort_index(temp, hum)
        price = ELECTRIC_VALLEY if hour >= 22 or hour < 6 else ELECTRIC_PEAK
        if hi >= TEMP_COOLING + seasonal_adjustments()[0]:
            action = "cool"
            est_cost = kwh_est(40, COOL_DUTY) * price
        elif muggy_level(temp, hum) >= 2:
            action = "dehumid"
            est_cost = kwh_est(60, dehumid_duty(temp, hum)) * price
        else:
            action = "off"
            est_cost = 0
        schedule.append((hour, action, est_cost))
    return schedule

def find_pre_cool_window(schedule, current_hour):
    """Find cheapest pre-cooling window before hot period"""
    hot_start = None
    for i, (h, action, cost) in enumerate(schedule):
        if action == "cool":
            if hot_start is None:
                hot_start = h
        else:
            if hot_start is not None:
                break
    if hot_start is None:
        return None
    pre_cool_start = max(0, hot_start - 3)
    pre_cool_end = max(0, hot_start - 1)
    valley_cost = ELECTRIC_VALLEY * kwh_est(40, COOL_DUTY)
    peak_cost = ELECTRIC_PEAK * kwh_est(40, COOL_DUTY)
    savings = peak_cost - valley_cost
    return (pre_cool_start, pre_cool_end, savings)

# ══════════════════════════════════════════════════════════════
# ── 自适应阈值学习 ──
# ══════════════════════════════════════════════════════════════
def load_learned() -> dict:
    default = {"adjusted_thresholds": {}, "decision_log": []}
    try:
        if os.path.exists(LEARN_FILE):
            with open(LEARN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_learned(learned: dict):
    tmp = LEARN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEARN_FILE)

def evaluate_and_learn(state, now_ts):
    learned = load_learned()
    log = learned.get("decision_log", [])
    cutoff = (datetime.now() - timedelta(minutes=30)).isoformat()
    adjusted = learned.get("adjusted_thresholds", {})
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
            if temp_drop < 0.3 and hum_drop < 3:
                success = False
        elif action in ("off", "fan"):
            temp_rise = cur_temp - pre_temp
            if temp_rise > 2.0 or (cur_hum is not None and cur_hum > 80):
                success = False
        if not success:
            if action in ("cooling", "dehumid"):
                cur_adj = adjusted.get("temp_cooling", 0)
                adjusted["temp_cooling"] = cur_adj - 1
            elif action in ("off", "fan"):
                cur_adj = adjusted.get("temp_cooling", 0)
                adjusted["temp_cooling"] = cur_adj - 1
        entry["evaluated"] = True
    learned["adjusted_thresholds"] = adjusted
    learned["decision_log"] = log[-50:]
    save_learned(learned)

def log_decision(state, action, pre_temp, pre_hum, now_ts):
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

# ══════════════════════════════════════════════════════════════
# ── 热质量学习 ──
# ══════════════════════════════════════════════════════════════
def load_thermal_data() -> dict:
    default = {"events": [], "thermal_model": {"cooling_rate_per_min": 0.05, "warmup_rate_per_min": 0.02, "time_constant_min": 120}}
    try:
        if os.path.exists(THERMAL_FILE):
            with open(THERMAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_thermal_data(data: dict):
    tmp = THERMAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, THERMAL_FILE)

def record_thermal_event(event_type, temp_before, temp_after, duration_min, outdoor_temp):
    data = load_thermal_data()
    events = data.get("events", [])
    events.append({
        "type": event_type,
        "temp_before": temp_before,
        "temp_after": temp_after,
        "duration_min": duration_min,
        "outdoor_temp": outdoor_temp,
        "timestamp": datetime.now().isoformat()
    })
    data["events"] = events[-100:]
    data["thermal_model"] = fit_thermal_model(data["events"])
    save_thermal_data(data)

def fit_thermal_model(events):
    cooling = [e for e in events if e["type"] == "cooling"]
    warming = [e for e in events if e["type"] == "warming"]
    model = {"cooling_rate_per_min": 0.05, "warmup_rate_per_min": 0.02, "time_constant_min": 120}
    if cooling:
        rates = [(e["temp_before"] - e["temp_after"]) / max(e["duration_min"], 1) for e in cooling[-20:]]
        model["cooling_rate_per_min"] = sum(rates) / len(rates)
    if warming:
        rates = [(e["temp_after"] - e["temp_before"]) / max(e["duration_min"], 1) for e in warming[-20:]]
        model["warmup_rate_per_min"] = sum(rates) / len(rates)
    return model

def predict_cooling_time(temp_current, temp_target, outdoor_temp, thermal_model):
    rate = thermal_model.get("cooling_rate_per_min", 0.05)
    diff = temp_current - temp_target
    if diff <= 0:
        return 0
    return int(diff / rate)

# ══════════════════════════════════════════════════════════════
# ── 功率/电费估算 ──
# ══════════════════════════════════════════════════════════════
AC_MEASURED_W = None
AC_SOCKET = None
AC_CTRL = None

def dehumid_duty(temp, hum=None):
    """定频除湿占空比"""
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
    """耗电估算(度)"""
    p = AC_MEASURED_W or AC_INPUT_W
    return p / 1000.0 * duty * (active_min / 60.0)

def current_price():
    h = datetime.now().hour
    return ELECTRIC_VALLEY if h >= 22 or h < 6 else ELECTRIC_PEAK

def cost_est(kwh):
    return kwh * current_price()

# ══════════════════════════════════════════════════════════════
# ── 共享状态文件 ──
# ══════════════════════════════════════════════════════════════
def load_state() -> dict:
    default = {
        "ac": {"mode": None, "run_start": None, "last_off_at": None, "target_temp": 26, "manual_off_at": None},
        "window": {"last_open": None, "last_close": None, "open_duration_min": 30},
        "fan": {"last_circulate": None},
        "thermal": {"cooling_rate_per_min": 0.05, "warmup_rate_per_min": 0.02},
        "learned": {"adjusted_thresholds": {}, "decision_log": []},
        "last_temp": None,
        "last_hum": None,
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        # 合并保存的字段
        for k, v in data.items():
            if isinstance(v, dict) and k in default:
                default[k].update(v)
            else:
                default[k] = v
        return default
    except Exception as e:
        print(f"[ERROR] state load failed: {type(e).__name__}: {e}")
        default["_state_load_failed"] = True
        return default

def save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

# 兼容旧 ac_state.json（迁移）
def migrate_legacy_state():
    legacy_file = os.path.join(SCRIPT_DIR, "ac_state.json")
    if os.path.exists(legacy_file) and not os.path.exists(STATE_FILE):
        try:
            with open(legacy_file, "r", encoding="utf-8-sig") as f:
                legacy = json.load(f)
            new_state = load_state()
            new_state["ac"]["mode"] = legacy.get("mode")
            new_state["ac"]["run_start"] = legacy.get("run_start")
            new_state["ac"]["last_off_at"] = legacy.get("last_off_at")
            new_state["ac"]["target_temp"] = legacy.get("target_temp", 26)
            new_state["ac"]["manual_off_at"] = legacy.get("manual_off_at")
            new_state["last_temp"] = legacy.get("last_temp")
            new_state["last_hum"] = legacy.get("last_hum")
            save_state(new_state)
            print("[INFO] 已迁移旧 ac_state.json → home_state.json")
        except Exception as e:
            print(f"[WARN] 迁移失败: {e}")

def minutes_since(ts_str):
    if not ts_str:
        return None
    try:
        then = datetime.fromisoformat(ts_str)
        now = datetime.now(tz=then.tzinfo if then.tzinfo else None)
        return (now - then).total_seconds() / 60.0
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
# ── 和风天气 API ──
# ══════════════════════════════════════════════════════════════
def _qw_get(endpoint: str) -> dict:
    url = f"https://{QW_HOST}/weather/v1/{endpoint}/{LAT}/{LON}"
    req = urllib.request.Request(url, headers={"X-QW-Api-Key": QW_KEY, "Accept-Encoding": "identity"})
    resp = urllib.request.urlopen(req, timeout=15)
    body = resp.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))

def weather_cn(code):
    return WEATHER_MAP.get(code, f"☁️ {code}")


def fetch_weather() -> dict:
    """获取天气数据（和风天气 CMA 数据源），返回 Open-Meteo 兼容格式"""
    try:
        CST = timezone(timedelta(hours=8))
        cur = _qw_get("current")
        dai = _qw_get("daily")["days"][0]
        hrs = _qw_get("hourly")["hours"]
        wmo = QW2WMO.get(int(cur.get("condition",{}).get("code", 0)), 0)
        day_prec = dai.get("daytime", {}).get("precipitation", {})
        rain_prob = day_prec.get("probability", 0) if isinstance(day_prec, dict) else 0
        times = []
        for h in hrs:
            t_utc = datetime.fromisoformat(h["forecastTime"].replace("Z", "+00:00"))
            t_local = t_utc.astimezone(CST)
            times.append(t_local.strftime("%Y-%m-%dT%H:%M"))
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
                "temperature_2m": [h["temperature"]["value"] for h in hrs],
                "relative_humidity_2m": [round(h["humidity"] * 100) for h in hrs],
                "precipitation_probability": [
                    round(h["precipitation"]["probability"] * 100) if isinstance(h.get("precipitation"), dict) else 0
                    for h in hrs
                ],
            },
        }
    except Exception as e:
        return {"error": str(e)}


def fetch_forecast(days=1):
    """逐小时预报（和风天气 CMA 数据源）- 含风向/阵风/降雨强度"""
    CST = timezone(timedelta(hours=8))
    url = f"https://{QW_HOST}/weather/v1/hourly/{LAT}/{LON}"
    req = urllib.request.Request(url, headers={"X-QW-Api-Key": QW_KEY, "Accept-Encoding": "identity"})
    resp = urllib.request.urlopen(req, timeout=20)
    body = resp.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    raw = json.loads(body.decode("utf-8"))
    times, rh, pp, prec, temp, ws, wd, gusts = [], [], [], [], [], [], [], []
    for h in raw.get("hours", []):
        t_utc = datetime.fromisoformat(h["forecastTime"].replace("Z", "+00:00"))
        t_local = t_utc.astimezone(CST)
        times.append(t_local.strftime("%Y-%m-%dT%H:%M"))
        rh.append(float(h.get("humidity", 0)) * 100)
        temp.append(float(h.get("temperature", {}).get("value", 0)))
        prec_obj = h.get("precipitation", {})
        pp.append(float(prec_obj.get("probability", 0)) * 100)
        prec.append(float(prec_obj.get("intensity", {}).get("value", 0)))
        wind_obj = h.get("wind", {})
        ws.append(float(wind_obj.get("speed", {}).get("value", 0)))
        wd.append(float(wind_obj.get("direction", {}).get("degree", 0)))
        gusts.append(float(h.get("windGust", {}).get("value", 0)))
    return {"hourly": {
        "time": times,
        "relative_humidity_2m": rh,
        "precipitation_probability": pp,
        "precipitation": prec,
        "temperature_2m": temp,
        "wind_speed_10m": [w * 3.6 for w in ws],
        "wind_gusts_10m": [g * 3.6 for g in gusts],
        "wind_direction_10m": wd,
    }}


def fetch_aqi(days=1):
    """免费 PM2.5 逐小时预报 (Open-Meteo Air Quality)"""
    try:
        url = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={LAT}&longitude={LON}"
               f"&hourly=pm2_5&forecast_days={days}&timezone=Asia%2FShanghai")
        d = json.load(urllib.request.urlopen(url, timeout=20))
        return dict(zip(d["hourly"]["time"], d["hourly"]["pm2_5"]))
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
# ── 室内传感器 ──
# ══════════════════════════════════════════════════════════════
def read_indoor(timeout=3.0):
    """读取小米空气净化器 4 Lite 的室内温湿度"""
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
    try:
        from miio import Device
        d = Device(ip, token, timeout=timeout)
        r = d.send("get_properties", [
            {"siid": 3, "piid": 7},
            {"siid": 3, "piid": 1},
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

# ══════════════════════════════════════════════════════════════
# ── 空调控制 ──
# ══════════════════════════════════════════════════════════════
def ac_control_init():
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
    """以插座实测为权威对账持久化状态"""
    ac_state = state.get("ac", {})
    if AC_SOCKET == "off" and ac_state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
        sys_off = ac_state.get("_system_off_at")
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
            ac_state["manual_off_at"] = now_ts
        ac_state["mode"] = "off"
        ac_state["last_off_at"] = now_ts
        ac_state["run_start"] = None
        ac_state.pop("_system_off_at", None)
        state["ac"] = ac_state
        return
    if ac_state.get("_system_off_at"):
        ac_state.pop("_system_off_at", None)
    if AC_SOCKET == "on" and ac_state.get("mode") not in ("cooling", "dehumid", "dehumid_alert"):
        ac_state["manual_on_at"] = now_ts
        ac_state["mode"] = "cooling"
        ac_state["run_start"] = now_ts
        ac_state["_fake_run_count"] = 0
        state["ac"] = ac_state
        _learn_from_manual(state, now_ts)


def verify_socket():
    if AC_CTRL is None:
        return None
    try:
        s = AC_CTRL.status()
        return "on" if s.is_on else "off"
    except Exception:
        return None


def apply_state_from_verify(state, new_mode, real, now_ts):
    ac_state = state.get("ac", {})
    was_on = ac_state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    if real == "on":
        if new_mode in ("cooling", "dehumid", "dehumid_alert"):
            if not was_on:
                ac_state["run_start"] = now_ts
                ac_state["last_on_at"] = now_ts
            ac_state["mode"] = new_mode
            state["ac"] = ac_state
            return None
        ac_state["mode"] = "cooling"
        ac_state.pop("last_off_at", None)
        state["ac"] = ac_state
        return True
    if new_mode in ("fan", "fan_locked", "off"):
        if was_on:
            ac_state["last_off_at"] = now_ts
            ac_state["_system_off_at"] = now_ts
        ac_state["mode"] = new_mode
        ac_state["run_start"] = None
        state["ac"] = ac_state
        return None
    ac_state["mode"] = "off"
    ac_state["run_start"] = None
    state["ac"] = ac_state
    return True


def apply_and_commit(new_mode, target_temp, state, now_ts=None, meta=None):
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
        ac_state = state.get("ac", {})
        for k, v in meta.items():
            ac_state[k] = v
        state["ac"] = ac_state
    if not contradict and target_temp is not None:
        ac_state = state.get("ac", {})
        ac_state["target_temp"] = target_temp
        state["ac"] = ac_state
    save_state(state)
    return ctrl


def read_ac_power(timeout=4.0):
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

# ══════════════════════════════════════════════════════════════
# ── 用户习惯学习 ──
# ══════════════════════════════════════════════════════════════
def _learn_from_manual(state, now_ts):
    try:
        ac_state = state.get("ac", {})
        rh_hist = state.get("rh_history", [])
        if not rh_hist:
            return
        last_rh = rh_hist[-1][1] if rh_hist else None
        if last_rh is None:
            return
        pref = ac_state.get("user_pref", {})
        manual_log = pref.get("manual_on_log", [])
        manual_log.append({"ts": now_ts, "rh": last_rh, "mode": ac_state.get("mode")})
        if len(manual_log) > 20:
            manual_log = manual_log[-20:]
        pref["manual_on_log"] = manual_log
        low_rh_manual = [m for m in manual_log if 60 <= m.get("rh", 0) < 65]
        if len(low_rh_manual) >= 3:
            pref["hum_threshold"] = 60
        else:
            pref.pop("hum_threshold", None)
        ac_state["user_pref"] = pref
        state["ac"] = ac_state
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════
# ── 夜间方案对比 ──
# ══════════════════════════════════════════════════════════════
NIGHT_HOURS = 6
DAYS_PER_MONTH = 30

def night_cost_lines(indoor_temp, indoor_hum):
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
    lines.append(f"   3️⃣ 除湿模式整夜:        {kd:.2f}度 ≈ {kd * p:.2f}元（慢且最贵）")
    if indoor_hum is not None and indoor_hum > 70:
        lines.append("   💡 湿度偏高：先压轮24°C到60%再睡")
    else:
        lines.append("   💡 湿度不高：压轮收工最省；怕热醒就睡眠26°C整夜")
    return lines

# ══════════════════════════════════════════════════════════════
# ── 滤网清洗提醒 ──
# ══════════════════════════════════════════════════════════════
def filter_clean_reminder():
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

# ══════════════════════════════════════════════════════════════
# ── ACH 模型（开窗通风） ──
# ══════════════════════════════════════════════════════════════
def ach(w_kmh, dt=0):
    """ACH 模型: 风压项 + 热压项"""
    w = w_kmh / 3.6
    ach_w = BASE_ACH * w / BASE_WIND
    ach_stack = 0.0
    if dt is not None and abs(dt) >= 4:
        ach_stack = 2.5 * (abs(dt) / 8.0) ** 0.5
    return max(0.8, (ach_w ** 2 + ach_stack ** 2) ** 0.5) * MIX_EFF


def t95(w_kmh, dt=0):
    """95% 换气时长(分钟)"""
    a = ach(w_kmh, dt)
    return 3.0 / a * 60.0 * SAFETY if a >= 0.5 else 999.0

# ══════════════════════════════════════════════════════════════
# ── 统一决策闸门（来自 vent_reminder） ──
# ══════════════════════════════════════════════════════════════
def gate_check(rh, pp, rain_mm, temp, wind_ms, gust_ms, pm25,
               indoor_temp, indoor_rh):
    """统一决策闸门"""
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

# ══════════════════════════════════════════════════════════════
# ── vent_advice（开窗建议） ──
# ══════════════════════════════════════════════════════════════
def vent_advice(now_rh, out_rh, now_temp=None, out_temp=None):
    """室内外对比 + 空调联动建议"""
    lines = []
    ac_mode = get_ac_mode()
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
                lines.append(f"⚠️ 室外露点{dp_out:.1f}°C > 室内{dp_in:.1f}°C → 开窗会灌湿气（露点差{ddp:.1f}°C）")
            elif ddp >= 0.5:
                lines.append(f"⚠️ 室外露点略高（{ddp:.1f}°C）→ 开窗请短促")
            else:
                lines.append(f"ℹ️ 室内外露点接近（室内{dp_in:.1f}°C/室外{dp_out:.1f}°C）→ 只为换新鲜空气")
        else:
            diff = out_rh - now_rh
            if diff <= -10:
                lines.append(f"💡 室外比室内干 {abs(diff):.0f}pp → 开窗可顺带除湿 🟢")
            elif diff <= -3:
                lines.append(f"💡 室外略干于室内（{abs(diff):.0f}pp）→ 开窗有利")
            elif diff >= 10:
                lines.append(f"⚠️ 室外比室内潮 {diff:.0f}pp → 开窗会灌湿气")
            elif diff >= 3:
                lines.append(f"⚠️ 室外比室内潮 {diff:.0f}pp → 请短促")
            else:
                lines.append(f"ℹ️ 室内外湿度接近 → 只为换新鲜空气")
    if ac_mode:
        if ac_mode in AC_BLOCK_MODES:
            label = '制冷' if ac_mode == 'cooling' else '除湿'
            lines.append(f"❄️ 空调{label}中 → 开窗会抵消效果，换完即关")
        elif ac_mode == "fan":
            lines.append("🍃 空调风扇 → 开窗与风扇同向")
        elif ac_mode == "off":
            lines.append("🌡 空调关 → 开窗无冲突")
    return lines

# ══════════════════════════════════════════════════════════════
# ── 辅助函数 ──
# ══════════════════════════════════════════════════════════════
def get_ac_mode():
    """获取空调模式（兼容新旧 ac_state.json）"""
    state = load_state()
    ac = state.get("ac", {})
    return ac.get("mode")


def verdict(rh):
    if rh < 60:  return ("极佳", "🟢")
    if rh < 70:  return ("好", "🟢")
    if rh < 78:  return ("一般", "🟡")
    if rh < 85:  return ("谨慎", "🔴")
    return ("不推荐", "🔴")


WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def wind_dir_cn(deg):
    if deg is None:
        return None
    dirs = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return dirs[int((deg % 360) / 45) % 8] + "风"

# ══════════════════════════════════════════════════════════════
# ── 预报数据整理 ──
# ══════════════════════════════════════════════════════════════
def build_rows(h, today):
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
    """统一闸门过滤 + 露点差排序"""
    cand = []
    blocked = None
    for r in rows:
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

# ══════════════════════════════════════════════════════════════
# ── 统一决策引擎 ──
# ══════════════════════════════════════════════════════════════
def unified_decision(wx, indoor_temp, indoor_hum):
    """统一决策：空调 + 开窗 + 风扇
    
    决策表：
    - 空调制冷/除湿中 → 不开窗（冷气跑掉）
    - 空调关着 + 室外舒适 → 开窗
    - 空调关着 + 室外潮湿/有雨 → 关窗 + 风扇
    - 空调关着 + 室外极端热 → 关窗 + 开制冷
    """
    outdoor_temp = wx["current"]["temperature_2m"]
    outdoor_hum = wx["current"]["relative_humidity_2m"]
    
    ac_mode = get_ac_mode()
    ac_running = ac_mode in ("cooling", "dehumid", "dehumid_alert")
    
    # 露点计算
    dp_in = dew_point(indoor_temp, indoor_hum)
    dp_out = dew_point(outdoor_temp, outdoor_hum)
    
    # 舒适指数
    hi_in = comfort_index(indoor_temp, indoor_hum) if indoor_temp and indoor_hum else None
    hi_out = comfort_index(outdoor_temp, outdoor_hum)
    
    # 获取空调状态（含运行时长）
    state = load_state()
    ac_state = state.get("ac", {})
    run_start = ac_state.get("run_start")
    run_mins = minutes_since(run_start)
    off_mins = minutes_since(ac_state.get("last_off_at"))
    
    # ── 空调决策 ──
    # 季节自适应 + 学习修正
    temp_offset, hum_offset, strategy_label = seasonal_adjustments()
    learned = load_learned()
    learned_temp_adj = learned.get("adjusted_thresholds", {}).get("temp_cooling", 0)
    effective_cooling_threshold = TEMP_COOLING + temp_offset + learned_temp_adj
    effective_hum_threshold = HUM_DEHUMID_ON + hum_offset
    
    # 室内信号优先
    signal = indoor_temp if indoor_temp is not None else wx["current"]["apparent_temperature"]
    hum_sig = indoor_hum
    
    # 空调决策
    if ac_running:
        # 已在运行，检查是否应保持
        ac_decision = ac_mode
        ac_target = ac_state.get("target_temp", 26)
    elif hi_in is not None and hi_in >= effective_cooling_threshold:
        ac_decision = "cooling"
        ac_target = round(max(26, min(28, indoor_temp - 2)))
    elif dp_in is not None and dp_in > 16 and hum_sig is not None and hum_sig > effective_hum_threshold:
        ac_decision = "cooling"
        ac_target = 24
    elif hum_sig is not None and hum_sig > effective_hum_threshold:
        ac_decision = "cooling"
        ac_target = 24
    elif signal >= TEMP_DEHUMID_LOW:
        ac_decision = "fan"
        ac_target = None
    else:
        ac_decision = "off"
        ac_target = None
    
    # 最小运行/停机时间约束
    if ac_decision in ("cooling", "dehumid") and off_mins is not None and off_mins < MIN_OFF:
        ac_decision = "fan_locked"
    elif ac_decision in ("off", "fan") and run_mins is not None and run_mins < MIN_RUN:
        ac_decision = ac_mode if ac_running else "off"
    
    # ── 开窗决策（受空调状态影响） ──
    if ac_decision in ("cooling", "dehumid"):
        window_decision = "close"
        window_reason = "空调运行中，开窗浪费冷气"
    elif outdoor_temp > 35:
        window_decision = "close"
        window_reason = "室外极端热，开窗灌热气"
    elif dp_out is not None and dp_in is not None and dp_out > dp_in + DEW_DELTA_MAX:
        window_decision = "close"
        window_reason = f"室外露点高（差{dp_out - dp_in:.1f}°C），开窗灌湿气"
    elif hi_in is not None and hi_in < TEMP_COOLING and outdoor_temp < indoor_temp:
        window_decision = "open"
        window_reason = "室外舒适，开窗通风"
    elif outdoor_hum > 85 and (indoor_hum is None or outdoor_hum > indoor_hum + 10):
        window_decision = "close"
        window_reason = "室外潮湿，关窗防潮"
    else:
        window_decision = "close"
        window_reason = "室外条件一般，关窗+风扇"
    
    # ── 风扇决策 ──
    if ac_decision in ("off", "fan") and window_decision == "close":
        fan_decision = "circulate"
    else:
        fan_decision = "off"
    
    return {
        "ac_mode": ac_decision,
        "ac_target": ac_target,
        "window": window_decision,
        "window_reason": window_reason,
        "fan": fan_decision,
        "outdoor_temp": outdoor_temp,
        "outdoor_hum": outdoor_hum,
        "indoor_temp": indoor_temp,
        "indoor_hum": indoor_hum,
        "hi_in": hi_in,
        "dp_in": dp_in,
        "dp_out": dp_out,
    }

# ══════════════════════════════════════════════════════════════
# ── 统一输出 ──
# ══════════════════════════════════════════════════════════════
def format_output(decision, wx, power, state, ctrl):
    """格式化统一输出"""
    lines = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    wcode = wx["current"]["weather_code"]
    
    lines.append(f"🏠 上海闵行 · 家庭生活系统 v1.0")
    lines.append(f"📅 {now_str} · {weather_cn(wcode)}")
    lines.append("")
    lines.append("🌡️ 环境")
    lines.append(f"  室外: {decision['outdoor_temp']:.1f}°C  体感: {wx['current']['apparent_temperature']:.1f}°C  湿度: {decision['outdoor_hum']:.0f}%  露点: {decision['dp_out']:.1f}°C" if decision['dp_out'] else f"  室外: {decision['outdoor_temp']:.1f}°C  湿度: {decision['outdoor_hum']:.0f}%")
    if decision['indoor_temp'] is not None:
        lines.append(f"  室内: {decision['indoor_temp']:.1f}°C  湿度: {decision['indoor_hum']:.0f}%  露点: {decision['dp_in']:.1f}°C" if decision['dp_in'] else f"  室内: {decision['indoor_temp']:.1f}°C  湿度: {decision['indoor_hum']:.0f}%")
        if decision['hi_in'] is not None:
            lines.append(f"  舒适指数: {decision['hi_in']:.1f}（阈值{TEMP_COOLING}°C）")
    lines.append("")
    
    # 空调
    lines.append("❄️ 空调")
    ac_mode = decision['ac_mode']
    if ac_mode == "cooling":
        lines.append(f"  💡 制冷模式 {decision['ac_target']}°C + 自动风速")
    elif ac_mode == "dehumid":
        lines.append(f"  💡 除湿模式")
    elif ac_mode == "fan":
        lines.append(f"  🍃 风扇够用")
    elif ac_mode == "fan_locked":
        lines.append(f"  ⏳ 风扇锁定（关后{MIN_OFF}分钟内不重开）")
    else:
        lines.append(f"  ❌ 不开")
    
    if power:
        lines.append(f"  🔌 空调运行中（实测 {power}W）")
    if ctrl and ctrl.get("status") == "action":
        lines.append(f"  🎛️ 已自动执行: {ctrl['action']}")
    lines.append("")
    
    # 开窗
    lines.append("🪟 开窗")
    if decision['window'] == "open":
        lines.append(f"  ✅ 开窗（{decision['window_reason']}）")
    else:
        lines.append(f"  ❌ 关窗（{decision['window_reason']}）")
    
    if decision['fan'] == "circulate":
        lines.append(f"  💨 风扇内循环")
    lines.append("")
    
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════
# ── 每日报告（合并 ac_advisor + vent_reminder） ──
# ══════════════════════════════════════════════════════════════
def daily_report():
    """统一每日报告"""
    # 获取数据
    wx = fetch_weather()
    if "error" in wx:
        return f"⚠️ 天气API失败: {wx['error']}\n🏠 上海闵行 · 家庭生活系统 v1.0"
    
    # 室内
    indoor_temp, indoor_hum = read_indoor()
    
    # 统一决策
    decision = unified_decision(wx, indoor_temp, indoor_hum)
    
    # 获取空调状态
    state = load_state()
    ac_state = state.get("ac", {})
    power = read_ac_power()
    ac_control_init()
    
    # 控制
    ctrl = None
    if decision['ac_mode'] in ("cooling", "dehumid", "fan", "off", "fan_locked"):
        ctrl = apply_and_commit(decision['ac_mode'], decision['ac_target'], state)
    
    # 输出
    out = format_output(decision, wx, power, state, ctrl)
    
    # ── 开窗最佳窗口 ──
    forecast_data = fetch_forecast(1)
    h = forecast_data["hourly"]
    today = date.today().isoformat()
    rows = build_rows(h, today)
    
    if rows:
        aqi = fetch_aqi(1)
        best, blocked = pick_best(rows, indoor_temp, indoor_hum, decision['ac_mode'], aqi)
        if best:
            vv, emoji = verdict(best["rh"])
            dt = (best["temp"] - indoor_temp) if indoor_temp is not None else 0
            dur = t95(best["wind_kmh"], dt)
            dur_s = f"约 {dur:.0f} 分钟" if dur <= 90 else "风小，配风扇 ~10-15 分钟"
            lines = ["", "🪟 今日最佳开窗窗口"]
            lines.append(f"  🏆 {best['hr']:02d}:00  RH{best['rh']}% {best['temp']}°C 风{best['wind_kmh']:.0f}km/h → {emoji}{vv}")
            _wd = wind_dir_cn(best.get("wind_dir"))
            if _wd:
                lines.append(f"     🧭 {_wd} → 迎风1-2扇+背风2扇")
            lines.append(f"     ⏱ 建议时长: {dur_s}")
            if indoor_temp is not None:
                lines.append(f"     📍 室内实测: {indoor_temp}°C / {indoor_hum:.0f}%")
            for x in vent_advice(indoor_hum, best["rh"], indoor_temp, best["temp"]):
                lines.append(f"     {x}")
            out += "\n" + "\n".join(lines)
    
    # ── 24h 最优调度 ──
    learned = load_learned()
    schedule = compute_optimal_schedule(wx, indoor_temp, indoor_hum, learned)
    if schedule:
        slines = ["", "📊 未来24h最优计划"]
        total_cost = 0
        cool_hours = []
        dehumid_hours = []
        for hour, action, est_cost in schedule:
            total_cost += est_cost
            if action == "cool":
                cool_hours.append(hour)
            elif action == "dehumid":
                dehumid_hours.append(hour)
        slines.append(f"   制冷时段: {', '.join(f'{h}时' for h in cool_hours[:8])}{'...' if len(cool_hours) > 8 else ''}")
        slines.append(f"   除湿时段: {', '.join(f'{h}时' for h in dehumid_hours[:8])}{'...' if len(dehumid_hours) > 8 else ''}")
        slines.append(f"   预估总电费: {total_cost:.2f}元")
        out += "\n" + "\n".join(slines)
    
    # ── 热质量 ──
    thermal_data = load_thermal_data()
    thermal_model = thermal_data.get("thermal_model", {})
    if thermal_model:
        cool_rate = thermal_model.get("cooling_rate_per_min", 0.05)
        warm_rate = thermal_model.get("warmup_rate_per_min", 0.02)
        out += f"\n\n🏢 房间热惯性：降温{cool_rate:.3f}°C/min，升温{warm_rate:.3f}°C/min"
    
    # ── 滤网提醒 ──
    reminder = filter_clean_reminder()
    if reminder:
        out += "\n" + reminder
    
    # ── 夜间方案 ──
    if indoor_temp is not None and indoor_hum is not None:
        for nl in night_cost_lines(indoor_temp, indoor_hum):
            out += "\n" + nl
    
    return out


def alert_check():
    """智能模式: 未来90分钟内通过统一闸门的窗口才提醒"""
    now = datetime.now()
    if now.hour == 8 and now.minute < 30:
        return ""
    data = fetch_forecast(1)
    h = data["hourly"]
    today = date.today().isoformat()
    rows = build_rows(h, today)
    if not rows:
        return ""
    now_min = now.hour * 60 + now.minute
    upcoming = [r for r in rows if now_min <= r["hr"] * 60 <= now_min + 90]
    if not upcoming:
        return ""
    indoor_temp, indoor_hum = read_indoor()
    ac_mode = get_ac_mode()
    aqi = fetch_aqi(1)
    best, _ = pick_best(upcoming, indoor_temp, indoor_hum, ac_mode, aqi)
    if best is None:
        return ""
    vv, emoji = verdict(best["rh"])
    dt = (best["temp"] - indoor_temp) if indoor_temp is not None else 0
    dur = t95(best["wind_kmh"], dt)
    dur_s = f"约 {dur:.0f} 分钟" if dur <= 90 else "风小，配风扇 ~10-15 分钟"
    rain = "☔降雨" if best["pp"] >= 40 else "🌦" if best["pp"] >= 20 else "☀"
    lines = [f"⏰ 换气提醒: {now.strftime('%H:%M')}后, {best['hr']:02d}:00 是好窗口"]
    lines.append(f"   RH{best['rh']}% {rain}{best['pp']}%  温度{best['temp']}°C 风{best['wind_kmh']:.0f}km/h → {emoji}{vv}")
    _wd = wind_dir_cn(best.get("wind_dir"))
    if _wd:
        lines.append(f"   🧭 {_wd} → 迎风开1-2扇+背风2扇")
    lines.append(f"   ⏱ 4窗全开换 {dur_s}, 到点关")
    if indoor_temp is not None:
        lines.append(f"   📍 室内实测: {indoor_temp}°C / {indoor_hum:.0f}%")
    for x in vent_advice(indoor_hum, best["rh"], indoor_temp, best["temp"]):
        lines.append(f"   {x}")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════
# ── 错误通知 ──
# ══════════════════════════════════════════════════════════════
def notify_error_once(key, detail):
    """故障静默"""
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
    return f"⚠️ 系统数据异常（{detail[:100]}）→ 今日暂停，明早 08:00 恢复"


def notify_windows(title, text):
    """Windows toast 通知"""
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

# ══════════════════════════════════════════════════════════════
# ── 主入口 ──
# ══════════════════════════════════════════════════════════════
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    
    # 迁移旧状态
    migrate_legacy_state()
    
    out = None
    try:
        if "--alert" in sys.argv:
            out = alert_check()
        else:
            out = daily_report()
    except Exception as e:
        out = notify_error_once("generic", str(e))
    
    if out:
        notify_windows("家庭生活提醒", out.splitlines()[0] if out else "")
        print(out)
    print()
    print("─" * 40)
    print("数据: 和风天气 + Open-Meteo + 小米净化器4Lite · v1.0")


if __name__ == "__main__":
    main()
