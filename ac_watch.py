#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调自动监控 v8.27 — 每 2 分钟自动环（Hermes cron: */2 * * *）

v8.27 变更：
  - 新增 TTS 语音播报（夜间静音）
  - 天气预冷：谷电时段读未来 3h 预报，超阈值提前开
  - 成本最优调度器（独立脚本）
  - 成本对比基准（独立脚本）
  - RC 热模型拟合（数据不足，暂缓）
"""

import os
import sys
import json
import math
import pyttsx3
import threading
from datetime import datetime, timedelta

# ── TTS 语音 ──
_tts_engine = None
_tts_lock = threading.Lock()


def tts_speak(text):
    """后台语音播报（不阻塞主循环）"""
    global _tts_engine
    if not text:
        return
    try:
        with _tts_lock:
            if _tts_engine is None:
                _tts_engine = pyttsx3.init()
            _tts_engine.say(text)
            _tts_engine.runAndWait()
    except Exception:
        pass


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_advisor as A
from ac_advisor import evaluate_and_learn as evaluate
from ac_advisor import log_decision as log_decision

# ── 并发锁 ──
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ac_watch.lock")
_LOCK_FD = None
_LOCK_STALE_SEC = 120
import atexit


def _cleanup_lock():
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            _LOCK_FD.close()
        except Exception:
            pass
        _LOCK_FD = None
    try:
        if os.path.exists(_LOCK_FILE):
            os.remove(_LOCK_FILE)
    except Exception:
        pass


def acquire_lock():
    global _LOCK_FD
    try:
        import msvcrt
    except ImportError:
        return True
    try:
        _LOCK_FD = open(_LOCK_FILE, "w")
        try:
            msvcrt.locking(_LOCK_FD.fileno(), msvcrt.LK_NBLCK, 1)
            atexit.register(_cleanup_lock)
            return True
        except OSError:
            _LOCK_FD.close()
            _LOCK_FD = None
            age = ((datetime.now() - datetime.fromtimestamp(os.path.getmtime(_LOCK_FILE)))
                   .total_seconds()) if os.path.exists(_LOCK_FILE) else 0
            if age < _LOCK_STALE_SEC:
                return False
            _LOCK_FD = open(_LOCK_FILE, "w")
            msvcrt.locking(_LOCK_FD.fileno(), msvcrt.LK_NBLCK, 1)
            atexit.register(_cleanup_lock)
            return True
    except Exception:
        return True

WATCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_watch.log")
WATCH_MAX_RUN = 90

# ── 阈值常量 ──
NIGHT = (23, 7)
NIGHT_START_T = 27.0
NIGHT_START_AH = 15.5
NIGHT_STOP_AH = 14.0
NIGHT_START_AH_HYST = 0.5
NIGHT_TARGET = 26
NIGHT_MIN_TARGET = 24
DAY_COOL_STOP_T = 22
DAY_COOL_STOP_AH = 15.0
DAY_STOP_AH = 14.5
DAY_EXIT_RH_MAX = 62
DUAL_STOP_MIN_COMP = 10
NIGHT_MIN_COMP_ON = 20
NIGHT_MAX_STARTS_PER_H = 4
DAY_MAX_STARTS_PER_H = 2

DAY_STOP_AH_HYST = 2.0
DAY_TEMP_REACHED_SLACK = 1.0

STEADY_STATE_MIN_MIN = 15
THERMAL_FAIL_WINDOW = 3
THERMAL_FAIL_RISE = 1

DAY_STARTS_OVERRIDE_T = 29.0

OUTDOOR_HOT_T = 30
HOT_DAY_TEMP_DROP = 3
HOT_DAY_TARGET_FLOOR = 24

SUSTAIN_MIN = 10
SUSTAIN_URGENT_T = 29.0

SENSOR_FALLBACK_OFF_ALLOWED = True
SENSOR_FALLBACK_ON_ALLOWED = False

COMPRESSOR_POWER_THRESHOLD = 300
FAN_ONLY_POWER_MAX = 50
COMPRESSOR_FALSE_RUN_MIN = 10
COMPRESSOR_RESTART_COOLDOWN = 30
COMPRESSOR_RESTART_DROP = 2

KWH_MAX_GAP_MIN = 10

VENT_GATE_DP_DIFF = 1.5
VENT_GATE_MAX_RH = 69
VENT_GATE_HOURS = (8, 22)
VENT_WX_TTL_MIN = 30

EVENING = (20, 23)
EVENING_TARGET = 26
EVENING_START_T = 26.5

DEHUMID_EXIT_RH = 55
DEHUMID_LOW_EFF_RH = 66
DEHUMID_DELTA_RH_MIN = -1.0
DEHUMID_ADJUST_COOLDOWN = 20
DEHUMID_FORCE_MIN = 40
DEHUMID_FORCE_RH = 68
DEHUMID_STALL_MIN = 60
DEHUMID_STALL_RH_BAND = 1.5
DEHUMID_STEP_C = 1
DEHUMID_MIN_TARGET = 16
DEHUMID_START_TARGET = 25
VALLEY_START_RH = 62

VIRTUAL_INV_APPROACH_RH = 58
VIRTUAL_INV_MAX_TARGET = 26
VIRTUAL_INV_RECOVER_RH = 62

SENSOR_PLAUSIBLE_T_MIN = 10
SENSOR_PLAUSIBLE_T_MAX = 45
SENSOR_PLAUSIBLE_RH_MIN = 20
SENSOR_PLAUSIBLE_RH_MAX = 98
SENSOR_TIMEOUT_ESCALATE = 20
FAKE_RUN_MAX_CYCLES = 3
MANUAL_ANCHOR_TTL = 720


def log(msg):
    try:
        with open(WATCH_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def night_hours(now=None):
    h = (now or datetime.now()).hour
    return h >= NIGHT[0] or h < NIGHT[1]


def dew_point(temp_c, rh):
    if temp_c is None or rh is None:
        return None
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    return (b * gamma) / (a - gamma)


def absolute_humidity(temp_c, rh):
    if temp_c is None or rh is None:
        return None
    e = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
    return (e * rh * 2.1674) / (273.15 + temp_c)


def compressor_state(load_power):
    if load_power is None:
        return "unknown"
    if load_power > COMPRESSOR_POWER_THRESHOLD:
        return "compressor"
    if load_power > FAN_ONLY_POWER_MAX:
        return "unknown"
    if load_power > 5:
        return "fan_only"
    return "off"


def compute_delta_rh(rh_history, now_ts, window_min=20):
    if not rh_history or len(rh_history) < 2:
        return None, len(rh_history) if rh_history else 0
    now = datetime.fromisoformat(now_ts)
    cutoff = now - timedelta(minutes=window_min)
    in_window = []
    for ts_str, rh in rh_history:
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts >= cutoff:
                in_window.append(rh)
        except Exception:
            continue
    if len(in_window) < 2:
        return None, len(in_window)
    delta = in_window[-1] - in_window[0]
    return round(delta, 1), len(in_window)


def update_rh_history(state, now_ts, rh):
    hist = state.get("rh_history") or []
    hist.append([now_ts, rh])
    if len(hist) > 12:
        hist = hist[-12:]
    state["rh_history"] = hist


def update_temp_history(state, now_ts, temp):
    hist = state.get("temp_history") or []
    hist.append([now_ts, temp])
    if len(hist) > 12:
        hist = hist[-12:]
    state["temp_history"] = hist


def is_temp_stable(state, target, slack, window_min):
    hist = state.get("temp_history") or []
    if len(hist) < 2:
        return None
    now = datetime.now()
    cutoff = now - timedelta(minutes=window_min)
    in_window = []
    for ts_str, t in hist:
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts >= cutoff and t is not None:
                in_window.append(t)
        except Exception:
            continue
    if len(in_window) < 2:
        return None
    return all(abs(t - target) <= slack for t in in_window)


def sustained_above(state, line, need_min, now_ts=None):
    hist = state.get("temp_history") or []
    if len(hist) < 2:
        return None
    now = (datetime.fromisoformat(now_ts) if isinstance(now_ts, str)
           else (now_ts or datetime.now()))
    cutoff = now - timedelta(minutes=need_min)
    in_window = []
    for ts_str, t in hist:
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if ts >= cutoff and t is not None:
            in_window.append(t)
    if len(in_window) < 2:
        return None
    span = (now - datetime.fromisoformat(hist[0][0])).total_seconds() / 60
    if span < need_min:
        return None
    return all(t >= line for t in in_window)


def vent_gate_decision(hour, hum, temp, rain, dp_out, dp_in):
    if dp_out is None or dp_in is None:
        return False
    return dp_out <= dp_in - VENT_GATE_DP_DIFF


def cached_outdoor(state, now_dt):
    c = state.get("_vent_wx_cache")
    if c:
        try:
            ts = datetime.fromisoformat(c["ts"])
            if (now_dt - ts).total_seconds() < VENT_WX_TTL_MIN * 60:
                return c["wx"]
        except Exception:
            pass
    try:
        wx = A.fetch_weather()
        if "error" not in wx:
            cur = wx.get("current", {})
            state["_vent_wx_cache"] = {
                "ts": now_dt.isoformat(timespec="seconds"),
                "wx": {
                    "t": cur.get("temperature_2m"),
                    "rh": cur.get("relative_humidity_2m"),
                    "rain": cur.get("precipitation_probability_max", [0])[0],
                },
            }
            return state["_vent_wx_cache"]["wx"]
    except Exception:
        pass
    return None


def update_kwh(state, now_ts, load_power):
    prev_power = state.get("_prev_power")
    prev_ts = state.get("_prev_kwh_ts")
    kwh = state.get("estimated_kwh", 0.0)
    if prev_power is not None and prev_ts is not None and load_power is not None:
        try:
            dt_hours = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(prev_ts)).total_seconds() / 3600
            if dt_hours <= KWH_MAX_GAP_MIN / 60:
                kwh += (prev_power + load_power) / 2 * dt_hours / 1000
        except Exception:
            pass
    state["estimated_kwh"] = round(kwh, 4)
    if load_power is not None:
        state["_prev_power"] = load_power
        state["_prev_kwh_ts"] = now_ts


def stale_stop_ts(old_ts, run_start_ts):
    if not old_ts:
        return True
    try:
        old = datetime.fromisoformat(old_ts) if isinstance(old_ts, str) else old_ts
        run_start = datetime.fromisoformat(run_start_ts) if isinstance(run_start_ts, str) else run_start_ts
        return old < run_start
    except Exception:
        return False


def open_cycle(state, now_ts, ah, rh, temp=None, outdoor_temp=None):
    state["cycle_start"] = {
        "ts": now_ts,
        "ah": ah,
        "rh": rh,
        "temp": temp,
        "outdoor_temp": outdoor_temp,
    }


def close_cycle(state, now_ts, ah, rh, target_temp, comp_min, path=None, abort_reason=None,
                temp=None, outdoor_temp=None):
    cs = state.get("cycle_start")
    if not cs:
        return False
    try:
        start_ts = datetime.fromisoformat(cs["ts"])
        dur_min = round((datetime.fromisoformat(now_ts) - start_ts).total_seconds() / 60, 1)
    except Exception:
        dur_min = 0
    rec = {
        "start_ts": cs["ts"],
        "end_ts": now_ts,
        "start_AH": cs["ah"],
        "end_AH": ah,
        "start_RH": cs["rh"],
        "end_RH": rh,
        "start_temp": cs.get("temp"),
        "end_temp": temp,
        "start_outdoor_temp": cs.get("outdoor_temp"),
        "end_outdoor_temp": outdoor_temp,
        "target_temp": target_temp,
        "compressor_runtime_min": round(comp_min, 1) if comp_min else 0,
        "kwh_used": round(state.get("estimated_kwh", 0) - cs.get("kwh", 0), 4),
        "duration_min": dur_min,
        "duty": min(1.0, dur_min / 60) if dur_min else 0,
        "duty_invalid": False,
        "abort_reason": abort_reason,
        "rh_spike": (rh is not None and cs.get("rh") is not None
                     and rh > cs["rh"] + 3),
    }
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cycle_log.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state.pop("cycle_start", None)
    return True


def handle_cycle_after_action(state, new_mode, mode_before, now_ts, ah, hum, running_target,
                          comp_min, path=None, abort_reason=None,
                          temp=None, outdoor_temp=None):
    if new_mode == "cooling" and mode_before != "cooling":
        open_cycle(state, now_ts, ah, hum, temp=temp, outdoor_temp=outdoor_temp)
        return True
    if new_mode == "off" and mode_before in ("cooling", "dehumid", "dehumid_alert"):
        return close_cycle(state, now_ts, ah, hum, running_target, comp_min, path=path,
                           abort_reason=abort_reason, temp=temp, outdoor_temp=outdoor_temp)
    return False


def decide(temp, hum, running, since_on, since_off, is_night,
           compressor=None, compressor_stop_duration_min=None, cooldown_until_dt=None,
           current_target=26, delta_rh_20min=None, delta_rh_60min=None,
           minutes_since_last_adjust=None, ah=None, compressor_run_min=None,
           night_comp_starts=None, fake_run_count=None, evening=False,
           outdoor_temp=None, outdoor_rain=None, sustained=None,
           is_steady_state=False, predicted_cool_min=None):
    """纯决策函数。返回 (new_mode, target_temp, reason) 或 (None, None, None)。"""

    learned = A.load_learned()
    adj = learned.get("adjusted_thresholds", {}).get("temp_cooling", 0)
    temp_cooling = A.TEMP_COOLING + adj

    if running is None:
        return (None, None, None)
    if running:
        if compressor == "fan_only":
            stop_duration = None
            if compressor_stop_duration_min is not None and since_on is not None:
                stop_duration = compressor_stop_duration_min
                if stop_duration < 0:
                    stop_duration = None
            in_cooldown = False
            if cooldown_until_dt is not None:
                in_cooldown = datetime.now() < cooldown_until_dt

            if fake_run_count is not None and fake_run_count >= FAKE_RUN_MAX_CYCLES:
                return ("off", None,
                        f"假运行已连续{int(fake_run_count)}次，空调可能硬件故障，停止自动重试")

            if (hum > DEHUMID_LOW_EFF_RH
                    and stop_duration is not None
                    and stop_duration >= COMPRESSOR_FALSE_RUN_MIN
                    and not in_cooldown):
                new_target = max(DEHUMID_MIN_TARGET, current_target - COMPRESSOR_RESTART_DROP)
                return ("cooling", new_target,
                        f"压缩机只吹风不制冷已{int(stop_duration)}分钟，湿度{hum:.0f}%仍偏高，降2度重启压缩机")

            if hum <= DEHUMID_EXIT_RH:
                return ("off", None, f"湿度已降到{hum:.0f}%，压缩机已停，关机省电")

            if (hum >= VIRTUAL_INV_RECOVER_RH
                    and current_target > A.TEMP_ABSOLUTE_FLOOR + 1
                    and not in_cooldown):
                return ("cooling", 24,
                        f"升温缓除后湿度回升到{hum:.0f}%，降回24度继续除湿")

            if (hum <= DEHUMID_LOW_EFF_RH
                    and stop_duration is not None
                    and stop_duration >= COMPRESSOR_FALSE_RUN_MIN):
                return ("off", None,
                        f"压缩机已停、湿度{hum:.0f}%接近目标，不再吹风空耗，关机省电")
            return (None, None, None)

        comp_min = compressor_run_min if compressor_run_min is not None else since_on

        if comp_min is not None and comp_min >= WATCH_MAX_RUN and not evening:
            return ("off", None, f"压缩机已连续运行{int(comp_min)}分钟，为保护压缩机强行关机")

        if is_night:
            if ah is not None and ah <= NIGHT_STOP_AH:
                temp_reached = (current_target is None
                                or temp <= current_target + DAY_TEMP_REACHED_SLACK)
                if temp_reached:
                    return ("off", None, f"夜间室内湿度已达标（AH={ah:.1f}），关机省电")

            if (hum <= DEHUMID_EXIT_RH and comp_min is not None
                    and comp_min >= NIGHT_MIN_COMP_ON):
                if current_target is None or temp <= current_target + DAY_TEMP_REACHED_SLACK:
                    return ("off", None, f"夜间湿度已降到{hum:.0f}%，压缩机工作完成关机")
            if temp <= A.TEMP_ABSOLUTE_FLOOR:
                return ("off", None, f"夜间室温{temp:.0f}度低于绝对下限{A.TEMP_ABSOLUTE_FLOOR}度，逃生门关机")
            if comp_min is not None and comp_min < NIGHT_MIN_COMP_ON:
                return (None, None, None)
            if night_comp_starts and len(night_comp_starts) >= NIGHT_MAX_STARTS_PER_H:
                return (None, None, None)

        if not is_night and temp <= DAY_COOL_STOP_T:
            if ah is not None and ah <= DAY_COOL_STOP_AH:
                return ("off", None, f"温度已降到{temp:.0f}度不闷，过冷保护关机")

        if temp < A.TEMP_ABSOLUTE_FLOOR:
            return ("off", None, f"温度{temp:.0f}度低于绝对下限{A.TEMP_ABSOLUTE_FLOOR}度，逃生门无条件关机")

        if (not is_night and not evening and ah is not None and hum <= DAY_EXIT_RH_MAX
                and ah <= DAY_STOP_AH
                and comp_min is not None and comp_min >= DUAL_STOP_MIN_COMP):
            temp_reached = (current_target is None
                            or temp <= current_target + DAY_TEMP_REACHED_SLACK)
            if temp_reached:
                return ("off", None, f"含水量已达标（AH={ah:.1f}，RH={hum:.0f}%），关机防过冷")

        if comp_min is not None and comp_min < A.MIN_RUN:
            return (None, None, None)

        if (not is_night and not evening and is_steady_state
                and comp_min is not None and comp_min >= A.MIN_RUN):
            return (None, None, None)

        if not evening and hum <= DEHUMID_EXIT_RH and comp_min is not None and comp_min >= A.MIN_RUN:
            return ("off", None, f"湿度已达标降到{hum:.0f}%，压缩机工作完成关机")

        if not evening and hum <= DEHUMID_EXIT_RH and predicted_cool_min is not None and predicted_cool_min <= 5:
            return ("off", None, f"湿度达标{hum:.0f}%，热模型预测再跑{predicted_cool_min}分钟即可到位，提前关省电")

        if (hum <= VIRTUAL_INV_APPROACH_RH
                and comp_min is not None and comp_min >= A.MIN_RUN
                and current_target is not None and current_target < VIRTUAL_INV_MAX_TARGET
                and minutes_since_last_adjust is not None
                and minutes_since_last_adjust >= DEHUMID_ADJUST_COOLDOWN):
            new_target = min(VIRTUAL_INV_MAX_TARGET, current_target + 1)
            return ("cooling", new_target,
                    f"湿度近达标（{hum:.0f}%），目标升1度到{new_target}度缓除防过冷")

        if (comp_min is not None and comp_min >= DEHUMID_STALL_MIN and not evening
                and delta_rh_60min is not None
                and delta_rh_60min > -DEHUMID_STALL_RH_BAND):
            return ("off", None, f"压缩机跑了{int(comp_min)}分钟湿度降幅不足，判定无效空耗关机")

        if delta_rh_20min is not None and delta_rh_20min <= DEHUMID_DELTA_RH_MIN:
            return (None, None, None)

        if hum > DEHUMID_LOW_EFF_RH and delta_rh_20min is not None and delta_rh_20min > DEHUMID_DELTA_RH_MIN:
            if minutes_since_last_adjust is not None and minutes_since_last_adjust < DEHUMID_ADJUST_COOLDOWN:
                return (None, None, None)
            if comp_min is not None and comp_min >= DEHUMID_FORCE_MIN and hum > DEHUMID_FORCE_RH:
                new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
                return ("cooling", new_target, f"除湿太慢湿度{hum:.0f}%还降不下来，再降1度加强除湿")
            new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
            return ("cooling", new_target, f"除湿偏慢，目标温度降1度到{new_target}度")

        return (None, None, None)

    # ── 未运行 ──
    effective_min_off = A.MIN_OFF if is_night else A.DAY_MIN_OFF
    if since_off is not None and since_off < effective_min_off:
        return (None, None, None)
    if is_night:
        night_target = max(NIGHT_MIN_TARGET, min(NIGHT_TARGET, round(temp - 2)))
        if temp >= NIGHT_START_T:
            if temp < SUSTAIN_URGENT_T and sustained is False:
                return (None, None, None)
            return ("cooling", night_target, f"夜间室温{temp:.0f}度偏热，自动开制冷{night_target}度")
        if (ah is not None and ah >= NIGHT_START_AH + NIGHT_START_AH_HYST
                and temp - night_target >= 1):
            return ("cooling", night_target, f"夜间感觉闷（湿度高），自动开制冷{night_target}度压一压")
        return (None, None, None)

    # ── v8.21 白天启停次数上限 ──
    if (night_comp_starts and len(night_comp_starts) >= DAY_MAX_STARTS_PER_H
            and temp < DAY_STARTS_OVERRIDE_T):
        return (None, None, None)

    # 峰谷电：峰电提高阈值（晚开省电）
    is_peak = A.current_price() >= A.ELECTRIC_PEAK
    temp_threshold = temp_cooling + (1.0 if is_peak else 0.0)

    # 峰电时：温度未达提高阈值 → 不动作（晚开省电）
    if is_peak and temp < temp_threshold:
        return (None, None, None)

    hot_day = outdoor_temp is not None and outdoor_temp >= OUTDOOR_HOT_T
    if temp >= temp_cooling:
        if (temp < SUSTAIN_URGENT_T and sustained is False
                and hum < A.HUM_DEHUMID_ON):
            return (None, None, None)
        if hum >= A.HUM_DEHUMID_ON:
            return ("cooling", DEHUMID_START_TARGET, f"室内{temp:.0f}度湿度{hum:.0f}%闷热，制冷{DEHUMID_START_TARGET}度先除湿")
        drop = HOT_DAY_TEMP_DROP if hot_day else 2
        t = round(max(HOT_DAY_TARGET_FLOOR if hot_day else 25, min(28, temp - drop)))
        wx_note = f"（室外{outdoor_temp:.0f}度炎热，多压1度少启停）" if hot_day else ""
        return ("cooling", t, f"室内{temp:.0f}度偏热，自动开制冷{t}度{wx_note}")

    # v8.14 E 方案（谷电积极版）：22-6 谷电半价 → 除湿启动阈值 65→62 更早压湿；
    # 峰电维持原阈值不推迟（保舒适，不牺牲体验）
    if temp >= temp_cooling - 1 and hum >= (VALLEY_START_RH if A.current_price() < A.ELECTRIC_PEAK else A.HUM_DEHUMID_ON):
        if ah is not None and ah < DAY_STOP_AH + DAY_STOP_AH_HYST:
            return (None, None, None)
        return ("cooling", DEHUMID_START_TARGET, f"室内{temp:.0f}度湿度{hum:.0f}%闷热，制冷{DEHUMID_START_TARGET}度强力除湿")

    # v8.18 晚间恒温巡航
    if evening and temp >= EVENING_START_T:
        return ("cooling", EVENING_TARGET, f"晚间恒温巡航{EVENING_TARGET}度（温度优先，电耗与锯齿打平）")

    return (None, None, None)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    now_ts = datetime.now().isoformat(timespec="seconds")
    now_dt = datetime.now()
    dry = "--dry" in sys.argv

    A.read_ac_power()
    A.ac_control_init()
    socket = A.AC_SOCKET
    load_power = A.AC_MEASURED_W
    temp, hum = A.read_indoor()

    if temp is not None and hum is not None:
        if not (SENSOR_PLAUSIBLE_T_MIN <= temp <= SENSOR_PLAUSIBLE_T_MAX
                and SENSOR_PLAUSIBLE_RH_MIN <= hum <= SENSOR_PLAUSIBLE_RH_MAX):
            log(f"传感器读数越界：T={temp} RH={hum}%，视为不可达")
            temp = hum = None

    state = A.load_state()
    state["last_temp"] = temp if temp is not None else state.get("last_temp")
    state["last_hum"] = hum if hum is not None else state.get("last_hum")
    if state.get("_state_load_failed"):
        log("[ERROR] 状态文件损坏，本次 tick fail-safe 跳过（不执行开/关）")
        print("ac_watch: 状态文件损坏，fail-safe 跳过（不执行开/关）")
        return
    A.reconcile_state(state, now_ts)

    wx_fallback_used = False
    if temp is None or hum is None:
        try:
            _wx_fallback = cached_outdoor(state, now_dt)
        except Exception as e:
            _wx_fallback = None
            log(f"[WARN] 天气兜底获取失败：{type(e).__name__}: {e}")
        if _wx_fallback and _wx_fallback.get("t") is not None:
            temp = _wx_fallback["t"]
            hum = None
            wx_fallback_used = True
            state["_temp_src"] = "outdoor_fallback"
            state["_hum_src"] = "outdoor_fallback"
            log(f"传感器离线，回退到天气预报 T={temp}°C RH={hum}%")

    if temp is None or hum is None:
        sensor_off_since = state.get("_sensor_off_since")
        if sensor_off_since is None:
            state["_sensor_off_since"] = now_ts
        else:
            sensor_off_min = A.minutes_since(sensor_off_since)
            if sensor_off_min is not None and sensor_off_min >= SENSOR_TIMEOUT_ESCALATE:
                if state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
                    log(f"传感器断连{sensor_off_min:.0f}分钟，空调运行中，保守关机")
                    print(f"ac_watch: 传感器断连{sensor_off_min:.0f}分钟，空调运行中，执行保守关机")
                    A.apply_and_commit("off", None, state, now_ts)
                    state.pop("_sensor_off_since", None)
                    A.save_state(state)
                    return
        log(f"传感器不可达且无天气兜底，跳过 socket={socket}")
        print("ac_watch: 室内传感器不可达且无天气兜底，本次跳过")
        A.save_state(state)
        return

    if wx_fallback_used:
        log("传感器不可达但有天气预报兜底，继续控制（保留断连计时）")
        if state.get("_sensor_off_since") is None:
            state["_sensor_off_since"] = now_ts
    else:
        state.pop("_sensor_off_since", None)

    manual_off = state.get("manual_off_at")
    if manual_off and state.get("mode") in (None, "off"):
        try:
            off_dt = datetime.fromisoformat(manual_off) if isinstance(manual_off, str) else manual_off
            mins = (now_dt - off_dt).total_seconds() / 60
            if 0 <= mins < 30 and mins < MANUAL_ANCHOR_TTL:
                temp_at_off = None
                off_dt_str = state.get("manual_off_at")
                if off_dt_str:
                    try:
                        off_dt = datetime.fromisoformat(off_dt_str) if isinstance(off_dt_str, str) else off_dt_str
                        th = state.get("temp_history", [])
                        closest = None
                        for ts_str, t in th:
                            try:
                                ts = datetime.fromisoformat(ts_str)
                                if closest is None or abs((ts - off_dt).total_seconds()) < abs((closest[0] - off_dt).total_seconds()):
                                    closest = (ts, t)
                            except Exception:
                                pass
                        if closest:
                            temp_at_off = closest[1]
                    except Exception:
                        pass
                temp_rise = 0
                if temp_at_off is not None and temp is not None:
                    temp_rise = temp - temp_at_off
                if temp_rise >= 1.0:
                    log(f"手动关后{int(mins)}分钟，温度回升{temp_rise:.1f}°C，解除冷却期恢复自动")
                else:
                    log(f"手动关后{int(mins)}分钟，跳过自动启动（尊重用户意图）")
                    print(f"ac_watch: 手动关后{int(mins)}分钟，跳过自动启动")
                    A.save_state(state)
                    return
            if mins >= MANUAL_ANCHOR_TTL:
                log(f"手动关锚点已过期（{int(mins)}分钟 > {MANUAL_ANCHOR_TTL}），清除后恢复自动逻辑")
                state.pop("manual_off_at", None)
        except Exception:
            pass

    manual_on = state.get("manual_on_at")
    if manual_on and state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
        try:
            on_dt = datetime.fromisoformat(manual_on) if isinstance(manual_on, str) else manual_on
            mins = (now_dt - on_dt).total_seconds() / 60
            if 0 <= mins < 30 and mins < MANUAL_ANCHOR_TTL:
                log(f"手动开后{int(mins)}分钟，暂不自动关（保护用户意图）")
                print(f"ac_watch: 手动开后{int(mins)}分钟，暂不自动关")
                A.save_state(state)
                return
            if mins >= MANUAL_ANCHOR_TTL:
                log(f"手动开锚点已过期（{int(mins)}分钟 > {MANUAL_ANCHOR_TTL}），清除后恢复自动逻辑")
                state.pop("manual_on_at", None)
        except Exception:
            pass

    if socket is None:
        running = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    else:
        running = socket == "on" or state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    since_on = A.minutes_since(state.get("run_start"))
    since_off = A.minutes_since(state.get("last_off_at"))

    comp = compressor_state(load_power)
    last_comp_stop = state.get("last_compressor_stop_at")
    cooldown_until_str = state.get("compressor_restart_cooldown_until")
    cooldown = None
    if cooldown_until_str:
        try:
            cooldown = datetime.fromisoformat(cooldown_until_str)
        except Exception:
            pass
    state_comp_before = state.get("compressor_state")
    fake_run_count = state.get("_fake_run_count", 0) or 0

    starts = state.get("_night_comp_starts", [])
    cutoff = (now_dt - timedelta(hours=1)).isoformat(timespec="seconds")
    state["_night_comp_starts"] = [s for s in starts if s >= cutoff]

    if comp == "compressor" and state_comp_before != "compressor":
        state["compressor_restart_cooldown_until"] = (
            now_dt + timedelta(minutes=COMPRESSOR_RESTART_COOLDOWN)
        ).isoformat(timespec="seconds")
        state["last_compressor_start_at"] = now_ts
        state["_fake_run_count"] = 0
    elif comp == "fan_only" and state_comp_before == "compressor":
        state["last_compressor_stop_at"] = now_ts
        last_comp_stop = now_ts
        fake_run_count += 1
        state["_fake_run_count"] = fake_run_count
    elif comp == "fan_only" and state_comp_before != "compressor" and stale_stop_ts(
            state.get("last_compressor_stop_at"), state.get("run_start")):
        state["last_compressor_stop_at"] = now_ts
        last_comp_stop = now_ts
    state["compressor_state"] = comp

    comp_on_min = 0
    cycle_comp_total = state.get("cycle_comp_total", 0) or 0
    if comp == "compressor":
        comp_since = state.get("compressor_on_since")
        if comp_since:
            try:
                elapsed = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(comp_since)).total_seconds() / 60
                comp_on_min = round(max(0, elapsed), 1)
            except Exception:
                comp_on_min += 10
        else:
            state["compressor_on_since"] = now_ts
            comp_on_min = 0
        state["compressor_on_min"] = comp_on_min
    else:
        prev_comp_on_min = state.get("compressor_on_min", 0) or 0
        if prev_comp_on_min > 0:
            cycle_comp_total = round(cycle_comp_total + prev_comp_on_min, 1)
        comp_on_min = 0
        state["compressor_on_min"] = 0
        state.pop("compressor_on_since", None)
    state["cycle_comp_total"] = cycle_comp_total

    daily_increment = state.get("estimated_kwh", 0) - state.get("_prev_kwh", 0)
    if daily_increment > 0:
        state["_daily_kwh"] = state.get("_daily_kwh", 0) + daily_increment
    state["_prev_kwh"] = state.get("estimated_kwh", 0)

    update_kwh(state, now_ts, load_power)
    update_rh_history(state, now_ts, hum)
    update_temp_history(state, now_ts, temp)
    delta_rh_20, _ = compute_delta_rh(state.get("rh_history"), now_ts, 20)
    delta_rh_60, _ = compute_delta_rh(state.get("rh_history"), now_ts, 60)

    current_target = state.get("target_temp", 26) or 26
    dp = dew_point(temp, hum)
    ah = absolute_humidity(temp, hum)

    is_night = night_hours()
    evening = EVENING[0] <= now_dt.hour < EVENING[1]

    last_adjust = state.get("last_dehumid_adjust_at")
    minutes_since_adjust = A.minutes_since(last_adjust) if last_adjust else None

    stop_duration = None
    if last_comp_stop is not None and since_on is not None:
        try:
            stop_dt = datetime.fromisoformat(last_comp_stop) if isinstance(last_comp_stop, str) else last_comp_stop
            run_start = state.get("run_start")
            if run_start:
                run_dt = datetime.fromisoformat(run_start) if isinstance(run_start, str) else datetime.fromisoformat(run_start)
                stop_duration = since_on - (stop_dt - run_dt).total_seconds() / 60
                if stop_duration < 0:
                    stop_duration = None
        except Exception:
            pass

    try:
        _wx = cached_outdoor(state, now_dt)
    except Exception:
        _wx = None
    _outdoor_t = _wx["t"] if (_wx and "t" in _wx) else None
    _outdoor_rain = _wx.get("rain") if _wx else None

    _learned = A.load_learned()
    _adj = _learned.get("adjusted_thresholds", {}).get("temp_cooling", 0)
    _temp_cooling_adj = A.TEMP_COOLING + _adj

    _thermal_data = A.load_thermal_data()
    _model = _thermal_data.get("thermal_model", {})
    _predicted_cool_min = None
    if running:
        _predicted_cool_min = A.predict_cooling_time(temp, current_target, _outdoor_t, _model)

    # v8.26 天气预冷：谷电时段读未来 3h 预报，超阈值提前开
    _pre_cool = False
    _pre_cool_target = 24
    if not running and not is_night:
        _h = now_dt.hour
        if _h >= 22 or _h < 6:
            if _wx and "hourly" in _wx:
                _temps = _wx["hourly"].get("temperature_2m", [])
                _future_max = max(_temps[:3]) if len(_temps) >= 3 else None
                if _future_max is not None and _future_max >= A.OUTDOOR_HOT_T + 2:
                    _pre_cool_target = max(24, round(_future_max - 2))
                    _pre_cool = True
                    log(f"天气预冷：谷电 {_h} 点，未来 3h 最高 {_future_max}°C，提前开制冷 {_pre_cool_target}°C 蓄冷")

    new_mode, target, reason = decide(temp, hum, running, since_on, since_off, is_night,
                              compressor=comp,
                              compressor_stop_duration_min=stop_duration,
                              cooldown_until_dt=cooldown,
                              current_target=current_target,
                              delta_rh_20min=delta_rh_20,
                              delta_rh_60min=delta_rh_60,
                              minutes_since_last_adjust=minutes_since_adjust,
                              ah=ah,
                              compressor_run_min=(state.get("cycle_comp_total") or 0) + (state.get("compressor_on_min") or 0),
                              night_comp_starts=state.get("_night_comp_starts"),
                              fake_run_count=fake_run_count,
                              evening=evening,
                              outdoor_temp=_outdoor_t,
                              outdoor_rain=_outdoor_rain,
                              sustained=sustained_above(
                                  state,
                                  NIGHT_START_T if is_night else _temp_cooling_adj,
                                  SUSTAIN_MIN, now_ts),
                              is_steady_state=False,
                              predicted_cool_min=_predicted_cool_min)

    # v8.26 天气预冷执行
    if _pre_cool and new_mode is None:
        new_mode, target, reason = "cooling", _pre_cool_target, "天气预冷：谷电蓄冷"
        log(f"执行预冷：target={target}°C（室外{_outdoor_t}°C 未来更热）")

    COMP_LABEL = {"compressor": "压缩机运行", "fan_only": "仅风扇",
                  "off": "已关机", "unknown": "未知"}
    cl = COMP_LABEL.get(comp, comp)
    kwh_str = f"kWh={state.get('estimated_kwh', 0):.3f}"
    delta_str = f"dRH20={delta_rh_20}% dRH60={delta_rh_60}%"
    dp_str = f"dp={dp:.1f}C" if dp is not None else "dp=?"
    ah_str = f"AH={ah:.1f}" if ah is not None else "AH=?"
    night_str = "夜间" if is_night else "白天"
    meta = f"{night_str} T={temp} RH={hum}% {dp_str} {ah_str} {delta_str} {kwh_str} target={current_target}C"

    if new_mode is None:
        log(f"无动作 {cl} {meta} 已开={since_on} 已关={since_off} mode={state.get('mode')}")
        print(f"ac_watch: 无需动作 · {cl} · {meta}")
        A.save_state(state)
        evaluate(state, now_ts)
        return

    log_decision(state, new_mode, temp, hum, now_ts)

    if dry:
        print(f"ac_watch [dry]: 将执行 {new_mode} target={target} · {meta}")
        log(f"[dry] 将执行 {new_mode} target={target} · {meta}")
        evaluate(state, now_ts)
        return

    evaluate(state, now_ts)

    mode_before = state.get("mode")
    if new_mode == "cooling" and mode_before not in ("cooling", "dehumid", "dehumid_alert"):
        wx = cached_outdoor(state, now_dt)
        dp_out = dew_point(wx["t"], wx["rh"]) if wx else None
        if vent_gate_decision(now_dt.hour, hum, temp, wx and wx.get("rain"), dp_out, dp):
            log(f"vent_gate 拦截开机（室外干爽可免费除湿）· {meta}")
            state["_vent_skip_at"] = now_ts
            A.save_state(state)
            return

    extra_meta = None
    if new_mode == "cooling" and target is not None and target != current_target:
        extra_meta = {"last_dehumid_adjust_at": now_ts}

    mode_before = state.get("mode")
    running_target = current_target
    comp_min_at_apply = (state.get("cycle_comp_total") or 0) + (state.get("compressor_on_min") or 0)
    ctrl = A.apply_and_commit(new_mode, target, state, now_ts, meta=extra_meta)
    log(f"执行 {new_mode} target={target} → {ctrl['status']} {ctrl.get('action','')} {ctrl.get('reason','')} · {cl} {meta}")
    if ctrl["status"] == "action":
        if new_mode == "off":
            state["cycle_comp_total"] = 0
            state["compressor_on_min"] = 0
            state.pop("compressor_on_since", None)
        handle_cycle_after_action(state, new_mode, mode_before, now_ts, ah, hum, running_target,
                                  comp_min_at_apply, abort_reason=reason,
                                  temp=temp, outdoor_temp=_outdoor_t)
        state.pop("manual_off_at", None)
        A.save_state(state)
        if new_mode == "off" and mode_before == "cooling":
            run_start = state.get("run_start")
            if run_start:
                try:
                    run_dt = datetime.fromisoformat(run_start) if isinstance(run_start, str) else run_start
                    run_hours = (now_dt - run_dt).total_seconds() / 3600
                    if run_hours >= 2 and temp >= 24 and hum <= 70:
                        log(f"建议开窗换气：已连续制冷{run_hours:.1f}小时，室外温度可能更低")
                except Exception:
                    pass
        # v8.27 TTS 播报（夜间静音）
        if not night_hours():
            if new_mode == "cooling":
                tts_speak(f"已自动开空调制冷{target}度，{reason}")
            elif new_mode == "off":
                tts_speak(f"已自动关空调，{reason}")
        print(f"ac_watch: 已自动{ctrl['action']} · {meta}")
    elif ctrl["status"] == "no_action":
        print(f"ac_watch: 已处目标状态无需动作 · {meta}")
    else:
        print(f"ac_watch: 自动控制失败（{ctrl['reason']}）——建议人工确认空调状态")

    evaluate(state, now_ts)


def _selftest():
    # ── compressor_state ──
    comp_cases = [
        (None, "unknown"), (0, "off"), (3, "off"),
        (50, "fan_only"), (51, "unknown"), (300, "unknown"),
        (301, "compressor"), (1100, "compressor"),
    ]
    for power, expected in comp_cases:
        result = compressor_state(power)
        assert result == expected, f"compressor_state({power}) = {result}, expected {expected}"

    # ── absolute_humidity ──
    assert absolute_humidity(25, 60) is not None
    assert absolute_humidity(None, 60) is None
    assert absolute_humidity(25, None) is None

    # ── dew_point ──
    assert dew_point(25, 60) is not None
    assert dew_point(None, 60) is None

    # ── compute_delta_rh ──
    assert compute_delta_rh([], "2026-08-14T10:00:00") == (None, 0)
    assert compute_delta_rh([["2026-08-14T10:00:00", 60]], "2026-08-14T10:01:00") == (None, 1)
    assert compute_delta_rh([["2026-08-14T10:00:00", 60], ["2026-08-14T10:20:00", 55]], "2026-08-14T10:20:00") == (-5, 2)

    # ── sustained_above ──
    assert sustained_above({"temp_history": [["2026-08-14T10:00:00", 27.0], ["2026-08-14T10:10:00", 27.0], ["2026-08-14T10:15:00", 27.0]]}, 27, 10, now_ts="2026-08-14T10:20:00") is True


    # ── vent_gate_decision ──
    assert vent_gate_decision(15, 66, 26, 10, None, 19.0) is False
    assert vent_gate_decision(15, 66, 26, 10, None, 21.0) is False
    assert vent_gate_decision(15, 66, 26, 80, None, 19.0) is False
    assert vent_gate_decision(15, 66, 26, 10, None, None) is False

    # ── kWh 积分 ──
    st = {}
    update_kwh(st, "2026-08-14T22:00:00", 1100)
    assert st.get("estimated_kwh") == 0.0
    assert st["_prev_power"] == 1100
    update_kwh(st, "2026-08-14T22:10:00", 1100)
    assert abs(st["estimated_kwh"] - 0.1833) < 0.01
    update_kwh(st, "2026-08-14T22:20:00", 25)
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01
    update_kwh(st, "2026-08-14T22:30:00", None)
    assert st["_prev_power"] == 25
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01
    update_kwh(st, "2026-08-14T22:40:00", 50)
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01
    assert st["_prev_power"] == 50
    update_kwh(st, "2026-08-14T22:50:00", 45)
    assert abs(st["estimated_kwh"] - 0.2850) < 0.01
    update_kwh(st, "2026-08-14T23:30:00", None)
    update_kwh(st, "2026-08-14T23:35:00", 1100)
    assert abs(st["estimated_kwh"] - 0.2850) < 0.01
    update_kwh(st, "2026-08-14T23:37:00", 1100)
    assert abs(st["estimated_kwh"] - (0.2850 + 1100 * 2 / 60 / 1000)) < 0.01

    # ── 夜间模式 decide ──
    _future = datetime.now() + timedelta(hours=1)
    # Test: running, humidity above threshold, not time to stop yet
    assert decide(28, 65, True, 50, 90, False, "compressor", None, None, 26, 0, None, None, 13.5, 50) == (None, None, None)
    assert decide(25, 65, True, 30, 90, False, "compressor", None, None, 26, -1.0, None, None, 13.5, 30)[:2] != ("off", None)
    assert decide(29, 60, False, None, None, True, "off", None, None, 26, None, None, False, None, None)[:2] == ("cooling", 26)
    assert decide(26, 75, False, None, None, True, "off", None, None, 26, None, None, False, 18.0, None)[:2] == ("cooling", 24)
    assert decide(26, 65, False, None, None, True, "off", None, None, 26, None, None, False, 15.0, None)[:2] == (None, None)
    assert decide(24, 60, True, 30, 90, True, "compressor", None, None, 27, 0, 0, None, 13.5, None)[:2] == ("off", None)
    assert decide(27, 65, True, 50, 90, False, "compressor", None, None, 26, -2.0, None, None, None, None)[:2] == (None, None)

    # v8.10 假运行计数器测试
    for i in range(FAKE_RUN_MAX_CYCLES):
        r = decide(27, 75, True, 30, 90, False, "fan_only", 15, None, 26, 0, 0, None, None, None, None, fake_run_count=i)
        assert r[0] == "cooling", f"fake_run #{i} should restart, got {r}"
    r = decide(27, 75, True, 30, 90, False, "fan_only", 15, None, 26, 0, 0, None, None, None, None, fake_run_count=FAKE_RUN_MAX_CYCLES)
    assert r[0] == "off", f"fake_run #{FAKE_RUN_MAX_CYCLES} should stop, got {r}"

    # v8.16 白天双轴停止（AH+RH）
    assert decide(25, 60, True, 30, 90, False, "compressor", None, None, 26, None, None, None, 14.0, 15)[:2] == ("off", None)
    assert decide(25, 60, True, 30, 90, False, "compressor", None, None, 26, None, None, None, 15.0, 15)[:2] != ("off", None)

    # v8.18 晚间巡航豁免
    r = decide(25, 60, True, 100, 90, False, "compressor", None, None, 26, None, None, None, None, None, evening=True)
    assert r[0] != "off", f"evening cruise should not stop at 100min, got {r}"

    # v8.21 启停次数上限
    r = decide(27, 60, False, None, None, False, "off", None, None, 26, None, None, False, None, None, night_comp_starts=["2026-08-14T10:00:00"] * 2)
    assert r[0] is None, f"DAY_MAX_STARTS_PER_H=2 should block at 2 starts, got {r}"
    r = decide(30, 60, False, None, None, False, "off", None, None, 26, None, None, False, None, None, night_comp_starts=["2026-08-14T10:00:00"] * 2)
    assert r[0] == "cooling", f"DAY_STARTS_OVERRIDE_T=30 should force start, got {r}"

    # v8.23 持续判据
    r = decide(27, 60, False, None, None, False, "off", None, None, 26, None, None, False, None, None)
    assert r[0] is None, f"SUSTAIN_MIN=10 should block brief touch, got {r}"
    r = decide(30, 60, False, None, None, False, "off", None, None, 26, None, None, None, None, None, None, None, False, None, None, True, None)
    assert r[0] == "cooling", f"sustained should start, got {r}"

    # v8.24 稳态运行
    r = decide(25, 60, True, 50, 90, False, "compressor", None, None, 26, None, None, None, None, None, is_steady_state=True)
    assert r[0] is None, f"steady state should not stop, got {r}"

    # 热模型预测
    r = decide(25, 55, True, 50, 90, False, "compressor", None, None, 26, None, None, None, None, None, predicted_cool_min=3)
    assert r[0] == "off", f"predicted_cool_min=3 should early stop, got {r}"
    r = decide(25, 56, True, 50, 90, False, "compressor", None, None, 26, None, None, None, None, None, predicted_cool_min=10)
    assert r[0] is None, f"predicted_cool_min=10 should not early stop, got {r}"

    # 峰谷电测试
    r = decide(27, 60, False, None, None, False, "off", None, None, 26, None, None, False, None, None)
    assert r[0] is None, f"peak electricity should block 27°C (adj threshold), got {r}"
    r = decide(30, 60, False, None, None, False, "off", None, None, 26, None, None, False, None, None)
    assert r[0] == "cooling", f"peak electricity should allow 30°C, got {r}"

    # close_cycle 测试
    st_spike = {"estimated_kwh": 1.0, "cycle_start": {"ts": "2026-08-16T10:00:00", "ah": 16.0, "rh": 70, "kwh": 0.5},
                "rh_history": [["2026-08-16T10:00:00", 70], ["2026-08-16T10:02:00", 70],
                                    ["2026-08-16T10:04:00", 74], ["2026-08-16T10:06:00", 74]]}
    assert close_cycle(st_spike, "2026-08-16T10:06:00", 14.0, 74, 25, 6.0,
                       path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_cycle.tmp.jsonl"))
    import json as _json
    _rec = _json.loads(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_cycle.tmp.jsonl"),
                            encoding="utf-8").read().splitlines()[-1])
    assert _rec["rh_spike"] is True
    os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_cycle.tmp.jsonl"))

    print("ac_watch selftest: ALL PASS (v8.27)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        if not acquire_lock():
            print("ac_watch: 上一轮还在运行，跳过")
            sys.exit(0)
        main()
