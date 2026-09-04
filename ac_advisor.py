#!/usr/bin/env python3
"""
定频空调省电顾问 v11.0 · 上海闵行
RC 热模型 + DP 最优调度 + 自学习闭环 + TTS 语音
"""

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from enum import Enum

_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]
for p in _MIIO_PATHS:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

TEMP_COOLING = 27
TEMP_DEHUMID_LOW = 26
HUM_DEHUMID_ON = 65
TEMP_ABSOLUTE_FLOOR = 24
MIN_RUN = 40
MIN_OFF = 15
DAY_MIN_OFF = 10
AC_INPUT_W = 1076
ELECTRIC_PEAK = 0.617
ELECTRIC_VALLEY = 0.307
DEHUMID_DUTY = 0.60
COOL_DUTY = 0.70
# v8.45 fix (审计 2026-09-02): v10.0 引入的模块常量，v8.29 重写时误删定义但
# night_cost_lines() 内仍引用 → 该函数一被调用就 NameError（潜伏 6 个版本）。
# 恢复 v10.0 原值 40（对齐「压轮 24°C 40~60min」早报文案）。
COOL_BURST_MIN = 40


class ACState(Enum):
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


def transition(current, target):
    return target if target in TRANSITIONS.get(current, set()) else current


def comfort_index(temp, hum):
    return temp if hum is None else temp + 0.05 * (hum - 10)


def dew_point(temp, hum):
    if hum is None or hum <= 0:
        return None
    a, b = 17.27, 237.7
    alpha = (a * temp) / (b + temp) + math.log(hum / 100.0)
    return (b * alpha) / (a - alpha)


def muggy_level(temp, hum):
    dp = dew_point(temp, hum)
    if dp is None:
        return 0
    if dp < 12:
        return 0
    elif dp < 16:
        return 1
    elif dp < 18:
        return 2
    else:
        return 3


def seasonal_adjustments():
    m = datetime.now().month
    if m in (7, 8):
        return 0, 0, "盛夏制冷"
    elif m == 6:
        return 1, -5, "梅雨除湿优先"
    elif m in (4, 5, 9, 10):
        return 2, 5, "春秋风扇优先"
    else:
        return 4, 0, "冬季关窗优先"


# ── 24h 最优调度（v11.0 DP） ──
def compute_optimal_schedule(
    wx, current_temp, current_hum, learned, comfort_weight=1.0, comfort_target=26.0
):
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    hums = hourly.get("relative_humidity_2m", [])
    if not times:
        return []

    CST = timezone(timedelta(hours=8))
    local_times = []
    for t in times:
        try:
            t_utc = datetime.fromisoformat(t)
            t_local = t_utc.astimezone(CST)
            local_times.append(t_local.strftime("%Y-%m-%dT%H:%M"))
        except:
            local_times.append(t)

    thermal_data = load_thermal_data()
    rc = thermal_data.get("thermal_model", {})
    a = rc.get("thermal_conductance", 0.003)
    c = rc.get("baseline_cooling", -0.035)
    T_MIN, T_MAX, T_STEP = 22.0, 32.0, 0.5
    n_temps = int((T_MAX - T_MIN) / T_STEP) + 1

    def temp_to_idx(t):
        return min(n_temps - 1, max(0, round((t - T_MIN) / T_STEP)))

    def idx_to_temp(i):
        return T_MIN + i * T_STEP

    def next_temp(t_in, t_out, action, dt_min=60):
        t = t_in
        for _ in range(dt_min):
            dt = a * (t_out - t) + c if action == "cool" else a * (t_out - t)
            t += dt
        return t

    def hour_cost(hour_idx, t_in, action):
        # v8.36 fix (hy4审计#9): 形参原名 hour 有误导——两个调用点（下方 DP 递推的
        # `for h in range(23,-1,-1)` 与正向生成的 `for h in range(min(24,len(...)))`）
        # 传的都是 local_times 的**数组索引**（相对当前小时的偏移 0..23），不是绝对
        # 小时。原代码直接拿索引跟 22/6 比较判峰谷 → 把索引 0-5 与 22-23 恒判为谷电，
        # DP 的 V[] 递推与 policy 全部建立在错误电价上，"谷电蓄冷"算出的时机不可信。
        # 主流程触发时机用的是真实小时（ac_watch.py: is_valley = _h>=22 or _h<6），
        # 所以线上未炸，但 DP 本身是错的。改为从 local_times 取真实小时。
        hour = hour_idx
        if isinstance(hour_idx, int) and 0 <= hour_idx < len(local_times):
            try:
                hour = int(local_times[hour_idx][11:13])
            except Exception:
                pass
        price = ELECTRIC_VALLEY if hour >= 22 or hour < 6 else ELECTRIC_PEAK
        elec_cost = kwh_est(60, COOL_DUTY) * price if action == "cool" else 0
        comfort_penalty = comfort_weight * max(0, t_in - comfort_target) ** 2
        return elec_cost + comfort_penalty

    V = [[float("inf")] * n_temps for _ in range(25)]
    policy = [["off"] * n_temps for _ in range(24)]
    for ti in range(n_temps):
        V[24][ti] = 0

    for h in range(23, -1, -1):
        t_out = temps[h] if h < len(temps) else 28.0
        for ti in range(n_temps):
            t_in = idx_to_temp(ti)
            best_cost, best_action = float("inf"), "off"
            for action in ["off", "cool"]:
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
            hot_start = h
            break
    if hot_start is None:
        return None
    all_valley = [22, 23, 0, 1, 2, 3, 4, 5]
    hours_to_hot = []
    for v in all_valley:
        hours_ago = (hot_start + 24 - v) if v >= 22 else (hot_start - v)
        if 1 <= hours_ago <= 16:
            hours_to_hot.append(v)
    if not hours_to_hot:
        return None
    pc_start, pc_end = hours_to_hot[0], hours_to_hot[-1]
    n_hours = len(hours_to_hot)
    valley_cost = ELECTRIC_VALLEY * kwh_est(40, COOL_DUTY) * n_hours
    peak_cost = ELECTRIC_PEAK * kwh_est(40, COOL_DUTY) * n_hours
    return (pc_start, pc_end, peak_cost - valley_cost)


