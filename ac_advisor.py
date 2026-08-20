"""
定频空调省电顾问 v10.1 · 上海闵行（⚠️ 本文件已暂停，以 ac_watch.py 为准）
基于 v10.0 升级：露点判据、24h 最优调度、热质量学习

升级内容：
O. v10.1: 修正舒适指数公式（湿度权重 0.5→0.184，加房间面积系数）
    26°C/58% → HI≈28.9（刚好到阈值，不再激进）
    默认 70 平米系数 1.15（你家实际面积）
    湿度权重改为湿球温度近似，更准确
保留：v10.0 功能 · 露点判据 · 24h 最优调度 · 热质量学习
     v9.0 功能 · 舒适度指标 · 预测式预冷 · 自适应阈值学习 · 季节自适应 · 显式状态机
     v8.1 功能 · 室内传感器 · 天气获取 · 手动关锚点 · 滤网提醒 · 夜间方案对比 · TTS
"""
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from enum import Enum

# 确保能找到 miio（cron 可能用 python3.11，miio 装在 3.12）
_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]

for p in _MIIO_PATHS:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

# ── 阈值常量（统一头部，可配置） ─────────────
TEMP_COOLING = 27       # 体感≥27 → 制冷（2026-08-19：28→27，用户在 27.0°C 反馈热；公用一套阈值不分屋）
TEMP_DEHUMID_LOW = 26   # 除湿温度下限（对齐"不算热"）
TEMP_DEHUMID_HIGH = 28  # 除湿温度上限
HUM_DEHUMID_ON = 65     # 除湿开启湿度阈值（68% 就闷了，65% 更合理）
HUM_DEHUMID_OFF = 55    # 除湿关闭湿度阈值（滞回 10%，防频繁启停）
TEMP_ABSOLUTE_FLOOR = 24# 除湿温度绝对下限（OR 逃生门，低于此无条件关）
MIN_RUN = 40            # 开一次至少 40 分钟
MIN_OFF = 15            # 夜间关后至少 15 分钟再开
DAY_MIN_OFF = 10        # 白天关后至少 10 分钟再开
MAX_RUN = 180           # 连续运行超 180 分钟建议切换/关（防死锁）

# ── 空调功率（松川 KFRd-35GW 定频 1.5 匹） ──
AC_INPUT_W = 1076     # 输入功率 W（铭牌）
AC_COP = 3.25          # 能效比
ELECTRIC_PEAK = 0.617   # 上海居民峰电（6:00-22:00）元/度（一户一表第一档）
ELECTRIC_VALLEY = 0.307 # 上海居民谷电（22:00-6:00）元/度（约半价）
ELECTRIC_PRICE = ELECTRIC_PEAK  # 兼容旧引用（默认峰电）
DEHUMID_DUTY = 0.60    # 定频除湿模式占空比（压缩机跑跑停停, 估算）
COOL_DUTY = 0.70       # 定频制冷模式占空比（估算）
DEHUMID_MIN = 60       # 定频除湿达标通常更慢（风速低, 估算60分钟）
COOL_BURST_MIN = 40    # 制冷集中一轮达标（风速高, 约40分钟）
FILTER_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter_state.json")
FILTER_CLEAN_INTERVAL = 30  # 滤网建议清洗间隔（天）, 脏滤网=风量降=除湿慢=费电
# 铭牌实测: 制冷1076W/5.1A, 热泵1110W, PTC1000W, 最大2400W/11.3A, 2018-09产(老机除湿稍慢属正常)

# ── 显式状态机（v9.0 新增） ────────────────
class ACState(Enum):
    """空调运行状态枚举"""
    OFF = "off"
    COOLING = "cooling"
    COOLING_MAINTAIN = "cooling_maintain"
    DEHUMID = "dehumid"
    FAN = "fan"
    FAN_LOCKED = "fan_locked"

# 状态转移表：当前状态 → 允许的目标状态集合
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
    return current  # 非法转移 → 保持现状

# ── 综合舒适度指标（v10.1 修正） ─────────────
def comfort_index(temp, hum):
    """酷度指数 = T + 0.05×(RH-10)
    湿度权重 0.05：26°C/58% → 28.4（舒适边缘，不触发）
    30°C/70% → 32.0（该开）
    面积影响运行策略（MIN_RUN/阈值），不影响舒适判定"""
    if hum is None:
        return temp
    return temp + 0.05 * (hum - 10)

