#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调自动监控 v8.4 — 每 10 分钟自动闭环（Hermes cron: */10 7-23 * * *）
感知 → 判断 → 执行 → 验证 → 提交。复用 ac_advisor 的 v8.1 统一控制接口。

v8.4 新增：除湿效率反馈闭环（ΔRH/20min）
  - 4 档除湿控制：正常 / 低效(-1C) / 强制(-1C再) / 无效(停止)
  - 阶梯 -1C 代替 -2C，每次降后观察 20~30min
  - 露点/绝对湿度日志输出，辅助判断
  - 保留 v8.3 压缩机状态识别层

阈值:
  启动: T>=28C OR (T>=26C AND RH>=70%)
  停止(正常除湿): RH<=60%；硬上限 MAX_RUN=90min
  低效: 压缩机运行 + RH>66% + ΔRH/20min > -1% → -1C
  强制: 连续40min + RH>68% + ΔRH < -1% → 再 -1C
  无效: 连续60min + RH几乎不降 → 停止降温
  保护: 关后 MIN_OFF(30)min 不重开；夜间(23-7)不启动
"""
import os
import sys
import math
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ac_advisor as A

WATCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_watch.log")
WATCH_MAX_RUN = 90      # 硬上限，防死锁
QUIET = (23, 7)         # 夜间不自动启动

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


def quiet(now=None):
    h = (now or datetime.now()).hour
    return h >= QUIET[0] or h < QUIET[1]


def dew_point(temp_c, rh):
    """计算露点温度（Magnus 公式）。"""
    if temp_c is None or rh is None:
        return None
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    return (b * gamma) / (a - gamma)


def absolute_humidity(temp_c, rh):
    """计算绝对湿度 (g/m3)。"""
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
    """从 RH 历史记录计算 ΔRH/{window_min}min。
    rh_history: [(ts_iso, rh), ...]，按时间升序。
    返回 (delta_rh, valid_count) 或 (None, 0)。"""
    if not rh_history or len(rh_history) < 2:
        return None, len(rh_history) if rh_history else 0
    now = datetime.fromisoformat(now_ts)
    cutoff = now - timedelta(minutes=window_min)
    # 找窗口起点附近最早的读数
    window_start = None
    for ts_str, rh_val in rh_history:
        ts = datetime.fromisoformat(ts_str)
        if ts >= cutoff:
            window_start = (ts, rh_val)
            break
    if window_start is None:
        return None, len(rh_history)
    # 当前 RH = 最新一条
    latest = rh_history[-1][1]
    delta = latest - window_start[1]
    return round(delta, 1), len(rh_history)


def update_rh_history(state, now_ts, rh):
    """往 state 的 RH 历史环追加当前读数，保留最近 12 条（2h）。"""
    hist = state.get("rh_history") or []
    hist.append([now_ts, rh])
    # 只保留最近 12 条（每 10min 一次 = 2h 窗口）
    if len(hist) > 12:
        hist = hist[-12:]
    state["rh_history"] = hist


def decide(temp, hum, running, since_on, since_off, is_quiet,
           compressor=None, last_compressor_stop_at=None, cooldown_until_dt=None,
           current_target=26, delta_rh_20min=None, delta_rh_60min=None,
           minutes_since_last_adjust=None):
    """纯决策函数。返回 (new_mode, target_temp) 或 (None, None)。

    v8.4 除湿效率反馈闭环：
      compressor 运行中 → 按 ΔRH 分 4 档
      fan_only → 假运行逻辑（v8.3）
      未运行 → 启动逻辑
    """
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

        # ── 压缩机运行中：v8.4 除湿效率反馈 ──
        # 硬上限保护
        if since_on is not None and since_on >= WATCH_MAX_RUN:
            return ("off", None)

        # 湿度达标 → 关
        if hum <= DEHUMID_EXIT_RH:
            return ("off", None)

        # 无效（Tier 4）：连续 60min RH 几乎不动 → 停止降温
        # 优先级高于低效/强制，因为继续降只会更冷而无除湿效果
        if (since_on is not None and since_on >= DEHUMID_STALL_MIN
                and delta_rh_60min is not None and abs(delta_rh_60min) < DEHUMID_STALL_RH_BAND):
            return (None, None)

        # 正常除湿（Tier 1）：ΔRH 下降正常
        if delta_rh_20min is not None and delta_rh_20min <= DEHUMID_DELTA_RH_MIN:
            return (None, None)

        # 低效（Tier 2）：RH>66% 且 ΔRH 下降不足
        # 先检查调温冷却锁：距离上次调整 <20min 则不动
        if hum > DEHUMID_LOW_EFF_RH and delta_rh_20min is not None and delta_rh_20min > DEHUMID_DELTA_RH_MIN:
            if minutes_since_last_adjust is not None and minutes_since_last_adjust < DEHUMID_ADJUST_COOLDOWN:
                # 仍在冷却期内，不降
                return (None, None)
            if since_on is not None and since_on >= DEHUMID_FORCE_MIN and hum > DEHUMID_FORCE_RH:
                # 强制除湿（Tier 3）：连续跑了 40min 以上仍高
                new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
                return ("cooling", new_target)
            # 低效（Tier 2）：-1C
            new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
            return ("cooling", new_target)

        # 湿度不低、ΔRH 数据不足 → 继续等
        return (None, None)

    # ── 未运行 ──
    if is_quiet:
        return (None, None)
    if since_off is not None and since_off < A.MIN_OFF:
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
    elif comp == "fan_only" and state_comp_before != "compressor" and not state.get("last_compressor_stop_at"):
        state["last_compressor_stop_at"] = now_ts
        last_comp_stop = now_ts
    state["compressor_state"] = comp

    # ── v8.4 RH 历史记录 + ΔRH 计算 ──
    update_rh_history(state, now_ts, hum)
    delta_rh_20, rh_count_20 = compute_delta_rh(state.get("rh_history"), now_ts, 20)
    delta_rh_60, rh_count_60 = compute_delta_rh(state.get("rh_history"), now_ts, 60)

    # 当前目标温度
    current_target = state.get("target_temp", 26) or 26

    # 露点 / 绝对湿度（日志用）
    dp = dew_point(temp, hum)
    ah = absolute_humidity(temp, hum)

    # 调温冷却锁：距离上次降温度过了多久
    last_adjust = state.get("last_dehumid_adjust_at")
    minutes_since_adjust = A.minutes_since(last_adjust) if last_adjust else None

    new_mode, target = decide(temp, hum, running, since_on, since_off, quiet(),
                              compressor=comp,
                              last_compressor_stop_at=last_comp_stop,
                              cooldown_until_dt=cooldown,
                              current_target=current_target,
                              delta_rh_20min=delta_rh_20,
                              delta_rh_60min=delta_rh_60,
                              minutes_since_last_adjust=minutes_since_adjust)

    COMP_LABEL = {"compressor": "压缩机运行", "fan_only": "仅风扇",
                  "off": "已关机", "unknown": "未知"}
    cl = COMP_LABEL.get(comp, comp)

    # 决策日志（含露点/ΔRH）
    delta_rh_str = f"dRH20={delta_rh_20}% dRH60={delta_rh_60}%"
    dp_str = f"dp={dp:.1f}C" if dp is not None else "dp=?"
    ah_str = f"AH={ah:.1f}g/m3" if ah is not None else "AH=?"
    meta = f"T={temp} RH={hum}% {dp_str} {ah_str} {delta_rh_str} target={current_target}C"

    if new_mode is None:
        log(f"无动作 {cl} {meta} 已开={since_on} 已关={since_off} mode={state.get('mode')}")
        print(f"ac_watch: 无需动作 · {cl} · {meta}")
        return

    if dry:
        print(f"ac_watch [dry]: 将执行 {new_mode} target={target} · {meta}")
        log(f"[dry] 将执行 {new_mode} target={target} · {meta}")
        return

    # meta：控制成功时额外落盘的字段（由 apply_and_commit 内部 merge，不破坏 P2 所有权）
    extra_meta = None
    if new_mode == "cooling" and target is not None and target < current_target:
        extra_meta = {"last_dehumid_adjust_at": now_ts}

    ctrl = A.apply_and_commit(new_mode, target, state, now_ts, meta=extra_meta)
    log(f"执行 {new_mode} target={target} → {ctrl['status']} {ctrl.get('action','')} {ctrl.get('reason','')} · {cl} {meta}")
    if ctrl["status"] == "action":
        print(f"ac_watch: 已自动{ctrl['action']} · {meta}")
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
        assert compressor_state(pw) == exp, f"compressor_state({pw}) = {compressor_state(pw)}，期望 {exp}"

    # ── 露点/绝对湿度 ──
    # 23C/70%RH → 露点约 17.3C, AH 约 12.9g/m3
    dp = dew_point(23, 70)
    ah = absolute_humidity(23, 70)
    assert dp is not None and 16 < dp < 18, f"dew_point={dp}"
    assert ah is not None and 12 < ah < 16, f"AH={ah}"

    # ── ΔRH 计算 ──
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
    assert d20 is not None, "delta_rh is None"
    # 69 - 71 = -2 (reading at -20min = 71, latest = 69)
    assert abs(d20 - (-2.0)) < 0.1, f"delta_rh_20={d20} 期望 -2.0"

    # 不足 2 条
    d, n = compute_delta_rh([(base.isoformat(), 70)], base.isoformat(), 20)
    assert d is None and n == 1, f"single entry: d={d} n={n}"

    # ── decide v8.4 除湿效率 ──
    # 正常除湿（Tier 1）：ΔRH=-2%/20min, 有效
    assert decide(27, 68, True, 30, 90, False, "compressor", None, None, 26, -2.0, None) == (None, None)
    # 低效（Tier 2）：RH>66%, ΔRH=0, 未达强制阈值 → -1C
    assert decide(27, 68, True, 30, 90, False, "compressor", None, None, 26, 0.0, None) == ("cooling", 25)
    # 强制（Tier 3）：已跑40min+, RH>68%, ΔRH=0 → -1C
    assert decide(27, 69, True, 45, 90, False, "compressor", None, None, 26, 0.0, None) == ("cooling", 25)
    # 达标关
    assert decide(27, 60, True, 30, 90, False, "compressor", None, None, 26, 0.0, None) == ("off", None)
    # 硬上限关
    assert decide(27, 68, True, 95, 90, False, "compressor", None, None, 26, 0.0, None) == ("off", None)
    # 无效（Tier 4）：60min RH 波动 <1.5%
    assert decide(27, 69, True, 65, 90, False, "compressor", None, None, 26, -0.5, 0.5) == (None, None)
    # ΔRH 数据不足 → 继续
    assert decide(27, 68, True, 30, 90, False, "compressor", None, None, 26, None, None) == (None, None)
    # 假运行
    _past = datetime.now() - timedelta(minutes=31)
    assert decide(27, 68, True, 20, 90, False, "fan_only", 5, None, 26, None, None) == ("cooling", 24)
    # 假运行 - 冷却期内
    _future = datetime.now() + timedelta(hours=1)
    assert decide(27, 68, True, 20, 90, False, "fan_only", 5, _future, 26, None, None) == (None, None)
    # 未运行
    assert decide(29, 60, False, None, None, False, "off", None, None, 26, None, None) == ("cooling", 27)

    # ── apply_and_commit 状态所有权测试 ──
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
    assert r["status"] == "action" and st["mode"] == "cooling" and st["run_start"] == TS and st["last_on_at"] == TS, (r, st)

    st2 = {"mode": "cooling", "run_start": "2026-08-14T12:00:00", "last_on_at": "2026-08-14T12:00:00"}
    A.apply_and_commit("cooling", 24, st2, TS)
    assert st2["run_start"] == "2026-08-14T12:00:00" and st2["last_on_at"] == "2026-08-14T12:00:00", st2

    A.ac_apply = lambda m, t: fake_apply(m, t, "action", "关")
    A.verify_socket = lambda: "off"
    saved.clear(); called.clear()
    st = {"mode": "cooling", "run_start": "2026-08-14T12:00:00", "last_off_at": None}
    r = A.apply_and_commit("off", None, st, TS)
    assert r["status"] == "action" and st["mode"] == "off" and st["last_off_at"] == TS and st["run_start"] is None, (r, st)

    st3 = {"mode": "off", "last_off_at": "2026-08-14T13:00:00"}
    A.apply_and_commit("off", None, st3, TS)
    assert st3["last_off_at"] == "2026-08-14T13:00:00", st3

    A.ac_apply = lambda m, t: fake_apply(m, t, "failed")
    A.verify_socket = lambda: None
    saved.clear(); called.clear()
    st = {"mode": "off", "run_start": None}
    r = A.apply_and_commit("cooling", 24, st, TS)
    assert r["status"] == "failed" and st == {"mode": "off", "run_start": None}, (r, st)

    A.ac_apply = lambda m, t: fake_apply(m, t, "action", "开")
    A.verify_socket = lambda: "off"
    saved.clear(); called.clear()
    st = {"mode": "off", "run_start": None}
    r = A.apply_and_commit("cooling", 24, st, TS)
    assert r["status"] == "failed" and r["reason"] == "verify_off_after_on" and st["mode"] == "off" and st["run_start"] is None, (r, st)

    A.ac_apply = lambda m, t: fake_apply(m, t, "action", "关")
    A.verify_socket = lambda: "on"
    saved.clear(); called.clear()
    st = {"mode": "cooling", "run_start": "2026-08-14T12:00:00", "last_off_at": None}
    r = A.apply_and_commit("off", None, st, TS)
    assert r["status"] == "failed" and r["reason"] == "verify_on_after_off" and st["mode"] == "cooling" and "last_off_at" not in st, (r, st)

    A.ac_apply = lambda m, t: fake_apply(m, t, "action", "开")
    A.verify_socket = lambda: None
    saved.clear(); called.clear()
    st = {"mode": "off", "run_start": None}
    r = A.apply_and_commit("cooling", 24, st, TS)
    assert r["status"] == "failed" and r["reason"] == "verify_unreachable" and st == {"mode": "off", "run_start": None}, (r, st)

    A.ac_apply = lambda m, t: fake_apply(m, t, "no_action")
    A.verify_socket = lambda: "off"
    saved.clear(); called.clear()
    st = {"mode": "off", "run_start": None}
    r = A.apply_and_commit("off", None, st, TS)
    assert r["status"] == "no_action" and st["mode"] == "off", (r, st)

    total = len(comp_cases) + 11  # decide cases
    print(f"selftest OK: {total} decide + 9 apply_and_commit 状态路径")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()