def predict_dehumidify_need(wx, current_hum, current_temp):
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    hums = hourly.get("relative_humidity_2m", [])
    if not times or not hums:
        return False, None, None
    future_rh = []
    for i, t in enumerate(times):
        try:
            t_dt = datetime.fromisoformat(t)
            hours_ahead = (t_dt - datetime.now()).total_seconds() / 3600
            if 6 <= hours_ahead <= 30:
                future_rh.append(hums[i] if i < len(hums) else None)
        except:
            continue
    if not future_rh:
        return False, None, None
    max_future_rh = max(r for r in future_rh if r is not None)
    avg_future_rh = sum(r for r in future_rh if r is not None) / len(
        [r for r in future_rh if r is not None]
    )
    if max_future_rh > 70 or avg_future_rh > 65:
        if current_hum and current_hum > 55:
            return (
                True,
                55,
                f"谷电预湿：明日最高RH{max_future_rh:.0f}%，当前{current_hum:.0f}%，预除湿至55%",
            )
    return False, None, None


# ── 自适应阈值学习 ──
LEARN_FILE = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "ac_learned.json"
)
EVAL_DELAY_MIN = 30
EVAL_STALE_MIN = 120


def load_learned():
    default = {"adjusted_thresholds": {}, "decision_log": []}
    try:
        if os.path.exists(LEARN_FILE):
            with open(LEARN_FILE, encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default


def save_learned(learned):
    tmp = LEARN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEARN_FILE)