# ── 露点判据（v10.0 新增） ─────────────────
def dew_point(temp, hum):
    """Magnus formula: Td = (b·α(T,RH)) / (a - α(T,RH)) where α = (a·T)/(b+T) + ln(RH/100)
    Returns dew point in °C. Simplified Magnus: a=17.27, b=237.7"""
    if hum is None or hum <= 0:
        return None
    a, b = 17.27, 237.7
    alpha = (a * temp) / (b + temp) + math.log(hum / 100.0)
    td = (b * alpha) / (a - alpha)
    return td

def muggy_level(temp, hum):
    """0=comfort, 1=slight muggy, 2=muggy, 3=very muggy
    Based on dew point: <12 comfort, 12-16 slight, 16-18 muggy, >18 very muggy"""
    dp = dew_point(temp, hum)
    if dp is None:
        return 0
    if dp < 12: return 0
    elif dp < 16: return 1
    elif dp < 18: return 2
    else: return 3

# ── 季节自适应模式（v9.0 新增） ─────────────
def seasonal_adjustments():
    """根据月份自动切换：
    盛夏(7-8): 正常制冷
    梅雨(6): 除湿优先，温度阈值 +1°C
    春秋(4-5/9-10): 风扇优先，温度阈值 +2°C
    冬季(11-3): 关窗优先，不开空调
    返回 (temp_offset, hum_offset, strategy_label)
    """
    m = datetime.now().month
    if m in (7, 8):
        return 0, 0, "盛夏制冷"
    elif m == 6:
        return 1, -5, "梅雨除湿优先"  # 湿度阈值从 65 降到 60
    elif m in (4, 5, 9, 10):
        return 2, 5, "春秋风扇优先"  # 温度阈值从 28 到 30
    else:  # 11, 12, 1, 2, 3
        return 4, 0, "冬季关窗优先"  # 阈值 +4 基本不开

# ── 预测式预冷（v9.0 新增） ────────────────
def should_precool(wx, current_hi, threshold_hi):
    """如果未来 3 小时内 HI 超过阈值 +3°C，建议提前预冷"""
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    hums = hourly.get("relative_humidity_2m", [])
    if not times or not temps:
        return False, None, None
    now_h = datetime.now().hour
    # 找当前小时索引
    idx = None
    for i, t in enumerate(times):
        if len(t) >= 13 and t[11:13] == f"{now_h:02d}":
            idx = i
            break
    if idx is None:
        return False, None, None
    # 看未来 3h
    max_future_hi = current_hi
    for i in range(idx + 1, min(idx + 4, len(temps))):
        t = temps[i]
        h = hums[i] if i < len(hums) else None
        hi = comfort_index(t, h) if h is not None else t
        max_future_hi = max(max_future_hi, hi)
    if max_future_hi >= threshold_hi + 3:
        return True, max_future_hi, idx
    return False, None, None

# ── 24h 最优调度（v10.0 新增） ─────────────
def compute_optimal_schedule(wx, current_temp, current_hum, learned):
    """Compute optimal AC schedule for next 24h using:
    - QW (CMA) hourly temperature + humidity forecast
    - Time-of-use electricity price (peak/valley)
    - Learned thermal mass (from ac_thermal.json)
    - Returns list of (hour, action, est_cost)"""
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

        # Decision per hour
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
    """Find cheapest pre-cooling window before hot period
    Returns (start_hour, end_hour, estimated_savings)"""
    # Find first hot period (consecutive hours with 'cool')
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

    # Find cheapest valley/pre-cool window 2-3h before hot period
    pre_cool_start = max(0, hot_start - 3)
    pre_cool_end = max(0, hot_start - 1)

    # Calculate savings: pre-cool during valley vs cooling during peak
    valley_cost = ELECTRIC_VALLEY * kwh_est(40, COOL_DUTY)
    peak_cost = ELECTRIC_PEAK * kwh_est(40, COOL_DUTY)
    savings = peak_cost - valley_cost

    return (pre_cool_start, pre_cool_end, savings)

# ── 自适应阈值学习（v9.0 新增，持久化） ────
LEARN_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ac_learned.json")
# v8.23 回评时窗：决策后至少等 EVAL_DELAY_MIN 才有资格回评（此前是"刚做完就评"，
# 温度还没变化必然判失败）；超过 EVAL_STALE_MIN 则视为跨了别的事件，消费但不学习。
EVAL_DELAY_MIN = 30
EVAL_STALE_MIN = 120

