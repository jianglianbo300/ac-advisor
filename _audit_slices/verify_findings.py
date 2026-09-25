# -*- coding: utf-8 -*-
"""外审结论可复现验证器（U1/U2/U3/U4/U5 切片）。

不依赖 miio / 网络 / 真实 state 文件：把 5 个切片 exec 进同一个命名空间，
注入与生产同值的常量与桩，逐条复现审计结论。

运行：python verify_findings.py
"""
import io
import json
import math
import os
import sys
import types
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'OK ' if cond else 'BAD'}] {name}{(' | ' + str(detail)) if detail else ''}")


# ── 生产常量（ac_advisor.py v8.58 实测值）───────────────────────────
ENV = dict(
    math=math, json=json, os=os,
    datetime=datetime, timezone=timezone, timedelta=timedelta,
    ELECTRIC_PEAK=0.617, ELECTRIC_VALLEY=0.307,
    COOL_DUTY=0.70, COOL_BURST_MIN=40, DEHUMID_DUTY=0.60, NIGHT_HOURS=6,
    AC_INPUT_W=1076, AC_MEASURED_W=None, AC_SOCKET=None, AC_COMPANION_TARGET=None,
    TEMP_COOLING=27, TEMP_ABSOLUTE_FLOOR=24,
    EVAL_DELAY_MIN=30, EVAL_STALE_MIN=120, CTRL_FAIL_ALERT_THRESHOLD=8,
    HEAL_MIN_INTERVAL_SEC=1800, SCRIPT_DIR=BASE,
    CONFIG_FILE=os.path.join(BASE, "miio_config.json"),
    LEARN_FILE=os.path.join(BASE, "_t_learned.json"),
    THERMAL_FILE=os.path.join(BASE, "_t_thermal.json"),
    ALERTS_FILE=os.path.join(BASE, "_t_alerts.jsonl"),
    _WARN_LOG=os.path.join(BASE, "_t_error.log"),
    _HEAL_STATE=os.path.join(BASE, "_t_heal.json"),
    AC_CTRL=None, AC_CONTROL_PAUSED=False,
)
ORDER = ["U2_learn_thermal", "U1_env_model", "U5_control", "U3_reconcile", "U4_commit_learn"]


def build():
    mod = types.ModuleType("slice_env")
    mod.__dict__.update(ENV)
    for unit in ORDER:
        src = io.open(os.path.join(BASE, unit + ".py"), encoding="utf-8").read()
        exec(compile(src, unit + ".py", "exec"), mod.__dict__)
    # 桩：热模型 / 落盘 / 天气 / 时间差，避免触碰真实文件与网络
    mod.load_thermal_data = lambda: {
        "thermal_model": {
            "thermal_conductance": 0.003,
            "baseline_cooling": -0.035,
            "time_constant_min": 120,
        }
    }
    mod.save_state = lambda s: None
    mod.save_thermal_data = lambda d: None
    mod.save_learned = lambda d: None
    mod.fetch_weather = lambda: {}
    mod.minutes_since = lambda ts: None
    return mod


def diurnal_wx(peak=35.0, night=27.0):
    """造 24h 逐时：time[0]=当前整点（生产 fetch_weather 的语义），15 点最高。"""
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    times, temps, hums = [], [], []
    for i in range(24):
        t = now + timedelta(hours=i)
        h = t.hour
        x = max(0.0, math.cos((h - 15) / 24.0 * 2 * math.pi))
        temps.append(round(night + (peak - night) * x, 1))
        hums.append(60 if x < 0.5 else 45)
        times.append(t.strftime("%Y-%m-%dT%H:%M"))
    return {"hourly": {"time": times, "temperature_2m": temps, "relative_humidity_2m": hums}}