def evaluate_and_learn(state, now_ts):
    learned = load_learned()
    log = learned.get("decision_log", [])
    cutoff = (datetime.now() - timedelta(minutes=EVAL_DELAY_MIN)).isoformat()
    stale = (datetime.now() - timedelta(minutes=EVAL_STALE_MIN)).isoformat()
    adjusted = learned.get("adjusted_thresholds", {})
    for entry in log:
        ts = entry.get("time", "")
        if entry.get("evaluated"):
            continue
        if ts > cutoff:
            continue
        if ts < stale:
            entry["evaluated"] = True
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
            comp_running = (entry.get("power_at_decision") or 0) > 300
            if comp_running:
                success = True
            elif (pre_temp - cur_temp) < 0.3 and ((pre_hum or 0) - (cur_hum or 0)) < 3:
                success = False
        elif action in ("off", "fan"):
            # v8.29 audit fix: 旧标准 (cur_temp - pre_temp) > 2.0 把"关机后自然回热"
            # 误判为决策失败 → 偏移被无端扣到-2(启动线25°C, 过早开机费电)。
            # 关机决策的正确目标 = 不该关的时候关了(关后很快过冷/湿度爆升)。
            # 回热本身是物理规律, 不是错误。改为: 只有"关机后30分钟内温度不升反降
            # (说明关早了, 房间还在降温)"或湿度爆升才算失败。
            if (cur_temp is not None and cur_temp < pre_temp - 0.5) or (
                cur_hum is not None and cur_hum > 80
            ):
                success = False
        cur_adj = adjusted.get("temp_cooling", 0)
        # v8.30: 负偏移会把启动线压进抖振死区（启动线<关机线+迟滞），白天已由
        # ac_watch.DAY_START_LINE_FLOOR 兜底，这里不再产出负偏移。
        #
        # v8.36 fix (hy4审计#2): 原写法两条分支在 cur_adj∈[0,3] 上数学等价——
        # 失败 `max(0, min(2, a-1))` 与成功 `a-1` 在 a=0/1/2/3 时结果完全相同，
        # 决策质量回评信号彻底失效（成功与失败对偏移的影响无差别）；且成功分支
        # 缺 max(0,·) 夹紧，cur_adj=0.5 时会产出 **负偏移 -0.5**，与上面这段
        # v8.30 注释「这里不再产出负偏移」直接冲突（0.5 是高频取值，预算每次 ±0.5）。
        #
        # 新语义（按动作类型分向，不再让 cooling 失败与 off 失败同向）：
        #   成功            → -1（保守回归默认，保留 v8.29 的防顶死收敛速度）
        #   off/fan 失败    → -1（关早了：关后温度反降或湿度爆升 → 更早开机保舒适）
        #   cooling/dehumid 失败 → **不动**（开了但温湿度都没改善，多半是硬件/功率
        #                          计量问题；此时调启动线无意义，若按"更早开"处理
        #                          只会白白多耗电，按"更晚开"处理则会单向累加顶到
        #                          +3（启动线 30°C）——正是 v8.29 这段注释要防的事故）
        # 三者在偏移上互不相同，回评信号恢复；且失败路径永不增大偏移，杜绝顶死。
        if not success:
            if action not in ("cooling", "dehumid"):
                adjusted["temp_cooling"] = round(max(0, cur_adj - 1), 2)
        elif cur_adj > 0:
            adjusted["temp_cooling"] = round(max(0, cur_adj - 1), 2)
        entry["evaluated"] = True
    # v8.29 fix: 日预算学习按"当日"用电判断，且偏移只能回落不能因超预算单向顶死。
    # 旧逻辑用累计 kWh 对比日预算 → 永远超支 → 偏移被持续 +0.5 顶到上限，
    # 启动线被推到 29°C，8/24 下午室温 30°C 都不开机。改为当日值+上限放宽到 +3，
    # 超预算最多把启动线推到 30°C（极端热天用户可手动干预）。
    daily_kwh = state.get("_daily_kwh", 0)
    daily_budget = 8.0
    _today_str = (
        now_ts[:10] if isinstance(now_ts, str) else datetime.now().strftime("%Y-%m-%d")
    )
    if state.get("_budget_prediction", {}).get("date") == _today_str:
        daily_budget = max(
            4.0, (state["_budget_prediction"].get("predicted_kwh") or 8.0) * 1.3
        )
    # v8.36 fix (hy4审计#3): v8.31 引入成本口径时注释写的是"预算按加权电价成本
    # **而非** kWh 判断"，但旧的 kWh 分支并未删除，两套独立 if/elif 叠加生效：
    # 每次 evaluate_and_learn 调用最多 +1.0，而 main() 一轮调用 evaluate 两次
    # （ac_watch.py 的 decide 前后各一次）→ 一轮最多 +2.0；护栏一次仅 -0.5，
    # 净增益 2:1 失配，实测 2 轮即顶到上限 +3（启动线 30°C），正是 v8.29 这段
    # 注释声称要防止的"30°C 不开机"事故。改为互斥：有峰谷分时数据走成本口径，
    # 无数据才回退 kWh 口径（这才是 v8.31 注释的原意）。
    _wh = state.get("_kwh_by_price_band") or {}
    _peak_kwh = _wh.get("peak", 0.0)
    _valley_kwh = _wh.get("valley", 0.0)
    _has_band = (_peak_kwh + _valley_kwh) > 0
    if not _has_band:
        # 回退口径：无峰谷分时数据时按原始 kWh 判断
        if daily_kwh > daily_budget and adjusted.get("temp_cooling", 0) < 3:
            adjusted["temp_cooling"] = min(3, adjusted.get("temp_cooling", 0) + 0.5)
        elif daily_kwh < daily_budget * 0.5 and adjusted.get("temp_cooling", 0) > 0:
            adjusted["temp_cooling"] = max(0, adjusted.get("temp_cooling", 0) - 0.5)
    # v8.31 峰谷套利：预算按"加权电价成本"而非 kWh 判断。
    # 谷电(22-6点, 0.307元)制冷多跑不罚；峰电(0.617元)超支才推高启动线。
    # 效果：同样8度电，谷电花的钱≈4度峰电，系统自然学会"往夜里搬负荷"。
    else:
        try:
            daily_cost = _peak_kwh * ELECTRIC_PEAK + _valley_kwh * ELECTRIC_VALLEY
            _cost_budget = max(
                2.0,
                (
                    state["_budget_prediction"].get("predicted_kwh", 8.0) * 1.3
                    if state.get("_budget_prediction", {}).get("date") == _today_str
                    else 8.0 * 1.3
                )
                * (ELECTRIC_PEAK + ELECTRIC_VALLEY)
                / 2,
            )
            if daily_cost > _cost_budget and adjusted.get("temp_cooling", 0) < 3:
                adjusted["temp_cooling"] = min(3, adjusted.get("temp_cooling", 0) + 0.5)
            elif (
                daily_cost < _cost_budget * 0.5 and adjusted.get("temp_cooling", 0) > 0
            ):
                adjusted["temp_cooling"] = max(0, adjusted.get("temp_cooling", 0) - 0.5)
        except Exception:
            pass
    # 偏移健康护栏：白天(8-21点)若室温≥启动线-0.5 且空调未运行超过20分钟，
    # 说明启动线过高，强制回落 0.5（自愈，防止再次出现 30°C 不开机）
    if adjusted.get("temp_cooling", 0) > 0 and 8 <= datetime.now().hour < 21:
        # v8.36 fix (hy4审计#5): 原条件 `last_temp >= TEMP_COOLING + adj - 0.5` 自指
        # ——触发门槛随偏移一起被抬高，形成死锁：adj=2 时启动线 29°C、护栏却要求
        # last_temp >= 28.5°C；adj=3 时启动线 30°C、要求 >= 29.5°C。启动线越高越
        # 难开机，室温越涨不到门槛，护栏越不触发 → 温和天气下永久顶死。
        # 改为对绝对启动线 TEMP_COOLING 判定，任何正偏移在高温下都能自愈。
        if (state.get("last_temp") or 0) >= TEMP_COOLING:
            _off_min = minutes_since(state.get("last_off_at"))
            if _off_min is not None and _off_min > 20:
                adjusted["temp_cooling"] = round(
                    max(0, adjusted.get("temp_cooling", 0) - 0.5), 2
                )
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
                        _h = (
                            int(hourly["time"][i][11:13])
                            if i < len(hourly.get("time", []))
                            else i
                        )
                        if t_out > 26:
                            _hours_cooling = min(1, (t_out - 26) / 6)
                            _kwh = kwh_est(60 * _hours_cooling, COOL_DUTY)
                            _total_kwh += _kwh
                    state["_budget_prediction"] = {
                        "date": _today,
                        "predicted_kwh": round(_total_kwh, 2),
                        "predicted_cost": round(_total_kwh * 0.5, 2),
                        "max_temp": max(temps) if temps else None,
                    }
        except:
            pass
    # 压缩机健康监控
    # v8.45 fix (审计 2026-09-02): 原实现读 state["_cycle_log"]，但该键全仓从未被
    # 写入（真实周期数据由 close_cycle 落盘到 cycle_log.jsonl）→ len>=5 恒假 →
    # _compressor_health 自引入以来从未产出过（write-only 死键）。改为从
    # cycle_log.jsonl 尾部取最近 5 条已完结周期，按 duration_min 均值判健康；
    # 文件缺失/损坏/不足 5 条时静默跳过（与原「样本不足不动 state」语义一致）。
    try:
        _cl_path = os.path.join(SCRIPT_DIR, "cycle_log.jsonl")
        if os.path.exists(_cl_path):
            with open(_cl_path, encoding="utf-8") as f:
                _tail = f.readlines()[-5:]
            _cycle_log = []
            for _ln in _tail:
                try:
                    _cycle_log.append(json.loads(_ln))
                except Exception:
                    pass
            if len(_cycle_log) >= 5:
                _recent = [c.get("duration_min", 0) or 0 for c in _cycle_log]
                _avg = sum(_recent) / len(_recent)
                if _avg < 15:
                    state["_compressor_health"] = "short_cycling"
                elif _avg > 40:
                    state["_compressor_health"] = "long_running"
                else:
                    state["_compressor_health"] = "normal"
    except Exception:
        pass
    learned["adjusted_thresholds"] = adjusted
    learned["decision_log"] = log[-50:]
    save_learned(learned)


def log_decision(
    state, action, pre_temp, pre_hum, now_ts, reason=None
):  # v8.39: 加 reason 参数，支持决策归因
    learned = load_learned()
    log = learned.get("decision_log", [])
    power_at_decision = state.get("_prev_power") or state.get("measured_w")
    log.append(
        {
            "time": now_ts,
            "action": action,
            "pre_temp": pre_temp,
            "pre_hum": pre_hum,
            "evaluated": False,
            "power_at_decision": power_at_decision,
            "reason": reason,
        }
    )  # v8.39: 决策原因，支持 grep 归因
    learned["decision_log"] = log[-50:]
    save_learned(learned)


# ── 热质量学习 ──
THERMAL_FILE = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "ac_thermal.json"
)