def load_learned() -> dict:
    """读取学习结果，包含 adjusted_thresholds 和 decision_log"""
    default = {"adjusted_thresholds": {}, "decision_log": []}
    try:
        if os.path.exists(LEARN_FILE):
            with open(LEARN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_learned(learned: dict):
    """持久化学习结果"""
    tmp = LEARN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEARN_FILE)

def evaluate_and_learn(state, now_ts):
    """每次决策后回评：开了→温度降到位没？没开→有没有闷？
    成功→阈值不变；失败→阈值 ±1°C，写入 ac_learned.json"""
    learned = load_learned()
    log = learned.get("decision_log", [])
    # 只回评「已经过了观察窗口」的决策。
    # v8.23 修反向条件：原写法 `time < cutoff → continue` 实际是"丢弃超过 30 分钟的"，
    # 于是刚做完的决策立刻被回评——此时温度根本还没变化，temp_drop≈0 必然判失败、
    # 阈值 -1。这是偏移一路撞到 -2 下限的根因（单向棘轮只是让它无法恢复）。
    # 实测印证：ac_learned.json 两条日志时间戳相隔 13 秒却都已 evaluated=true。
    # 正确语义：决策时间早于 cutoff（已满观察期）才有资格回评。
    cutoff = (datetime.now() - timedelta(minutes=EVAL_DELAY_MIN)).isoformat()
    stale = (datetime.now() - timedelta(minutes=EVAL_STALE_MIN)).isoformat()
    adjusted = learned.get("adjusted_thresholds", {})
    for entry in log:
        ts = entry.get("time", "")
        if entry.get("evaluated"):
            continue
        if ts > cutoff:
            continue                     # 还没到观察期，留到下一轮
        if ts < stale:
            entry["evaluated"] = True    # 太久远（可能跨了别的事件），消费但不学习
            continue
        # 30 分钟前做了决策，现在回评
        pre_temp = entry.get("pre_temp")
        pre_hum = entry.get("pre_hum")
        action = entry.get("action")  # "cooling" | "dehumid" | "off" | "fan"
        # 获取当前室内条件
        cur_temp = state.get("last_temp")
        cur_hum = state.get("last_hum")
        if pre_temp is None or cur_temp is None:
            entry["evaluated"] = True
            continue
        success = True
        if action in ("cooling", "dehumid"):
            temp_drop = pre_temp - cur_temp
            hum_drop = (pre_hum or 0) - (cur_hum or 0)
            # v8.24 F1: 使用决策时的压缩机功率（而非当前功率）判断
            # 避免"30分钟前开机、当前已到温停机"的误判
            comp_running = (entry.get("power_at_decision") or 0) > 300
            if comp_running:
                success = True
            elif temp_drop < 0.3 and hum_drop < 3:
                success = False

        elif action in ("off", "fan"):
            temp_rise = cur_temp - pre_temp
            if temp_rise > 2.0 or (cur_hum is not None and cur_hum > 80):
                success = False
        # v8.23 修单向棘轮：原实现两个分支都只 -1、成功时不做任何事，于是无论
        # 评估结果如何阈值只降不升，必然漂到下限（实测已撞 -2 并停在那里）。
        # v11.1 只加了钳位止损，没修方向性。现在成功时向 0 收敛一步，
        # 让偏移量能回到中性，而不是永久停在边界上。
        cur_adj = adjusted.get("temp_cooling", 0)
        if not success:
            adjusted["temp_cooling"] = max(-2, min(2, cur_adj - 1))
        elif cur_adj < 0:
            adjusted["temp_cooling"] = cur_adj + 1      # 决策成功 → 向中性回收
        elif cur_adj > 0:
            adjusted["temp_cooling"] = cur_adj - 1
        entry["evaluated"] = True
    learned["adjusted_thresholds"] = adjusted
    learned["decision_log"] = log[-50:]  # 只保留最近 50 条
    save_learned(learned)

def log_decision(state, action, pre_temp, pre_hum, now_ts):
    """记录一次决策，供后续回评。同时记录决策时的压缩机功率。"""
    learned = load_learned()
    log = learned.get("decision_log", [])
    # v8.24 F1: 记录决策时的压缩机功率，供 evaluate_and_learn 使用
    # 避免用当前功率评估3分钟前的决策（时序错配导致误判）
    power_at_decision = state.get("_prev_power") or state.get("measured_w")
    log.append({
        "time": now_ts,
        "action": action,
        "pre_temp": pre_temp,
        "pre_hum": pre_hum,
        "evaluated": False,
        "power_at_decision": power_at_decision,
    })
    learned["decision_log"] = log[-50:]
    save_learned(learned)