# ══ T1 (U4)·矛盾回读的 streak 被无条件 pop 掉 → 告警永不触发 ═══════════
def t1_contradict_streak(mod):
    print("\n── T1 U4 apply_and_commit：contradict 分支 streak 立即被清除 ──")
    alerts = []
    mod._alert_ctrl_failure = lambda state, ctrl, streak: alerts.append(streak)
    # 我方下令"关机"，设备回读仍 on → apply_state_from_verify 返回 True（矛盾）
    mod.ac_apply = lambda m, t=None: {"status": "action", "action": "关机", "reason": ""}
    mod.verify_socket = lambda: "on"
    mod.verify_target_temp = lambda: None
    state = {"mode": "cooling"}
    seen = []
    for i in range(20):
        ctrl = mod.apply_and_commit("off", None, state, now_ts="2026-09-25T10:%02d:00" % i)
        seen.append((ctrl["status"], ctrl["reason"], state.get("_ctrl_fail_streak")))
    check("20 轮矛盾回读全部判 failed", all(s[0] == "failed" for s in seen), seen[0])
    # v8.59 已修 T1（pop 改用 `if not contradict` 守卫）→ 断言改为**修复后**语义。
    # 修复前此断言为：每轮 streak 都被重置为 None。
    check("streak 逐轮累积 1..20（修复后：不再被无条件重置）",
          [s[2] for s in seen] == list(range(1, 21)), [s[2] for s in seen][:5])
    check("20 轮矛盾回读在阈值 8 起触发告警（修复后）",
          len(alerts) > 0 and min(alerts) == 8, alerts[:5])
    # 对照：verify_unreachable 分支能正常累积（说明是 pop 位置问题，非误判）
    # 注：alerts 累计了上一段的 [8..20]，故此处只断言"新一段同样从 8 开始累积"。
    mod.verify_socket = lambda: None
    n_before = len(alerts)
    st2 = {}
    for i in range(9):
        mod.apply_and_commit("off", None, st2, now_ts="2026-09-25T11:%02d:00" % i)
    seg2 = alerts[n_before:]
    check("对照：verify_unreachable 分支可累积至阈值并告警",
          seg2 == [8, 9], seg2)
    check("对照：越阈值后每轮重复告警（无再告警抑制）", len(seg2) > 1, seg2)


# ══ T2 (U1)·DP 电价信号强度 vs 舒适罚项 ═══════════════════════════
def t2_dp_price_sensitivity(mod):
    print("\n── T2 U1 compute_optimal_schedule：电价能否改变 DP 决策 ──")
    wx = diurnal_wx()

    def run(peak, valley, cw):
        mod.ELECTRIC_PEAK, mod.ELECTRIC_VALLEY = peak, valley
        s = mod.compute_optimal_schedule(
            wx, 30.0, 60.0, {}, comfort_weight=cw, comfort_target=26.0
        )
        return sum(1 for r in s if r[1] == "cool"), s[0][1], [r[1] for r in s[:6]]

    base = run(0.617, 0.307, 0.5)          # 真实电价
    swap = run(0.307, 0.617, 0.5)          # 峰谷对调
    cw_low = run(0.617, 0.307, 0.05)       # 舒适权重降 10 倍
    cw_hi = run(0.617, 0.307, 1.0)
    check("峰谷电价对调 → 制冷小时数/首动作不变（电价信号被舒适项淹没）",
          base[0] == swap[0] and base[1] == swap[1], f"base={base} swap={swap}")
    check("comfort_weight 0.05→1.0 也不改变首动作",
          cw_low[1] == cw_hi[1] == base[1], f"cw0.05={cw_low} cw1.0={cw_hi}")
    elec = mod.kwh_est(60, mod.COOL_DUTY) * 0.617
    pen = 0.5 * (30.0 - 26.0) ** 2
    check("舒适罚项比 1h 电费高 1 个数量级以上", pen / elec > 10,
          f"{pen:.2f} vs {elec:.2f} 元")


# ══ T3 (U1)·find_pre_cool_window 返回跨零点区间 ═════════════════════
def t3_precool_wrap(mod):
    print("\n── T3 U1 find_pre_cool_window：pc_start > pc_end（跨零点）──")
    schedule = [(h, "cool" if h == 7 else "off", 0.0, 26.0) for h in range(24)]
    out = mod.find_pre_cool_window(schedule, 23)  # h=7 → 绝对 06:00
    check("返回三元组", isinstance(out, tuple) and len(out) == 3, out)
    pc_start, pc_end, delta = out
    check("pc_start > pc_end（22 > 5）→ 朴素 range(pc_start, pc_end+1) 得空窗",
          pc_start > pc_end and list(range(pc_start, pc_end + 1)) == [],
          f"({pc_start},{pc_end},{delta:.2f})")
    check("窗口覆盖整个谷段 8 小时（22,23,0,1,2,3,4,5）", (pc_start, pc_end) == (22, 5),
          (pc_start, pc_end))


# ══ T4 (U2)·cur_hum=None 时 cooling 决策被判成功 ══════════════════
def t4_hum_none_success(mod):
    print("\n── T4 U2 evaluate_and_learn：湿度缺失把'无效'判成'有效' ──")
    entry = {
        "time": (datetime.now() - timedelta(minutes=60)).isoformat(timespec="seconds"),
        "action": "cooling", "pre_temp": 30.0, "pre_hum": 60.0,
        "evaluated": False, "executed": True,
        "power_at_decision": 100, "kwh_at_decision": None, "reason": "t",
    }
    out = {}
    for tag, last_hum in (("RH=None(传感器掉线)", None), ("RH=60(正常)", 60)):
        learned = {"adjusted_thresholds": {"temp_cooling": 2}, "decision_log": [dict(entry)]}
        mod.load_learned = lambda l=learned: l
        state = {"last_temp": 30.0, "last_hum": last_hum}
        mod.evaluate_and_learn(state, datetime.now().isoformat(timespec="seconds"))
        out[tag] = learned["adjusted_thresholds"]["temp_cooling"]
    check("RH 缺失 → 判 success（相对正常情形多降 1.0）",
          out["RH=60(正常)"] - out["RH=None(传感器掉线)"] == 1.0, out)
    check("RH=60 → 正确判 failure（该条不降偏移，只走预算回落 -0.5）",
          out["RH=60(正常)"] == 1.5, out)