def load_thermal_data():
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
            with open(THERMAL_FILE, encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default


def save_thermal_data(data):
    tmp = THERMAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, THERMAL_FILE)


def _thermal_event_usable(e):
    if not isinstance(e, dict):
        return False
    for k in ("temp_before", "temp_after", "duration_min"):
        v = e.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
    return e["duration_min"] > 0


def record_thermal_event(
    event_type, temp_before, temp_after, duration_min, outdoor_temp
):
    if temp_before is None:
        return False
    data = load_thermal_data()
    events = data.get("events", [])
    events.append(
        {
            "type": event_type,
            "temp_before": temp_before,
            "temp_after": temp_after,
            "duration_min": duration_min,
            "outdoor_temp": outdoor_temp,
            "timestamp": datetime.now().isoformat(),
        }
    )
    data["events"] = events[-100:]
    _last_fit = data.get("_last_fit_ts")
    _new_count = data.get("_new_event_count", 0) + 1
    data["_new_event_count"] = _new_count
    _should_fit = _new_count >= 5
    if not _should_fit and _last_fit:
        try:
            if (
                datetime.now() - datetime.fromisoformat(_last_fit)
            ).total_seconds() > 86400:
                _should_fit = True
        except:
            _should_fit = True
    elif not _last_fit:
        _should_fit = True
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
    model = {
        "thermal_conductance": 0.003,
        "baseline_cooling": -0.035,
        "time_constant_min": 120,
    }
    if len(cooling) >= 3:
        X, y = [], []
        for e in cooling[-30:]:
            t_in = e["temp_before"]
            t_out = e["outdoor_temp"] or t_in
            rate = (e["temp_after"] - t_in) / e["duration_min"]
            X.append([t_out - t_in, 1.0])
            y.append(rate)
        if len(X) >= 3:
            import numpy as np

            coeffs, _, _, _ = np.linalg.lstsq(np.array(X), np.array(y), rcond=None)
            a, c = coeffs
            if -0.01 < a < 0.1 and -0.2 < c < 0.05:
                model["thermal_conductance"] = float(a)
                model["baseline_cooling"] = float(c)
                if abs(a) > 0.0001:
                    model["time_constant_min"] = round(1.0 / abs(a), 1)
    if len(warming) >= 3:
        rates = [
            (e["temp_after"] - e["temp_before"]) / max(e["duration_min"], 1)
            for e in warming[-20:]
        ]
        rates = [r for r in rates if r > 0]
        if rates:
            model["warmup_rate_per_min"] = sum(rates) / len(rates)
    # 兼容旧接口：cooling_rate_per_min = 平均制冷速率（负值=降温），default=0.05
    if len(cooling) >= 1:
        crates = [
            (e["temp_after"] - e["temp_before"]) / max(e["duration_min"], 1)
            for e in cooling[-30:]
        ]
        model["cooling_rate_per_min"] = sum(crates) / len(crates)
    else:
        model["cooling_rate_per_min"] = 0.05
    return model


def predict_cooling_time(temp_current, temp_target, outdoor_temp, thermal_model):
    a = thermal_model.get("thermal_conductance", 0.003)
    c = thermal_model.get("baseline_cooling", -0.035)
    if temp_current is None or temp_target is None:
        return 0
    diff = temp_current - temp_target
    if diff <= 0:
        return 0
    # v8.36 fix (hy4审计#4): 室外温度缺失（天气 API 失败）时降级为"忽略室外传热"，
    # 只保留基础制冷速率 c，而不是让 `a * (None - t)` 抛 TypeError 打断主循环。
    if outdoor_temp is None:
        outdoor_temp = temp_current
    t, minutes = temp_current, 0
    while t > temp_target and minutes < 600:
        t += a * (outdoor_temp - t) + c
        minutes += 1
    return minutes


def dehumid_duty(temp, hum=None):
    if temp is None or temp >= 26:
        base = DEHUMID_DUTY
    elif temp <= TEMP_ABSOLUTE_FLOOR:
        base = 0.25
    else:
        base = 0.25 + (DEHUMID_DUTY - 0.25) * (temp - TEMP_ABSOLUTE_FLOOR) / (
            26 - TEMP_ABSOLUTE_FLOOR
        )
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
    p = AC_MEASURED_W or AC_INPUT_W
    return p / 1000.0 * duty * (active_min / 60.0)


def current_price():
    h = datetime.now().hour
    return ELECTRIC_VALLEY if h >= 22 or h < 6 else ELECTRIC_PEAK


def cost_est(kwh):
    return kwh * current_price()


SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "ac_state.json")
LAT, LON = 31.11, 121.38


def _load_env():
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


def _qw_get(endpoint):
    import gzip

    if not QW_KEY:
        raise RuntimeError("QW_API_KEY 未配置")
    url = f"https://{QW_HOST}/weather/v1/{endpoint}/{LAT}/{LON}"
    req = urllib.request.Request(
        url, headers={"X-QW-Api-Key": QW_KEY, "Accept-Encoding": "identity"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    body = resp.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")

WEATHER_MAP = {
    0: "☀️ 晴",
    1: "🌤 少云",
    2: "⛅ 多云",
    3: "☁️ 阴",
    45: "🌫 雾",
    51: "🌦 毛毛雨",
    61: "🌧 小雨",
    63: "🌧 中雨",
    65: "🌧 大雨",
    71: "🌨 小雪",
    73: "🌨 中雪",
    75: "🌨 大雪",
    80: "🌦 阵雨",
    81: "🌦 小阵雨",
    82: "🌦 大阵雨",
    95: "⛈ 雷暴",
}


def weather_cn(code):
    return WEATHER_MAP.get(code, f"☁️ {code}")


def load_state():
    default = {"mode": None, "last_on_at": None, "last_off_at": None, "run_start": None}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, encoding="utf-8-sig") as f:
            return {**default, **json.load(f)}
    except Exception:
        default["_state_load_failed"] = True
        return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def minutes_since(ts_str):
    if not ts_str:
        return None
    try:
        then = datetime.fromisoformat(ts_str)
        now = datetime.now(tz=then.tzinfo if then.tzinfo else None)
        return (now - then).total_seconds() / 60.0
    except:
        return None


def fetch_weather():
    try:
        CST = timezone(timedelta(hours=8))
        cur = _qw_get("current")
        dai = _qw_get("daily")["days"][0]
        hrs = _qw_get("hourly")["hours"]
        QW2WMO = {
            100: 0,
            101: 2,
            102: 2,
            103: 3,
            104: 3,
            200: 0,
            201: 0,
            202: 0,
            203: 0,
            300: 0,
            301: 1,
            302: 2,
            303: 95,
            304: 95,
            400: 0,
            401: 0,
            402: 0,
            403: 0,
            500: 45,
            501: 45,
            502: 45,
            503: 45,
            504: 45,
            507: 45,
            508: 45,
            509: 45,
            510: 51,
            511: 51,
            512: 51,
            513: 51,
            514: 51,
            600: 61,
            601: 61,
            602: 63,
            603: 65,
            305: 61,
            306: 63,
            307: 65,
            610: 80,
            611: 80,
            612: 80,
            613: 80,
            700: 45,
            701: 45,
            702: 45,
            703: 45,
            704: 45,
            800: 95,
            801: 95,
            802: 95,
            803: 95,
            804: 95,
        }
        wmo = QW2WMO.get(int(cur.get("condition", {}).get("code", 0)), 0)
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
                    round(h["precipitation"]["probability"] * 100)
                    if isinstance(h.get("precipitation"), dict)
                    else 0
                    for h in hrs
                ],
            },
        }
    except Exception as e:
        return {"error": str(e)}