# ── 热质量学习（v10.0 新增，持久化） ────────
THERMAL_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ac_thermal.json")

def load_thermal_data() -> dict:
    """读取热质量学习数据"""
    default = {"events": [], "thermal_model": {"cooling_rate_per_min": 0.05, "warmup_rate_per_min": 0.02, "time_constant_min": 120}}
    try:
        if os.path.exists(THERMAL_FILE):
            with open(THERMAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_thermal_data(data: dict):
    """持久化热质量学习数据"""
    tmp = THERMAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, THERMAL_FILE)

def _thermal_event_usable(e):
    """True when an event carries the numbers fit_thermal_model needs.
    Sensor reads return None when unreachable, so both a missing temperature
    and a missing duration make the event unusable for rate fitting."""
    if not isinstance(e, dict):
        return False
    for k in ("temp_before", "temp_after", "duration_min"):
        v = e.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
    return e["duration_min"] > 0


def record_thermal_event(event_type, temp_before, temp_after, duration_min, outdoor_temp):
    """Record a thermal event: cooling_cycle, warmup_cycle, or natural_drift
    Used to learn: cooling_rate (°C/min), thermal_mass (time constant)

    This module always passes a complete cycle, but home_living.py writes the
    same file with temp_after/duration_min left open until the next state
    change closes them. So keep every row here and let fit_thermal_model do the
    filtering - dropping open rows would destroy home_living's back-fill."""
    if temp_before is None:
        return False
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
    # Keep last 100 events
    data["events"] = events[-100:]
    # Recompute thermal model (filters unusable rows internally)
    data["thermal_model"] = fit_thermal_model(data["events"])
    save_thermal_data(data)
    return True

def fit_thermal_model(events):
    """Simple linear regression: cooling_rate = a * (temp_diff) + b * (outdoor_temp - indoor_temp)
    Returns {"cooling_rate_per_min": x, "warmup_rate_per_min": y, "time_constant_min": z}"""
    # Filter first: an existing ac_thermal.json may already hold events whose
    # temp_after / duration_min are None (written before the guard existed).
    # Fitting straight over those raised TypeError and took the whole run down.
    usable = [e for e in (events or []) if _thermal_event_usable(e)]
    cooling = [e for e in usable if e.get("type") == "cooling"]
    warming = [e for e in usable if e.get("type") == "warming"]

    model = {"cooling_rate_per_min": 0.05, "warmup_rate_per_min": 0.02, "time_constant_min": 120}

    if cooling:
        rates = [(e["temp_before"] - e["temp_after"]) / max(e["duration_min"], 1) for e in cooling[-20:]]
        # A cooling cycle that never cooled carries no usable rate; keep the
        # default rather than learning a zero/negative rate that would make
        # predict_cooling_time divide by ~0 and return absurd durations.
        rates = [r for r in rates if r > 0]
        if rates:
            model["cooling_rate_per_min"] = sum(rates) / len(rates)

    if warming:
        rates = [(e["temp_after"] - e["temp_before"]) / max(e["duration_min"], 1) for e in warming[-20:]]
        rates = [r for r in rates if r > 0]
        if rates:
            model["warmup_rate_per_min"] = sum(rates) / len(rates)

    return model

def predict_cooling_time(temp_current, temp_target, outdoor_temp, thermal_model):
    """Predict minutes needed to cool from temp_current to temp_target"""
    rate = thermal_model.get("cooling_rate_per_min", 0.05)
    diff = temp_current - temp_target
    if diff <= 0:
        return 0
    return int(diff / rate)

# ── 原有函数（保留） ────────────────────────
def dehumid_duty(temp, hum=None):
    """定频除湿占空比：温度基准(室温越低越频繁到温停机，24°C≈25%/26°C+≈60%) × 湿度修正(越潮压缩机越卖力：60%→0.7倍，85%→1.1倍，线性插值)"""
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
    """耗电估算(度): 输入功率 × 占空比 × 时长。有实测功率时用实测，否则回退铭牌。"""
    p = AC_MEASURED_W or AC_INPUT_W
    return p / 1000.0 * duty * (active_min / 60.0)