# ══ D2/T5 (U1×ac_watch)·除湿目标单位错配（v8.59 已修） ══════════════
def t5_dehumid_unit_cross(mod):
    print("\n── D2/T5 U1 predict_dehumidify_need：返回值单位与消费方一致 ──")
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    times = [(now + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(36)]
    wx = {"hourly": {"time": times, "relative_humidity_2m": [70] * 36}}
    need, target, reason = mod.predict_dehumidify_need(wx, 60, 29.0)
    check("触发预除湿", need is True, (need, target))
    # v8.59 已修 D2：第二返回值改为**温度**（DEHUMIDIFY_COOL_TARGET），
    # 不再是 %RH 的 55。修复前此断言为 target == 55。
    check("第二返回值是温度 26，不再是被误当温度用的 55(%RH)",
          target == 26, target)
    check("返回值与消费点同量纲：夹取不再改变数值（幂等）",
          int(min(26, max(24, target))) == target, target)
    check("文案不再谎称 55%RH，改为与实际动作一致的制冷温度",
          "预除湿至55%" not in reason and "26" in reason, reason)



# ══ T6 (U3)·_run_start_kwh 生命周期：kWh 门控退化为恒真 ═════════════
def t6_run_start_kwh_stale(mod):
    print("\n── T6 U3 reconcile_state：_run_start_kwh 陈旧 → kWh 门控失效 ──")
    calls = []
    mod._learn_from_manual = (
        lambda state, now_ts, rh=None, mode=None: calls.append((now_ts, rh, mode))
    )
    mod.AC_SOCKET = "off"

    def scenario(rs_kwh, note):
        calls.clear()
        state = {
            "mode": "cooling",
            "run_start": "2026-09-25T08:00:00",
            "estimated_kwh": 9.0,
            "rh_history": [["2026-09-25T09:58:00", 62]],
            "_prev_power": 1200,  # 上一拍确有压缩机级活动
        }
        if rs_kwh is not None:
            state["_run_start_kwh"] = rs_kwh
        mod.reconcile_state(state, "2026-09-25T10:00:00", load_power=None)
        print("   %-26s → mode=%s 学习=%d skipped_kwh=%s skipped_rh=%s" % (
            note, state.get("mode"), len(calls),
            state.get("_manual_learn_skipped_kwh"),
            state.get("_manual_learn_skipped_rh")))
        return len(calls)

    la = scenario(None, "无起点(首个周期)")
    lb = scenario(8.999, "起点=本周期(delta=0.001)")
    lc = scenario(5.0, "陈旧起点(delta=4.0)")
    check("无 _run_start_kwh → 只对账不学习（v8.50e）", la == 0, la)
    check("起点=本周期且增量<0.005 → 判幻象不学习", lb == 0, lb)
    check("陈旧起点 → 同一翻转被学习（门控恒真）", lc == 1, lc)
    check("同样一次'关'翻转，结论由 _run_start_kwh 的 epoch 决定（lb≠lc）", lb != lc, (lb, lc))


# ══ T7 (U3)·fan_locked 回读被当矛盾 ══════════════════════════════
def t7_fan_locked(mod):
    print("\n── T7 U3 apply_state_from_verify：fan_locked 命中矛盾分支 ──")
    state = {"mode": "off"}
    r = mod.apply_state_from_verify(state, "fan_locked", "on", "2026-09-25T10:00:00")
    check("new_mode=fan_locked + real=on → 判矛盾并强改 mode=cooling",
          r is True and state["mode"] == "cooling", (r, state))


if __name__ == "__main__":
    mod = build()
    t1_contradict_streak(mod)
    t2_dp_price_sensitivity(mod)
    t3_precool_wrap(mod)
    t4_hum_none_success(mod)
    t5_dehumid_unit_cross(mod)
    t6_run_start_kwh_stale(mod)
    t7_fan_locked(mod)
    print(f"\n=== 通过 {len(PASS)} / 断言 {len(PASS) + len(FAIL)} ===")
    if FAIL:
        print("未通过：", FAIL)
        sys.exit(1)