def read_indoor(timeout=3.0):
    if not os.path.exists(CONFIG_FILE):
        return None, None
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except:
        return None, None
    ip, token = cfg.get("ip"), cfg.get("token")
    if not ip or not token:
        return None, None
    temp, hum = _read_indoor_once(ip, token, timeout=timeout)
    if temp is not None:
        return temp, hum
    return _read_indoor_once(ip, token, 5)


def _read_indoor_once(ip, token, timeout):
    try:
        from miio import Device

        d = Device(ip, token, timeout=timeout)
        r = d.send("get_properties", [{"siid": 3, "piid": 7}, {"siid": 3, "piid": 1}])
        if isinstance(r, list) and len(r) >= 2:
            temp = r[0].get("value") if isinstance(r[0], dict) else None
            hum = r[1].get("value") if isinstance(r[1], dict) else None
            if temp is not None and hum is not None:
                return round(temp, 1), round(hum, 0)
    except:
        pass
    return None, None


AC_MEASURED_W = None
AC_SOCKET = None
AC_COMPANION_TARGET = None  # v8.42: 伴侣设定温度（回声字段，仅镜像我方发射命令）


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
    except:
        AC_CTRL = None


def ac_apply(new_mode, target_temp=None):
    if new_mode == "dehumid_alert":
        return {"status": "no_action", "action": "", "reason": "alert_only"}
    if AC_CTRL is None:
        return {"status": "failed", "action": "", "reason": "control_unavailable"}
    try:
        st = AC_CTRL.status()
    except Exception as e:
        return {"status": "failed", "action": "", "reason": f"status_read_failed: {e}"}
    on = st.is_on
    act = []
    if new_mode in ("cooling", "dehumid"):
        want_mode = "cool" if new_mode == "cooling" else "dry"
        if not on:
            try:
                AC_CTRL.send_command("set_power", ["on"])
                act.append("开机")
                on = True
            except Exception as e:
                return {
                    "status": "failed",
                    "action": "开机",
                    "reason": f"power_on_failed: {e}",
                }
        try:
            if st.mode is not None and st.mode.value != want_mode:
                AC_CTRL.send_command("set_mode", [want_mode])
                act.append(f"模式{want_mode}")
        # v8.36 fix (hy4审计#7): 原为裸 `except: pass`，设置失败被完全吞掉。
        # 此时 act 为空 → 返回 no_action → 上层的 apply_and_commit 无法区分
        # "本来就已是目标状态"与"设置失败"，日志显示"已处目标状态无需动作"。
        except Exception as e:
            return {
                "status": "failed",
                "action": "，".join(act) or "设定模式",
                "reason": f"set_mode_failed: {e}",
            }
        if want_mode == "dry":
            pass
        else:
            try:
                if target_temp and st.target_temperature != target_temp:
                    AC_CTRL.send_command("set_tar_temp", [target_temp])
                    act.append(f"设定{target_temp}°C")
            except Exception as e:
                return {
                    "status": "failed",
                    "action": "，".join(act) or "设定温度",
                    "reason": f"set_tar_temp_failed: {e}",
                }
    elif new_mode == "fan_locked":
        pass
    elif new_mode in ("fan", "off"):
        if on:
            try:
                AC_CTRL.send_command("set_power", ["off"])
                act.append("关机")
            except Exception as e:
                return {
                    "status": "failed",
                    "action": "关机",
                    "reason": f"power_off_failed: {e}",
                }
    return {
        "status": "action" if act else "no_action",
        "action": "，".join(act),
        "reason": "",
    }


def _anchor_oscillating(state, now_ts, window_min=35):
    """v8.43b: 锚点震荡检测（幻象周期识别）。v8.46: 窗口 20→35min。

    判据：35 分钟内存在①幻象门控标记 _phantom_gate_at（功率门控静默翻
    转时打）或②任一反向手动锚点 → 本次翻转判为幻象周期一环。
    背景：功率断表（load_power=None）窗口会回退 v8.36 原语义打锚点，而
    伴侣待机幻象翻转间隔实测 12-14 分钟（2026-09-01 全天 81+ 次）；2026-09-04
    审计再证：翻转间隔拉长到 16-24 分钟即可漏过 20min 窗口（今晨 9 次幻象
    「手动开」全部入账，comfort_weight 被再次推满 1.0）——35min 覆盖实测
    12-24 分钟幻象周期。真用户不会每十几分钟手动开关空调几小时。首个幻象
    翻转仍可能打锚点（不可区分），但周期后续翻转全部被静默。
    """
    try:
        now = datetime.fromisoformat(now_ts) if isinstance(now_ts, str) else now_ts
        for key in ("_phantom_gate_at", "manual_on_at", "manual_off_at"):
            t = state.get(key)
            if not t:
                continue
            dt = datetime.fromisoformat(t) if isinstance(t, str) else t
            if (now - dt).total_seconds() < window_min * 60:
                return True
        return False
    except Exception:
        return False