def current_price():
    """按当前时段取电价：22:00-6:00 谷电半价，其余峰电"""
    h = datetime.now().hour
    return ELECTRIC_VALLEY if h >= 22 or h < 6 else ELECTRIC_PEAK


def cost_est(kwh):
    """电费估算(元)：按当前时段电价（峰/谷）"""
    return kwh * current_price()

# ── 持久化状态文件 ────────────────────────
# 用 realpath：cron 侧 ~/.hermes/scripts/ac_advisor.py 是指向本文件的符号链接，
# abspath 会解析到链接所在目录，realpath 才指向真实目录（D:\work\ac-advisor）
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "ac_state.json")

# ── 天气 API（和风天气 CMA 数据源） ────────
# 预报（温湿度/降水/风）走和风 CMA = 中国气象局官方数据；只有 PM2.5 用 Open-Meteo。
# fetch_weather() 输出 Open-Meteo 兼容格式，所以下游字段名是 temperature_2m 等，
# 但数据来自和风——改数据源时记得同时改用户可见落款。
LAT, LON = 31.11, 121.38


def _load_env():
    """读取同目录 .env（git 已忽略），key 不硬编码在代码里"""
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


def _qw_get(endpoint: str) -> dict:
    """调用和风天气 API v2，自动解 gzip，返回 JSON。

    缺 key 时抛异常而非静默返回空：fetch_weather 会捕获成 {"error": ...}，
    调用方（main / decide 链）已有降级分支，空调控制不会因此中断。"""
    import gzip
    if not QW_KEY:
        raise RuntimeError("QW_API_KEY 未配置（应放在同目录 .env）")
    url = f"https://{QW_HOST}/weather/v1/{endpoint}/{LAT}/{LON}"
    req = urllib.request.Request(url, headers={"X-QW-Api-Key": QW_KEY, "Accept-Encoding": "identity"})
    resp = urllib.request.urlopen(req, timeout=15)
    body = resp.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))

# ── 室内传感器（小米空气净化器 4 Lite） ────
CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")

WEATHER_MAP = {
    0: "☀️ 晴", 1:"🌤 少云", 2:"⛅ 多云", 3:"☁️ 阴",
    45:"🌫 雾",
    51:"🌦 毛毛雨",61:"🌧 小雨",63:"🌧 中雨",65:"🌧 大雨",
    71:"🌨 小雪",73:"🌨 中雪",75:"🌨 大雪",
    80:"🌦 阵雨",81:"🌦 小阵雨",82:"🌦 大阵雨",95:"⛈ 雷暴",
}
def weather_cn(code): return WEATHER_MAP.get(code, f"☁️ {code}")


def load_state() -> dict:
    """读取持久化状态，不存在时返回默认"""
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
    """写入持久化状态（原子写：临时文件 → rename）"""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def minutes_since(ts_str: str | None) -> float | None:
    """计算自某个 ISO 时间戳以来的分钟数"""
    if not ts_str:
        return None
    try:
        then = datetime.fromisoformat(ts_str)
        now = datetime.now(tz=then.tzinfo if then.tzinfo else None)
        return (now - then).total_seconds() / 60.0
    except Exception:
        return None


def fetch_weather() -> dict:
    """获取天气数据（和风天气 CMA 数据源），返回 Open-Meteo 兼容格式"""
    try:
        CST = timezone(timedelta(hours=8))
        cur = _qw_get("current")  # 返回的已是扁平结构，无内层 current key
        dai = _qw_get("daily")["days"][0]
        hrs = _qw_get("hourly")["hours"]

        # 和风天气 code → WMO 近似值
        QW2WMO = {100:0, 101:2, 102:2, 103:3, 104:3,
                  200:0, 201:0, 202:0, 203:0,
                  300:0, 301:1, 302:2, 303:95, 304:95,  # 303=雷阵雨
                  400:0, 401:0, 402:0, 403:0,
                  500:45, 501:45, 502:45, 503:45, 504:45,
                  507:45, 508:45, 509:45,
                  510:51, 511:51, 512:51, 513:51, 514:51,
                  600:61, 601:61, 602:63, 603:65,
                  305:61, 306:63, 307:65,  # 305=小雨
                  610:80, 611:80, 612:80, 613:80,
                  700:45, 701:45, 702:45, 703:45, 704:45,
                  800:95, 801:95, 802:95, 803:95, 804:95}
        wmo = QW2WMO.get(int(cur.get("condition",{}).get("code", 0)), 0)

        # 取白天降雨概率（白天更活跃）
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


