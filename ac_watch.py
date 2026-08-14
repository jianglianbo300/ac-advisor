#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调自动监控 v8.5 — 每 10 分钟自动闭环（Hermes cron: */10 7-23 * * *）

v8.5 新增：
  1. 夜间模式：TTS 静音 + 控制继续（静默是通知层，不是控制层）
  2. kWh 积分：梯形积分(P_prev, P_now, Δt)
  3. 压缩机运行时间统计（基于 load_power，不是 is_on）

v8.4 继承：除湿效率反馈闭环（ΔRH/20min），4 档控制，阶梯 -1C
v8.3 继承：压缩机状态识别（load_power>300W=运行，5-50W=风扇）
"""
import os
import sys
import json
import math
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ac_advisor as A

WATCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_watch.log")
WATCH_MAX_RUN = 90      # 硬上限，防死锁

# ── v8.5 夜间模式：控制继续，只静音 TTS ──
QUIET_TTS = (23, 7)         # TTS 静音时段
NIGHT = (23, 7)             # 夜间节能模式时段
NIGHT_START_T = 28.0        # 夜间启动温度阈值
NIGHT_START_AH = 17.0       # 夜间启动绝对湿度阈值 (g/m3)
NIGHT_STOP_AH = 14.0        # 夜间停止绝对湿度阈值
NIGHT_STOP_AH_HYST = 1.0    # 夜间停止迟滞带（降到 14 停，升到 15 才重开）
NIGHT_STOP_T = 26.0         # 夜间停止温度阈值
NIGHT_TARGET = 26           # 夜间目标温度上限（=clamp(室温-2,24,26)，分支 A 夜间对齐）
NIGHT_MIN_TARGET = 24       # 夜间目标温度下限（防过冷）
NIGHT_MIN_COMP_ON = 20      # 夜间最小压缩机累计运行(min)
NIGHT_MAX_STARTS_PER_H = 4  # 每小时启动次数上限
NIGHT_EXTEND_RUN = 30       # 超限后单次运行延长(min)

# fallback：传感器不可达时的保守动作
SENSOR_FALLBACK_OFF_ALLOWED = True  # 读不到 → 允许关（安全动作）
SENSOR_FALLBACK_ON_ALLOWED = False  # 读不到 → 禁止开（危险动作）

# ── v8.3 压缩机状态识别层 ──
COMPRESSOR_POWER_THRESHOLD = 300   # 高于此值 = 压缩机在转
FAN_ONLY_POWER_MAX = 50            # 5~50W = 仅风扇，压缩机停
COMPRESSOR_FALSE_RUN_MIN = 10      # 压缩机停连续多久判定为"假运行"(min)
COMPRESSOR_RESTART_COOLDOWN = 30   # 压缩机重启后 30min 内不再次调温
COMPRESSOR_RESTART_DROP = 2        # 假运行时降 2C 重启压缩机

# ── v8.4 除湿效率参数 ──
DEHUMID_EXIT_RH = 60               # 湿度达标退出线
DEHUMID_DRYING_HOLD = 15           # 达标后持续确认分钟数
DEHUMID_LOW_EFF_RH = 66            # 低效检测湿度阈值
DEHUMID_DELTA_RH_MIN = -1.0        # ΔRH/20min <= -1% = 有效除湿
DEHUMID_OBSERVE_MIN = 20           # 每级降温后观察窗口(min)
DEHUMID_ADJUST_COOLDOWN = 20       # 调温后冷却锁(min)，防每 10min 连续降
DEHUMID_FORCE_MIN = 40             # 强制除湿所需连续运行(min)
DEHUMID_FORCE_RH = 68              # 强制除湿湿度阈值
DEHUMID_STALL_MIN = 60             # 判定无效的连续运行(min)
DEHUMID_STALL_RH_BAND = 1.5        # 60min 内 RH 波动 < 1.5% = 无效
DEHUMID_STEP_C = 1                 # 每级降 1C（不是 2C）
DEHUMID_MIN_TARGET = 16            # 最低目标温度


def log(msg):
    try:
        with open(WATCH_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def tts_mute(now=None):
    """23-7 TTS 静音（通知层静默，不影响控制层）。"""
    h = (now or datetime.now()).hour
    return h >= QUIET_TTS[0] or h < QUIET_TTS[1]


def night_hours(now=None):
    """23-7 夜间节能模式时段。"""
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
    window_start = None
    for ts_str, rh_val in rh_history:
        ts = datetime.fromisoformat(ts_str)
        if ts >= cutoff:
            window_start = (ts, rh_val)
            break
    if window_start is None:
        return None, len(rh_history)
    latest = rh_history[-1][1]
    delta = latest - window_start[1]
    return round(delta, 1), len(rh_history)


def update_rh_history(state, now_ts, rh):
    hist = state.get("rh_history") or []
    hist.append([now_ts, rh])
    if len(hist) > 12:
        hist = hist[-12:]
    state["rh_history"] = hist


def update_kwh(state, now_ts, load_power):
    """梯形积分更新 estimated_kWh。
    每次 tick 调用一次，用当前 load_power 与上一 tick 的功率做梯形积分。"""
    prev_power = state.get("_prev_power")
    prev_ts = state.get("_prev_kwh_ts")
    kwh = state.get("estimated_kwh", 0.0)
    if prev_power is not None and prev_ts is not None and load_power is not None:
        try:
            dt_hours = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(prev_ts)).total_seconds() / 3600
            if dt_hours > 0 and dt_hours < 1:  # 防止异常间隔
                avg_power = (prev_power + load_power) / 2.0
                kwh += avg_power * dt_hours / 1000.0
        except Exception:
            pass
    state["estimated_kwh"] = round(kwh, 4)
    if load_power is not None:
        state["_prev_power"] = load_power
        state["_prev_kwh_ts"] = now_ts


def stale_stop_ts(old_ts, run_start_ts):
    """旧 compressor_stop 时间戳是否早于本次 run_start（跨周期遗留视为无效）。"""
    if not old_ts:
        return True
    try:
        return bool(run_start_ts) and old_ts < run_start_ts
    except Exception:
        return False


def open_cycle(state, now_ts, ah, rh):
    """开机动作(apply+verify 通过)时记录周期开始快照。纯数据层，不影响决策。"""
    state["cycle_start"] = {
        "ts": now_ts,
        "ah": ah,
        "rh": rh,
        "kwh": state.get("estimated_kwh", 0.0) or 0.0,
    }


def close_cycle(state, now_ts, ah, rh, target_temp, comp_min, path=None):
    """关机动作(apply+verify 通过)时追加一条完整周期记录到 cycle_log.jsonl。
    无 cycle_start（异常/失败路径）→ 不写假周期。append-only，幂等。"""
    cs = state.get("cycle_start")
    if not cs:
        return False
    end_kwh = state.get("estimated_kwh", 0.0) or 0.0
    try:
        dur_min = round((datetime.fromisoformat(now_ts) - datetime.fromisoformat(cs["ts"])).total_seconds() / 60.0, 1)
    except Exception:
        dur_min = None
    rec = {
        "start_ts": cs["ts"],
        "end_ts": now_ts,
        "start_AH": cs.get("ah"),
        "end_AH": ah,
        "start_RH": cs.get("rh"),
        "end_RH": rh,
        "target_temp": target_temp,
        "compressor_runtime_min": round(comp_min, 1) if comp_min is not None else None,
        "kwh_used": round(max(0.0, end_kwh - cs.get("kwh", 0.0)), 4),
        "duration_min": dur_min,
    }
    path = path or os.path.join(os.path.dirname(os.path.realpath(__file__)), "cycle_log.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state.pop("cycle_start", None)
    return True


def decide(temp, hum, running, since_on, since_off, is_night,
           compressor=None, last_compressor_stop_at=None, cooldown_until_dt=None,
           current_target=26, delta_rh_20min=None, delta_rh_60min=None,
           minutes_since_last_adjust=None, ah=None, compressor_run_min=None):
    """纯决策函数。返回 (new_mode, target_temp) 或 (None, None)。

    v8.6 核心改进：所有"已运行多久"判断改用 compressor_run_min（压缩机实际累计运行分钟），
    不用 since_on（壁钟时间）——定频机到温停压缩机，since_on 包含大量风扇空吹时间。
    失败/低效判断只基于真实除湿工作时间，避免"看起来跑了很久实际没干活"的误判。
    """
    if running is None:
        # 传感器不可达：放弃本次决策，不动作
        return (None, None)
    if running:
        if compressor == "fan_only":
            # ── 假运行（v8.3） ──
            stop_duration = None
            if last_compressor_stop_at is not None and since_on is not None:
                stop_duration = since_on - last_compressor_stop_at
                if stop_duration < 0:
                    stop_duration = None
            in_cooldown = False
            if cooldown_until_dt is not None:
                in_cooldown = datetime.now() < cooldown_until_dt
            if (hum > DEHUMID_LOW_EFF_RH
                    and stop_duration is not None
                    and stop_duration >= COMPRESSOR_FALSE_RUN_MIN
                    and not in_cooldown):
                new_target = max(DEHUMID_MIN_TARGET, current_target - COMPRESSOR_RESTART_DROP)
                return ("cooling", new_target)
            if hum <= DEHUMID_EXIT_RH:
                return ("off", None)
            return (None, None)

        # ── 压缩机运行中 ──
        # 用压缩机实际累计运行时间（不是壁钟时间）
        comp_min = compressor_run_min if compressor_run_min is not None else since_on

        # 硬上限：压缩机累计运行超时，无论湿度如何都停（保护压缩机）
        if comp_min is not None and comp_min >= WATCH_MAX_RUN:
            return ("off", None)

        # 夜间停止条件
        if is_night:
            if ah is not None and ah <= NIGHT_STOP_AH:
                return ("off", None)
            if temp <= NIGHT_STOP_T and (ah is None or ah <= NIGHT_STOP_AH + 2):
                return ("off", None)

        # 湿度达标
        if hum <= DEHUMID_EXIT_RH:
            return ("off", None)

        # 无效（Tier 4）：压缩机实际跑了 60min 以上 RH 不动 → 立即关掉（不空耗）
        if (comp_min is not None and comp_min >= DEHUMID_STALL_MIN
                and delta_rh_60min is not None and abs(delta_rh_60min) < DEHUMID_STALL_RH_BAND):
            return ("off", None)

        # 正常除湿（Tier 1）
        if delta_rh_20min is not None and delta_rh_20min <= DEHUMID_DELTA_RH_MIN:
            return (None, None)

        # 低效（Tier 2）
        if hum > DEHUMID_LOW_EFF_RH and delta_rh_20min is not None and delta_rh_20min > DEHUMID_DELTA_RH_MIN:
            if minutes_since_last_adjust is not None and minutes_since_last_adjust < DEHUMID_ADJUST_COOLDOWN:
                return (None, None)
            if comp_min is not None and comp_min >= DEHUMID_FORCE_MIN and hum > DEHUMID_FORCE_RH:
                new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
                return ("cooling", new_target)
            new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
            return ("cooling", new_target)

        return (None, None)

    # ── 未运行 ──
    if since_off is not None and since_off < A.MIN_OFF:
        return (None, None)
    if is_night:
        # 分支 A 夜间对齐：目标永远低于室温 2C（保证定频压缩机启动），clamp 24~26
        night_target = max(NIGHT_MIN_TARGET, min(NIGHT_TARGET, round(temp - 2)))
        if temp >= NIGHT_START_T:
            return ("cooling", night_target)
        if ah is not None and ah >= NIGHT_START_AH + NIGHT_STOP_AH_HYST:
            return ("cooling", night_target)
        return (None, None)
    if temp >= A.TEMP_COOLING:
        return ("cooling", round(max(26, min(28, temp - 2))))
    if temp >= A.TEMP_DEHUMID_LOW and hum >= A.HUM_DEHUMID_ON:
        return ("cooling", 24)
    return (None, None)


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
    if temp is None or hum is None:
        log(f"传感器不可达跳过 socket={socket}")
        print("ac_watch: 室内传感器不可达，本次跳过")
        return

    state = A.load_state()
    A.reconcile_state(state, now_ts)

    # socket=None（传感器不可达）时，不信任 state.mode 冒充真实状态
    # load_power 才是实际执行状态；读不到就进入降级态而非假设运行
    if socket is None:
        running = None  # 未知 → decide 返回 None，本次不动作（避免误控）
    else:
        running = socket == "on" or state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    since_on = A.minutes_since(state.get("run_start"))
    since_off = A.minutes_since(state.get("last_off_at"))

    # ── 压缩机状态识别（v8.3） ──
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
    if comp == "compressor" and state_comp_before != "compressor":
        state["compressor_restart_cooldown_until"] = (
            now_dt + timedelta(minutes=COMPRESSOR_RESTART_COOLDOWN)
        ).isoformat(timespec="seconds")
        state["last_compressor_start_at"] = now_ts
    elif comp == "fan_only" and state_comp_before == "compressor":
        state["last_compressor_stop_at"] = now_ts
        last_comp_stop = now_ts
    elif comp == "fan_only" and state_comp_before != "compressor" and stale_stop_ts(
            state.get("last_compressor_stop_at"), state.get("run_start")):
        state["last_compressor_stop_at"] = now_ts
        last_comp_stop = now_ts
    state["compressor_state"] = comp

    # ── 压缩机连续运行时间（基于真实时间差，不是固定 +10/tick）──
    comp_on_min = state.get("compressor_on_min", 0) or 0
    if comp == "compressor":
        comp_since = state.get("compressor_on_since")
        if comp_since:
            try:
                elapsed = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(comp_since)).total_seconds() / 60
                comp_on_min = round(max(0, elapsed), 1)
            except:
                comp_on_min += 10
        else:
            state["compressor_on_since"] = now_ts
            comp_on_min = 0
    else:
        comp_on_min = 0
        state.pop("compressor_on_since", None)
    state["compressor_on_min"] = comp_on_min

    # ── v8.5 kWh 梯形积分 ──
    update_kwh(state, now_ts, load_power)

    # ── v8.4 RH 历史 ──
    update_rh_history(state, now_ts, hum)
    delta_rh_20, _ = compute_delta_rh(state.get("rh_history"), now_ts, 20)
    delta_rh_60, _ = compute_delta_rh(state.get("rh_history"), now_ts, 60)

    current_target = state.get("target_temp", 26) or 26
    dp = dew_point(temp, hum)
    ah = absolute_humidity(temp, hum)

    # ── v8.5 夜间模式判断 ──
    is_night = night_hours()

    # 调温冷却锁
    last_adjust = state.get("last_dehumid_adjust_at")
    minutes_since_adjust = A.minutes_since(last_adjust) if last_adjust else None

    # 假运行停运时长：将 last_comp_stop 时间戳转为距 run_start 的分钟数
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

    new_mode, target = decide(temp, hum, running, since_on, since_off, is_night,
                              compressor=comp,
                              last_compressor_stop_at=stop_duration,
                              cooldown_until_dt=cooldown,
                              current_target=current_target,
                              delta_rh_20min=delta_rh_20,
                              delta_rh_60min=delta_rh_60,
                              minutes_since_last_adjust=minutes_since_adjust,
                              ah=ah,
                              compressor_run_min=state.get("compressor_on_min"))

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
        # 无动作时也落盘 kWh 和 RH 历史（P2：这些是 watcher 自管字段，不影响控制状态）
        A.save_state(state)
        return

    if dry:
        print(f"ac_watch [dry]: 将执行 {new_mode} target={target} · {meta}")
        log(f"[dry] 将执行 {new_mode} target={target} · {meta}")
        return

    # meta 传递
    extra_meta = None
    if new_mode == "cooling" and target is not None and target < current_target:
        extra_meta = {"last_dehumid_adjust_at": now_ts}

    mode_before = state.get("mode")
    running_target = current_target
    comp_min_at_apply = state.get("compressor_on_min") or 0
    ctrl = A.apply_and_commit(new_mode, target, state, now_ts, meta=extra_meta)
    log(f"执行 {new_mode} target={target} → {ctrl['status']} {ctrl.get('action','')} {ctrl.get('reason','')} · {cl} {meta}")
    if ctrl["status"] == "action":
        # ── v8.6 cycle log：只在真实开关动作(apply+verify 通过)时记录，失败路径→action 不成立不写 ──
        if new_mode == "cooling" and mode_before != "cooling":
            open_cycle(state, now_ts, ah, rh)
            A.save_state(state)
        elif new_mode == "off" and mode_before in ("cooling", "dehumid", "dehumid_alert"):
            close_cycle(state, now_ts, ah, rh, running_target, comp_min_at_apply)
            A.save_state(state)
        print(f"ac_watch: 已自动{ctrl['action']} · {meta}")
        if not is_night:
            try:
                import xiaomi_tts
                xiaomi_tts.speak(f"空调已自动{ctrl['action']}")
            except Exception as e:
                log(f"TTS 播报失败（不影响控制）: {e}")
    elif ctrl["status"] == "no_action":
        print(f"ac_watch: 已处目标状态无需动作 · {meta}")
    else:
        print(f"ac_watch: 自动控制失败（{ctrl['reason']}）——建议人工确认空调状态")


def _selftest():
    # ── compressor_state ──
    comp_cases = [
        (None, "unknown"), (0, "off"), (3, "off"),
        (10, "fan_only"), (25, "fan_only"), (50, "fan_only"),
        (100, "unknown"), (250, "unknown"),
        (301, "compressor"), (500, "compressor"), (1100, "compressor"),
    ]
    for pw, exp in comp_cases:
        assert compressor_state(pw) == exp, f"compressor_state({pw}) = {compressor_state(pw)}"

    # ── 露点/AH ──
    dp = dew_point(23, 70)
    ah = absolute_humidity(23, 70)
    assert dp is not None and 16 < dp < 18, f"dew_point={dp}"
    assert ah is not None and 12 < ah < 16, f"AH={ah}"

    # ── ΔRH ──
    from datetime import timedelta
    base = datetime.now()
    hist = [
        [(base - timedelta(minutes=25)).isoformat(), 72.0],
        [(base - timedelta(minutes=20)).isoformat(), 71.0],
        [(base - timedelta(minutes=10)).isoformat(), 70.0],
        [(base - timedelta(minutes=5)).isoformat(), 69.0],
    ]
    d20, n20 = compute_delta_rh(hist, base.isoformat(), 20)
    assert n20 == 4, f"rh_count={n20}"
    assert d20 is not None and abs(d20 - (-2.0)) < 0.1, f"delta_rh_20={d20}"

    d, n = compute_delta_rh([(base.isoformat(), 70)], base.isoformat(), 20)
    assert d is None and n == 1

    # ── kWh 积分 ──
    st = {}
    update_kwh(st, "2026-08-14T22:00:00", 1100)
    assert st.get("estimated_kwh") == 0.0, f"first tick kwh={st['estimated_kwh']}"
    assert st["_prev_power"] == 1100
    update_kwh(st, "2026-08-14T22:10:00", 1100)
    # 1100W * 10min = 1100 * (10/60) / 1000 = 0.1833 kWh
    assert abs(st["estimated_kwh"] - 0.1833) < 0.01, f"kwh_10min={st['estimated_kwh']}"
    update_kwh(st, "2026-08-14T22:20:00", 25)
    # 梯形: (1100+25)/2 * 10min / 1000 = 0.09375
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01, f"kwh_20min={st['estimated_kwh']}"

    # ── kWh 积分: sensor unknown 不覆盖锚点（v8.6 fix） ──
    update_kwh(st, "2026-08-14T22:30:00", None)  # unknown: 不积分、锚点保留
    assert st["_prev_power"] == 25, f"unknown_prev={st['_prev_power']}"
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01, f"unknown_kwh={st['estimated_kwh']}"
    update_kwh(st, "2026-08-14T22:40:00", 50)  # 恢复: 用旧锚点(25,22:20)梯形积分空窗
    # (25+50)/2 * 20min / 1000 = 0.0125 → 0.2771+0.0125=0.2896
    assert abs(st["estimated_kwh"] - 0.2896) < 0.01, f"recover_kwh={st['estimated_kwh']}"
    assert st["_prev_power"] == 50  # 恢复后重新建立锚点
    update_kwh(st, "2026-08-14T22:50:00", 45)
    # (50+45)/2 * 10min / 1000 = 0.007917 → 0.2896+0.0079=0.2975
    assert abs(st["estimated_kwh"] - 0.2975) < 0.01, f"after_recover_kwh={st['estimated_kwh']}"

    # ── 夜间模式 decide ──
    _future = datetime.now() + timedelta(hours=1)
    # 夜间：T>=28 → 启动 室温-2=27 → clamp 上限 26
    assert decide(29, 60, False, None, None, True, "off", None, None, 26, None, None, False, None, None) == ("cooling", 26)
    # 夜间：AH>=18（17+1 迟滞，室温 26）→ 启动 26-2=24
    assert decide(26, 75, False, None, None, True, "off", None, None, 26, None, None, False, 18.0, None) == ("cooling", 24)
    # 夜间：条件不满足 → 不动
    assert decide(26, 65, False, None, None, True, "off", None, None, 26, None, None, False, 15.0, None) == (None, None)
    # 夜间运行中：AH<=14 → 关
    assert decide(24, 60, True, 30, 90, True, "compressor", None, None, 27, 0, 0, None, 13.5, None) == ("off", None)
    # 白天运行中：原逻辑
    assert decide(27, 65, True, 50, 90, False, "compressor", None, None, 26, -2.0, None, None, None, None) == (None, None)
    assert decide(27, 68, True, 50, 90, False, "compressor", None, None, 26, 0, 0, None, None, None) == ("cooling", 25)

    # ── 原 v8.4 decide 测试（带 is_night=False） ──
    cases = [
        ((27.0, 65, True, 30, 90, False, "compressor", None, None, 26, None, None, None, None, None), (None, None)),
        ((27.0, 68, True, 50, 90, False, "compressor", None, None, 26, None, None, None, None, None), (None, None)),
        ((27.0, 60, True, 50, 90, False, "compressor", None, None, 26, None, None, None, None, None), ("off", None)),
        ((27.0, 65, True, 95, 90, False, "compressor", None, None, 26, None, None, None, None, None), ("off", None)),
        ((29.0, 60, False, None, None, False, "off", None, None, 26, None, None, None, None, None), ("cooling", 27)),
        ((27.0, 72, False, None, None, False, "off", None, None, 26, None, None, None, None, None), ("cooling", 24)),
        ((27.0, 65, False, None, None, False, "off", None, None, 26, None, None, None, None, None), (None, None)),
        ((25.0, 72, False, None, None, False, "off", None, None, 26, None, None, None, None, None), (None, None)),
    ]
    for args, exp in cases:
        got = decide(*args)
        assert got == exp, f"decide{args} = {got}"

    # ── 假运行 ──
    _past = datetime.now() - timedelta(minutes=31)
    assert decide(27, 68, True, 20, 90, False, "fan_only", 5, None, 26, None, None, None, None, None) == ("cooling", 24)
    assert decide(27, 68, True, 20, 90, False, "fan_only", 5, _future, 26, None, None, None, None, None) == (None, None)
    assert decide(27, 58, True, 20, 90, False, "fan_only", 5, None, 26, None, None, None, None, None) == ("off", None)

    # ── apply_and_commit ──
    saved = []
    A.save_state = lambda s: saved.append(dict(s))
    called = []

    def fake_apply(m, t, status, action=""):
        called.append((m, t))
        return {"status": status, "action": action, "reason": ""}

    TS = "2026-08-14T15:00:00"
    A.ac_apply = lambda m, t: fake_apply(m, t, "action", "开")
    A.verify_socket = lambda: "on"
    saved.clear(); called.clear()
    st = {"mode": "off", "run_start": None}
    r = A.apply_and_commit("cooling", 24, st, TS)
    assert r["status"] == "action" and st["mode"] == "cooling" and st["run_start"] == TS

    st2 = {"mode": "cooling", "run_start": "2026-08-14T12:00:00"}
    A.apply_and_commit("cooling", 24, st2, TS)
    assert st2["run_start"] == "2026-08-14T12:00:00"

    A.ac_apply = lambda m, t: fake_apply(m, t, "action", "关")
    A.verify_socket = lambda: "off"
    saved.clear(); called.clear()
    st = {"mode": "cooling", "run_start": "2026-08-14T12:00:00", "last_off_at": None}
    r = A.apply_and_commit("off", None, st, TS)
    assert r["status"] == "action" and st["mode"] == "off" and st["last_off_at"] == TS

    A.ac_apply = lambda m, t: fake_apply(m, t, "failed")
    A.verify_socket = lambda: None
    saved.clear(); called.clear()
    st = {"mode": "off", "run_start": None}
    r = A.apply_and_commit("cooling", 24, st, TS)
    assert r["status"] == "failed" and st == {"mode": "off", "run_start": None}

    # ── stale_stop_ts（跨周期旧时间戳判定） ──
    assert stale_stop_ts(None, "2026-08-15T01:24:24") is True
    assert stale_stop_ts("2026-08-14T23:30:22", "2026-08-15T01:24:24") is True
    assert stale_stop_ts("2026-08-15T01:30:34", "2026-08-15T01:24:24") is False
    assert stale_stop_ts("2026-08-15T01:24:24", "2026-08-15T01:24:24") is False

    # ── cycle log（v8.6 数据层） ──
    import tempfile
    _tmp_cycle = os.path.join(tempfile.mkdtemp(), "cycle_log.jsonl")
    st_c = {}
    # 1) 未开机直接关 → 不写（异常/失败路径不产生假周期）
    assert close_cycle(st_c, "2026-08-15T02:00:00", 14.0, 60, 25, 20.0, path=_tmp_cycle) is False
    assert not os.path.exists(_tmp_cycle), "无 cycle_start 不应写文件"
    # 2) 正常开→关 → 写一条完整周期
    open_cycle(st_c, "2026-08-15T01:24:24", 18.3, 71)
    st_c["estimated_kwh"] = 0.0922
    assert close_cycle(st_c, "2026-08-15T02:00:00", 14.1, 60, 25, 20.5, path=_tmp_cycle) is True
    with open(_tmp_cycle, encoding="utf-8") as f:
        lines = f.read().strip().splitlines()
    assert len(lines) == 1, f"cycles={len(lines)}"
    r = json.loads(lines[0])
    assert r["start_AH"] == 18.3 and r["end_AH"] == 14.1
    assert r["start_RH"] == 71 and r["end_RH"] == 60
    assert r["target_temp"] == 25 and r["compressor_runtime_min"] == 20.5
    assert abs(r["duration_min"] - 35.6) < 0.1, f"dur={r['duration_min']}"  # 01:24:24→02:00:00
    assert abs(r["kwh_used"] - 0.0922) < 1e-4
    # 3) 重复关（周期已清）→ 不写第二条（幂等）
    assert close_cycle(st_c, "2026-08-15T02:10:00", 14.0, 59, 25, 20.5, path=_tmp_cycle) is False
    with open(_tmp_cycle, encoding="utf-8") as f:
        assert len(f.read().strip().splitlines()) == 1
    os.remove(_tmp_cycle)

    total = len(comp_cases) + 11 + 8 + 3
    print(f"selftest OK: {total} decide + 9 apply_and_commit 状态路径")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()