def reconcile_state(state, now_ts, load_power=None):
    """v8.43: 增加 load_power 门控参数（P0 修复：手动锚点震荡循环）。

    背景（2026-09-01 审计实证）：空调伴侣 is_on 是它对空调状态的 IR 信念，
    待机 1W 时随自身恒温循环/红外丢失自行翻转（当天 81+ 次）。reconcile
    无条件把 socket 翻转当用户手动操作 → 打手动锚点 + 喂 _learn_from_manual
    假样本 → comfort_weight 被推满 1.0、decide() 全天被锚点短路饥饿
    （545/591 条日志），学习回路零进账。

    门控原则：**夹钳功率 = 物理现实 > 伴侣 is_on = IR 信念**；
    功率不可测时回退 v8.36 原语义（保守不误伤真手动）。

    「关」翻转（socket=off 且 state 运行态）：
    - load_power >50W → 幻象关机：维持运行态不动，等下一轮一致再判；
    - 当前/上一 tick 均无压缩机级活动（≤50W/None）→ 信念漂移：静默对账
      mode=off（空调确实没在制冷），不打 manual_off_at、不喂学习
      （旧语义每次翻下压制自动启动 12 分钟 = 震荡主源）；
    - 上一 tick 有压缩机活动（prev>50W）且当前功率不可测 → 真手动关机：
      原语义打锚点+喂学习。
    「开」翻转（socket=on 且 state 非运行态）：
    - load_power ≤50W → 待机幻象/纯风扇级：**完全不动 state**（不翻 mode、
      不打锚点、不喂学习）——翻 mode=cooling 反而挡住 H2 接管（H2 要求
      state 非运行态）；真压缩机启动（>50W）后 H2 按双 tick 持久化正常接管；
    - v8.46: 打锚点前要求**压缩机级负载证据**——连续两个 tick >50W（≤7min
      间隔）或本 tick >300W；仅一次 50W<p≤300W 先观察一拍（打
      _on_flip_high_at，不动 state），下一 tick 复核；
    - 功率不可测（None）→ v8.36 原语义打锚点，但学习喂入**延迟 10 分钟**
      做 kWh 功耗验证（v8.46 修复③：幻象锚点零功耗，不入账）。
    - v8.48（09-04 晚审计 P0 三发）：①学习喂入统一延迟验证——功率铁证
      路径不再立即入账，与断表路径一律走 _pending_manual_on_learn
      （10min kWh ≥0.005 核验）；②震荡检测扩容到全部路径——伴侣
      load_power 与 is_on 同源（IR信念/瞬时读数），幻象翻转窗口能拿到
      高功率瞬时值穿透 v8.46「铁证」（实证：kWh今冻结仍打出4锚点+4次
      立即学习，comfort_weight 0.5→0.8）；③观察一拍不再打
      _phantom_gate_at（避免自设标记误拦双tick证据链）。
    """
    # ── v8.46 修复③: 延迟学习验证——断表窗口的手动开锚点 10 分钟后核对
    # kWh 增量：<0.005 = 幻象锚点（压缩机零出力）不入账；≥0.005 或电量
    # 不可测（None）时保守入账（保留旧行为，不误伤真手动）。
    _pml = state.get("_pending_manual_on_learn")
    if _pml:
        try:
            _pt = (
                datetime.fromisoformat(_pml["ts"])
                if isinstance(_pml["ts"], str)
                else _pml["ts"]
            )
            _now = (
                datetime.fromisoformat(now_ts)
                if isinstance(now_ts, str)
                else now_ts
            )
            _due = (_now - _pt).total_seconds() >= 600
        except Exception:
            _due = True
        if _due:
            state.pop("_pending_manual_on_learn", None)
            _k0 = _pml.get("kwh")
            _k1 = state.get("estimated_kwh")
            if _k0 is None or _k1 is None or (_k1 - _k0) >= 0.005:
                _learn_from_manual(state, _pml["ts"])
    # ── v8.43: 「关」翻转门控（功率为准，静默翻下不打锚点） ──
    if AC_SOCKET == "off" and state.get("mode") in (
        "cooling",
        "dehumid",
        "dehumid_alert",
    ):
        if load_power is not None and load_power > 50:
            # 幻象关机：伴侣信念说关但夹钳实测仍在压缩机级（>50W）。
            # 不翻 mode、不打锚点、不喂学习——维持运行态等下一轮再判。
            state["_phantom_gate_at"] = now_ts
            return
        prev = state.get("_prev_power")
        _activity = prev is not None and prev > 50
        if not _activity:
            # 静默翻下：当前无功率读数或 ≤50W，且上一 tick 也无压缩机活动
            # → 这次「关」没有打断任何真实运行，是伴侣信念漂移（红外丢失
            # 已知模式）。只对账 mode=off（物理现实），不打锚点不喂学习。
            state["mode"] = "off"
            state["last_off_at"] = now_ts
            state["run_start"] = None
            state.pop("_system_off_at", None)
            state["_phantom_gate_at"] = now_ts
            return
        # 上一 tick 有压缩机活动而当前功率不可测 → 按真手动关机处理（原语义）
        # v8.43b: 仅功率断表（None）时用震荡检测辅助判断；功率=1W 且上一 tick
        # 有活动是真实关机转变（物理证据充分），直接原语义。
        if load_power is None and _anchor_oscillating(state, now_ts):
            state["mode"] = "off"
            state["last_off_at"] = now_ts
            state["run_start"] = None
            state.pop("_system_off_at", None)
            state["_phantom_gate_at"] = now_ts
            return
        sys_off = state.get("_system_off_at")
        is_system_off = False
        if sys_off:
            try:
                sys_off_dt = (
                    datetime.fromisoformat(sys_off)
                    if isinstance(sys_off, str)
                    else sys_off
                )
                now_dt = (
                    datetime.fromisoformat(now_ts)
                    if isinstance(now_ts, str)
                    else now_ts
                )
                if (now_dt - sys_off_dt).total_seconds() < 180:
                    is_system_off = True
            except:
                pass
        never_ran = not state.get("run_start")
        if not is_system_off and not never_ran:
            state["manual_off_at"] = now_ts
        state["mode"] = "off"
        state["last_off_at"] = now_ts
        state["run_start"] = None
        state.pop("_system_off_at", None)
        # v8.36 fix (hy4审计#12): 用户手动关机也要喂给偏好学习。此前 _learn_from_manual
        # 只在下方"手动开机"分支被调用，且调用时 state["mode"] 已被置为 "cooling"，
        # 于是 manual_on_log 里 mode 恒为 cooling → manual_off_count 恒为 0 →
        # comfort_weight 只能单调下降、永不回升：用户嫌冷手动关机，系统永远学不会
        # 把舒适度权重调回去。这里在 mode 置 "off" 之后补调，使 off 样本得以入账。
        if not is_system_off and not never_ran:
            _learn_from_manual(state, now_ts)
        return
    if state.get("_system_off_at"):
        state.pop("_system_off_at", None)
    # ── v8.43→v8.46: 「开」翻转门控 ──
    if AC_SOCKET == "on" and state.get("mode") not in (
        "cooling",
        "dehumid",
        "dehumid_alert",
    ):
        if load_power is not None and load_power <= 50:
            # 待机幻象：伴侣信念说开但夹钳实测 ≤50W（纯待机 1W 恒温循环翻上）。
            # 完全不动 state：不翻 mode（翻了会挡 H2 接管）、不打锚点、不喂学习。
            # 真压缩机启动（>50W）后 H2 按双 tick 持久化+幻影防护正常接管。
            # v8.46: 低功率读数同时打断「连续两 tick 高功率」证据链。
            state.pop("_on_flip_high_at", None)
            state["_phantom_gate_at"] = now_ts
            return
        # v8.46 修复①（2026-09-04 审计）: 打锚点前要求**压缩机级负载证据**——
        # 连续两个 tick >50W（≤7min 间隔，容忍丢一拍）或本 tick >300W（压缩机
        # 级单帧铁证）。仅一次 50W<p≤300W → 打 _phantom_gate_at 观察一拍、
        # 不动 state，下一 tick 复核。背景：今晨幻象翻转以 16-24 分钟间隔拿到
        # 单帧 >50W/None 读数即入账，comfort_weight 再次被推满 1.0。
        if load_power is not None and load_power <= 300:
            _strong_pair = False
            _prev_high = state.get("_on_flip_high_at")
            if _prev_high:
                try:
                    _ph = (
                        datetime.fromisoformat(_prev_high)
                        if isinstance(_prev_high, str)
                        else _prev_high
                    )
                    _now_dt = (
                        datetime.fromisoformat(now_ts)
                        if isinstance(now_ts, str)
                        else now_ts
                    )
                    if 0 < (_now_dt - _ph).total_seconds() <= 7 * 60:
                        _strong_pair = True
                except Exception:
                    pass
            if _strong_pair:
                state.pop("_on_flip_high_at", None)  # 证据链已消费
            else:
                # v8.48 修复③: 观察一拍只打 _on_flip_high_at，不再打
                # _phantom_gate_at——后者是 _anchor_oscillating 的标记，
                # 观察拍自设标记会误拦下一 tick 的双tick证据链。
                state["_on_flip_high_at"] = now_ts
                return
        # v8.48 修复②: 震荡检测扩容到全部路径（原仅 None 路径）。
        # 幻象带功率读数时 v8.46 的「铁证」判据会被穿透，震荡窗口
        # （35min）内任何来源的开翻转一律静默；真运行由 H2 接管兜底。
        if _anchor_oscillating(state, now_ts):
            state.pop("_on_flip_high_at", None)
            state["_phantom_gate_at"] = now_ts
            return
        # 打锚点：真手动开机（尊重意图，state 立即对账）。
        state["manual_on_at"] = now_ts
        state["mode"] = "cooling"
        state["run_start"] = now_ts
        state["_fake_run_count"] = 0
        # v8.48 修复①: 学习喂入统一延迟验证——功率铁证路径不再立即入账。
        # 幻象功率读数与 is_on 同源，可穿透 v8.46「铁证」（09-04 晚实证:
        # kWh今冻结仍 4 锚点+4 次立即学习，comfort_weight 0.5→0.8）；
        # 唯一不可伪造的物理现实是 kWh 增量，真压缩机 10min 必过 0.005 闸。
        state["_pending_manual_on_learn"] = {
            "ts": now_ts,
            "kwh": state.get("estimated_kwh"),
        }