def read_indoor(timeout=3.0):
    """读取小米空气净化器 4 Lite 的室内温湿度。
    返回 (温度, 湿度) 或 (None, None)。
    session 死锁时自动重连（ack timeout 通常为 session token 过期，
    重建设备对象可恢复，无需等 3 分钟冷启动）。
    """
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

    # 第一次尝试：短超时快速读
    temp, hum = _read_indoor_once(ip, token, timeout=timeout)
    if temp is not None and hum is not None:
        return temp, hum

    # 失败：ack timeout 多为 session 死锁，重建 Device 对象重试一次
    # （不用延长 timeout，旧 session 再等也是死）
    temp, hum = _read_indoor_once(ip, token, timeout=5)
    if temp is not None and hum is not None:
        return temp, hum

    return None, None


def _read_indoor_once(ip, token, timeout):
    """单次读取室内温湿度，失败返回 (None, None)。"""
    try:
        from miio import Device
        d = Device(ip, token, timeout=timeout)
        # 4 Lite 是 MIoT 设备，用 get_properties
        # 温度 siid=3 piid=7，湿度 siid=3 piid=1（已验证与屏幕一致）
        r = d.send("get_properties", [
            {"siid": 3, "piid": 7},   # 温度
            {"siid": 3, "piid": 1},   # 湿度（不是 siid=4 piid=1!）
        ])
        if isinstance(r, list) and len(r) >= 2:
            temp = r[0].get("value") if isinstance(r[0], dict) else None
            hum = r[1].get("value") if isinstance(r[1], dict) else None
            if temp is not None and hum is not None:
                return round(temp, 1), round(hum, 0)
        # 备用：尝试 raw send
        r2 = d.send("get_prop", ["temp_dec", "humidity"])
        if isinstance(r2, list) and len(r2) >= 2:
            return round(r2[0] / 10, 1), round(r2[1], 0)
    except Exception:
        pass
    return None, None




AC_MEASURED_W = None     # 空调插座(空调伴侣 mcn02)实测功率，v7.0 引入
AC_SOCKET = None         # 插座实测开关状态: "on" | "off" | None(未知)
AC_CTRL = None           # 自动控制句柄，v8.0 引入


def ac_control_init():
    """初始化自动控制句柄。miio_config.json 的 ac_control=false 可关。"""
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
    """把决策执行到空调插座（红外 set_power/set_mode/set_tar_temp）。
    返回 {"status": "action"|"no_action"|"failed", "action": str, "reason": str}。
    status=failed 表示控制失败或无法验证——绝不能当作"无需动作"。"""
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
            pass  # 定频 dry 模式温度设定多数无效，跳过（T2）
        else:
            try:
                if target_temp and st.target_temperature != target_temp:
                    AC_CTRL.send_command("set_tar_temp", [target_temp])
                    act.append(f"设定{target_temp}°C")
            except Exception:
                pass
    elif new_mode == "fan_locked":
        # 想开但关后 <MIN_OFF 锁定 → 保持现状不动作（关着保持关；手动开着的绝不碰，用户意图优先）
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
    """以插座实测为权威对账持久化状态（P2）。
    插座可达时真实设备状态优先：state 说运行但插座关 → 回正为 off；
    state 说关但插座开 → 记 manual_on_at 并标记运行（模式未知按 cooling 计）。
    插座不可达（AC_SOCKET=None）时跳过，回退持久化状态。

    只有"曾被实测确认在运行、之后被关掉"才算用户手动关（写 manual_off_at）。
    两类情况明确排除，否则会造出假的用户意图并压制自动启动 30 分钟：
      1. 系统自己关的（_system_off_at 标记，180s 内）
      2. **系统下过开机指令但从未生效**（run_start 为空 = 没有任何一次实测确认过
         运行）。2026-08-19 实测：换屋后空调没接在该伴侣这一路，伴侣恒报 off，
         于是每轮对账都写一次假 manual_off_at → 日志刷"手动关后N分钟，跳过自动
         启动（尊重用户意图）"，而用户根本没碰过空调，且当时室内 27°C 正热。
    """
    if AC_SOCKET == "off" and state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
        # 排除系统自己关的 → 不设 manual_off_at，只回正
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
        # 排除"开机指令从未生效"：run_start 为空说明这个 cooling 只是系统意图，
        # 压缩机/插座从来没被确认过在运行，谈不上"用户把它关了"。
        never_ran = not state.get("run_start")
        if not is_system_off and not never_ran:
            state["manual_off_at"] = now_ts
        state["mode"] = "off"
        state["last_off_at"] = now_ts
        state["run_start"] = None
        # 清理 _system_off_at（已消费或过期，不再需要）
        state.pop("_system_off_at", None)
        return  # 处理后立即返回，不走下面

    # 清理过期 _system_off_at（非 off 状态时已无用，不影响 elif 连接）
    if state.get("_system_off_at"):
        state.pop("_system_off_at", None)
    if AC_SOCKET == "on" and state.get("mode") not in ("cooling", "dehumid", "dehumid_alert"):
        state["manual_on_at"] = now_ts
        state["mode"] = "cooling"
        state["run_start"] = now_ts
        state["_fake_run_count"] = 0
        _learn_from_manual(state, now_ts)  # 用户习惯学习