def verify_socket():
    if AC_CTRL is None:
        return None
    try:
        s = AC_CTRL.status()
        return "on" if s.is_on else "off"
    except:
        return None


def verify_target_temp():
    """v8.36 (hy4审计#7): 回读空调**实际**设定温度。

    verify_socket() 只验 is_on，不验目标温度。设温指令失败（或空调拒绝该档位）
    时，state 记的是期望值而设备是另一个值，decide() 用
    `temp <= current_target + DAY_TEMP_REACHED_SLACK` 判断是否达标就会永远
    判不达标，压缩机一路跑到 WATCH_MAX_RUN=90 分钟被强关。
    不可读（离线/不支持）时返回 None，由调用方按"未验证"处理。
    """
    if AC_CTRL is None:
        return None
    try:
        s = AC_CTRL.status()
        t = getattr(s, "target_temperature", None)
        return t if isinstance(t, (int, float)) and not isinstance(t, bool) else None
    except:
        return None


def apply_state_from_verify(state, new_mode, real, now_ts):
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


def apply_and_commit(
    new_mode, target_temp, state, now_ts=None, meta=None, tts_reason=None
):
    if now_ts is None:
        now_ts = datetime.now().isoformat(timespec="seconds")
    ctrl = ac_apply(new_mode, target_temp)
    if ctrl["status"] == "failed":
        save_state(state)
        return ctrl
    real = verify_socket()
    if real is None:
        ctrl = {
            "status": "failed",
            "action": ctrl.get("action", ""),
            "reason": "verify_unreachable",
        }
        save_state(state)
        return ctrl
    contradict = apply_state_from_verify(state, new_mode, real, now_ts)
    if contradict:
        ctrl = {
            "status": "failed",
            "action": ctrl.get("action", ""),
            "reason": "verify_on_after_off" if real == "on" else "verify_off_after_on",
        }
    if meta and not contradict:
        for k, v in meta.items():
            state[k] = v
    if not contradict and target_temp is not None:
        # v8.36 fix (hy4审计#7): 原来无条件写入期望值。若设温实际未生效（指令失败、
        # 或空调拒绝该档位），state 记 24 而设备是 26 → 后续 decide() 的达标判据
        # `temp <= current_target + DAY_TEMP_REACHED_SLACK` 永不满足 → 无效长跑到
        # WATCH_MAX_RUN=90min 强关。改为回读实测值：一致即记账，不一致则以实测为准
        # 并留下 _target_drift 供排查（实测不可读时保持原行为，记期望值）。
        state["target_temp"] = target_temp
        _real_t = verify_target_temp()
        if _real_t is not None and abs(_real_t - target_temp) >= 0.5:
            state["target_temp"] = _real_t
            state["_target_drift"] = {"want": target_temp, "got": _real_t}
        else:
            state.pop("_target_drift", None)
    save_state(state)
    if tts_reason and not contradict:
        try:
            from ac_watch import tts_speak

            tts_speak(tts_reason)
        except:
            pass
    return ctrl


def read_ac_power(timeout=4.0):
    global AC_MEASURED_W, AC_SOCKET, AC_COMPANION_TARGET
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
        # v8.42: 顺手读伴侣设定温度（回声字段，仅镜像我方发射的命令；用户改温不更新——2026-08-29 实验证实）
        AC_COMPANION_TARGET = getattr(st, "target_temperature", None)
        # v8.43: 功率读数不再受 is_on 门控——待机 1W 也是幻象门控的判据输入。
        # 旧逻辑 is_on=False 时 AC_MEASURED_W 恒 None，reconcile 功率门控会被架空回退旧语义。
        if st.load_power and st.load_power > 0:
            AC_MEASURED_W = round(st.load_power)
            return AC_MEASURED_W
    except:
        pass
    return None


NIGHT_HOURS = 6
DAYS_PER_MONTH = 30


def _learn_from_manual(state, now_ts):
    try:
        rh_hist = state.get("rh_history", [])
        if not rh_hist:
            return
        last_rh = rh_hist[-1][1] if rh_hist else None
        if last_rh is None:
            return
        pref = state.get("user_pref", {})
        manual_log = pref.get("manual_on_log", [])
        manual_log.append({"ts": now_ts, "rh": last_rh, "mode": state.get("mode")})
        if len(manual_log) > 20:
            manual_log = manual_log[-20:]
        pref["manual_on_log"] = manual_log
        low_rh_manual = [m for m in manual_log if 60 <= m.get("rh", 0) < 65]
        if len(low_rh_manual) >= 3:
            pref["hum_threshold"] = 60
        else:
            pref.pop("hum_threshold", None)
        recent = manual_log[-10:]
        manual_on_count = sum(1 for m in recent if m.get("mode") == "cooling")
        manual_off_count = sum(1 for m in recent if m.get("mode") == "off")
        # v8.45 fix (审计 2026-09-02): 原写法 open 失败时 user_pref 未绑定，
        # 下方棘轮分支 user_pref["comfort_weight"]=... 抛 NameError 被外层
        # except 吞掉 → ac_user_pref.json 缺失/损坏时 comfort_weight 学习整体
        # 静默失效。改为先初始化空 dict，读入独立变量：文件缺失时从 0.5 起步
        # 并正常写回，行为与文件存在时一致。
        user_pref = {}
        try:
            with open(os.path.join(SCRIPT_DIR, "ac_user_pref.json")) as f:
                user_pref = json.load(f)
        except Exception:
            user_pref = {}
        current_cw = user_pref.get("comfort_weight", 0.5)
        # v8.44 fix (2026-09-01 夜间审计 P1): 原判定 `if on_count>=3 / elif off_count>=3`
        # 是单向优先棘轮——幻象震荡循环（on/off 交替，recent-10 各 ~5 条）时
        # on_count 恒 ≥3 → 每次手动事件都 +0.1，off 分支永不触发 → comfort_weight
        # 只涨不跌被推满 1.0（0.4→1.0 仅需 6 次事件，实测 40 分钟内被吃掉）。
        # 改净优势判定：交替场景净差 0 → 不动；真实偏好（反复手动开/关）才调整。
        if manual_on_count - manual_off_count >= 2 and current_cw < 1.0:
            user_pref["comfort_weight"] = round(min(1.0, current_cw + 0.1), 1)
            try:
                # v8.36+: json.dump 不写末尾换行，每次回写都会产生 "\ No newline at end of file" 的 git 噪音
                with open(os.path.join(SCRIPT_DIR, "ac_user_pref.json"), "w") as f:
                    json.dump(user_pref, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            except:
                pass
        elif manual_off_count - manual_on_count >= 2 and current_cw > 0.1:
            user_pref["comfort_weight"] = round(max(0.1, current_cw - 0.1), 1)
            try:
                with open(os.path.join(SCRIPT_DIR, "ac_user_pref.json"), "w") as f:
                    json.dump(user_pref, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            except:
                pass
        state["user_pref"] = pref
    except:
        pass


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
    lines.append(f"   2️⃣ 睡眠+制冷24°C整夜:  {k24:.2f}度 ≈ {k24 * p:.2f}元")
    lines.append(f"   3️⃣ 除湿模式整夜:        {kd:.2f}度 ≈ {kd * p:.2f}元")
    if indoor_hum is not None and indoor_hum > 70:
        lines.append("   💡 湿度偏高：先压轮24°C到60%再睡")
    else:
        lines.append("   💡 湿度不高：压轮收工最省")
    return lines


def filter_clean_reminder():
    FILTER_STATE_FILE = os.path.join(SCRIPT_DIR, "filter_state.json")
    FILTER_CLEAN_INTERVAL = 30
    try:
        with open(FILTER_STATE_FILE, encoding="utf-8") as f:
            last = json.load(f).get("last_clean")
        if not last:
            return "  💡 记得每 15~30 天洗一次滤网"
        days = (datetime.now() - datetime.fromisoformat(last)).days
        if days > FILTER_CLEAN_INTERVAL:
            return f"  ⚠️ 该洗滤网了（距上次 {days} 天）"
        return None
    except:
        return None


if __name__ == "__main__":
    print("⚠️ ac_advisor.py 已合并为纯库，不再独立运行。")
    sys.exit(0)