def verify_socket():
    """执行后回读插座真实开关状态（command→verify）。返回 "on"|"off"|None(不可达)。"""
    if AC_CTRL is None:
        return None
    try:
        s = AC_CTRL.status()
        return "on" if s.is_on else "off"
    except Exception:
        return None


def apply_state_from_verify(state, new_mode, real, now_ts):
    """按插座实测结果更新运行/停止锚点。仅 apply_and_commit 调用。
    返回 None=状态一致；True=verify 与意图矛盾（调用方标 failed）。"""
    was_on = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    if real == "on":
        if new_mode in ("cooling", "dehumid", "dehumid_alert"):
            if not was_on:
                state["run_start"] = now_ts
                state["last_on_at"] = now_ts
            state["mode"] = new_mode
            return None
        # 想关/风扇但实测在制冷 → 按真实状态修正
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
    # 想开制冷/除湿但实测已关 → 按真实状态修正
    state["mode"] = "off"
    state["run_start"] = None
    return True


def apply_and_commit(new_mode, target_temp, state, now_ts=None, meta=None):
    """唯一执行接口（P2 状态所有权）：ac_apply → verify → 按真实结果修改 state → commit。
    advice(cron) 与 ac_watch(v8.2.1) 共用；mode/run_start/last_on_at/last_off_at 仅由此函数写入，
    调用方禁止预先修改——控制失败时绝不把"意图态"落盘。

    meta(dict, optional): 执行成功时合并到 state 再落盘的额外字段。
    用于控制方在不破坏 P2 所有权的前提下附加元数据（如 last_dehumid_adjust_at）。
    控制失败/验证失败时不写入 meta，与"意图态不落盘"原则一致。"""
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
    # meta 只在执行成功时合并（控制失败/验证矛盾都不写入，与 P2 一致）
    if meta and not contradict:
        for k, v in meta.items():
            state[k] = v
    # 持久化 target_temp（执行成功时写入，off 模式不覆盖）
    if not contradict and target_temp is not None:
        state["target_temp"] = target_temp
    save_state(state)
    return ctrl


def read_ac_power(timeout=4.0):
    """读取空调插座实测功率(W)与开关状态。
    返回实测瓦数(开且读得到)，否则 None。全局 AC_SOCKET 记录 on/off/未知。
    空调插座 = 米家空调伴侣 lumi.acpartner.mcn02 @ 192.168.71.43，走局域网 miio。
    """
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

NIGHT_HOURS = 6          # 夜间整夜对比时长（小时）
DAYS_PER_MONTH = 30      # 月差价估算天数
# Bug fix: 删除 NIGHT_DUTY_26 / NIGHT_DUTY_24 死代码（原 501-502 行）

def _learn_from_manual(state, now_ts):
    """用户习惯学习：记录手动干预，学 3 次以上自动调整阈值。
    存储在 ac_state.json 的 user_pref 字段中。"""
    try:
        # 读取当前室内条件（从 rh_history 获取最近一条）
        rh_hist = state.get("rh_history", [])
        if not rh_hist:
            return
        last_rh = rh_hist[-1][1] if rh_hist else None
        if last_rh is None:
            return

        # 初始化 user_pref
        pref = state.get("user_pref", {})
        manual_log = pref.get("manual_on_log", [])

        # 记录本次手动干预
        manual_log.append({
            "ts": now_ts,
            "rh": last_rh,
            "mode": state.get("mode"),
        })

        # 只保留最近 20 条
        if len(manual_log) > 20:
            manual_log = manual_log[-20:]

        pref["manual_on_log"] = manual_log

        # 分析：如果 3+ 次手动开在 RH 60-65 之间，降低阈值
        low_rh_manual = [m for m in manual_log if 60 <= m.get("rh", 0) < 65]
        if len(low_rh_manual) >= 3:
            pref["hum_threshold"] = 60  # 学到用户偏好更低湿度
        else:
            pref.pop("hum_threshold", None)  # 恢复默认 65

        state["user_pref"] = pref
    except Exception:
        pass  # 学习失败不影响主逻辑


def night_cost_lines(indoor_temp, indoor_hum):
    """夜间方案对比（睡前 20:00~次日 6:00 谷电窗口展示）。
    0️⃣ 压一轮即关（用户现行打法，基准参照）vs 1️⃣~3️⃣ 整夜方案。
    定频耗电 = 输入功率 × 占空比 × 时长；谷电 0.307 元/度。
    """
    h = datetime.now().hour
    if not (h >= 20 or h < 6):
        return []
    p = ELECTRIC_VALLEY
    kb = kwh_est(COOL_BURST_MIN, COOL_DUTY)   # 压轮 24°C 40~60min（对齐文档实测案例 0.5度≈0.15元）
    # Bug fix: 传入 indoor_hum 参数（原只传 temp）
    dd = dehumid_duty(indoor_temp if indoor_temp is not None else 26.5, indoor_hum)
    # 使用动态占空比估算（替代已删除的死代码 NIGHT_DUTY_26/24）
    duty_26 = dehumid_duty(26, indoor_hum) if indoor_hum else 0.45
    duty_24 = dehumid_duty(24, indoor_hum) if indoor_hum else 0.55
    k26 = kwh_est(NIGHT_HOURS * 60, duty_26)
    k24 = kwh_est(NIGHT_HOURS * 60, duty_24)
    kd = kwh_est(NIGHT_HOURS * 60, dd)
    lines = [f"🌙 夜间方案对比（谷电 {p:.3f} 元/度）:"]
    lines.append(f"   0️⃣ 压一轮24°C×40~60min: {kb:.2f}度 ≈ {kb * p:.2f}元 ← 最省（能顶到天亮就收工）")
    lines.append(f"   1️⃣ 睡眠+制冷26°C整夜:  {k26:.2f}度 ≈ {k26 * p:.2f}元（怕热醒选它，后半夜基本停）")
    lines.append(f"   2️⃣ 睡眠+制冷24°C整夜:  {k24:.2f}度 ≈ {k24 * p:.2f}元（贵 {(k24 - k26) * p * DAYS_PER_MONTH:.1f}元/月，除湿最快）")
    lines.append(f"   3️⃣ 除湿模式整夜:        {kd:.2f}度 ≈ {kd * p:.2f}元（慢且最贵；睡眠升降温对除湿无效）")
    if indoor_hum is not None and indoor_hum > 70:
        lines.append("   💡 湿度偏高：先压轮24°C到60%再睡；后半夜闷醒就切睡眠26°C兜底")
    else:
        lines.append("   💡 湿度不高：压轮收工最省；怕热醒就睡眠26°C整夜")
    return lines


def filter_clean_reminder():
    """滤网清洗提醒(v2.5): 脏滤网=风量降=除湿慢=费电, 2018老机更敏感。"""
    try:
        with open(FILTER_STATE_FILE, encoding="utf-8") as f:
            last = json.load(f).get("last_clean")
        if not last:
            return "  💡 记得每 15~30 天洗一次滤网（脏滤网=除湿慢+费电）"
        days = (datetime.now() - datetime.fromisoformat(last)).days
        if days > FILTER_CLEAN_INTERVAL:
            return f"  ⚠️ 该洗滤网了（距上次清洗 {days} 天）——滤网脏直接拖累除湿速度"
        return None
    except Exception:
        return None


if __name__ == "__main__":
    print("⚠️ ac_advisor.py 已被合并为纯库，不再独立运行。")
    print("   空调控制 → ac_watch.py（每2分钟自动闭环）")
    print("   家庭生活 → home_living.py（换气/通风/提醒）")
    print("   本文件仅作为 ac_watch.py 和 home_living.py 的共享函数库被导入。")
    sys.exit(0)

    # ── (以下为共享函数库，供 ac_watch.py 和 home_living.py import) ──
