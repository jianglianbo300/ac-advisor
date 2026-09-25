# 审计单元 U4_commit_learn —— 由 mk_slices.py 从 ac_advisor.py (sha:96eb82598019) 生成，请勿手改。
# 本单元为 AST 取整函数切片；依赖项由 verify_findings.py 注入。

# ==== 模块级常量 ====
AC_INPUT_W = 1076
ELECTRIC_PEAK = 0.617
ELECTRIC_VALLEY = 0.307
DEHUMID_DUTY = 0.60
COOL_DUTY = 0.70
COOL_BURST_MIN = 40
DEHUMIDIFY_COOL_TARGET = 26
CTRL_FAIL_ALERT_THRESHOLD = 8  # 连续失败≥8次(≈16min@2min tick)触发告警
NIGHT_HOURS = 6
TEMP_COOLING = 27
TEMP_ABSOLUTE_FLOOR = 24
EVAL_DELAY_MIN = 30
EVAL_STALE_MIN = 120

# ==== kwh_est  (原文件行 699-701，模块级辅助) ====
def kwh_est(active_min, duty=1.0):
    p = AC_MEASURED_W or AC_INPUT_W
    return p / 1000.0 * duty * (active_min / 60.0)

# ==== apply_and_commit  (原文件行 1418-1496) ====
def apply_and_commit(
    new_mode, target_temp, state, now_ts=None, meta=None, tts_reason=None
):
    if now_ts is None:
        now_ts = datetime.now().isoformat(timespec="seconds")
    ctrl = ac_apply(new_mode, target_temp)
    if ctrl["status"] == "failed":
        # v8.55: 暂停期（ac_control=false → AC_CTRL=None）产生的 control_unavailable
        # 不累计连续失败、不触发告警——09-19 用户只关软开关、保留 cron（采集不断链），
        # 决策引擎每 2 分钟空转，streak 一天刷到 678、ac_alerts.jsonl 671 条垃圾、
        # 且微信投递 cron 已禁用（无人收件）。软开关暂停是用户意图不是故障。
        if ctrl.get("reason") != "control_unavailable":
            # v8.51: 连续失败计数，阈值触发告警（曾静默重试 2.5h 无提醒）
            streak = int(state.get("_ctrl_fail_streak", 0)) + 1
            state["_ctrl_fail_streak"] = streak
            save_state(state)
            if streak >= CTRL_FAIL_ALERT_THRESHOLD:
                _alert_ctrl_failure(state, ctrl, streak)
        else:
            save_state(state)
        return ctrl
    real = verify_socket()
    if real is None:
        ctrl = {
            "status": "failed",
            "action": ctrl.get("action", ""),
            "reason": "verify_unreachable",
        }
        # v8.55: verify 失败同口径计入连续失败计数（旧代码此处直接 return，
        # 指令发出但验证不可达的失败永不计 streak → 告警盲区）
        streak = int(state.get("_ctrl_fail_streak", 0)) + 1
        state["_ctrl_fail_streak"] = streak
        save_state(state)
        if streak >= CTRL_FAIL_ALERT_THRESHOLD:
            _alert_ctrl_failure(state, ctrl, streak)
        return ctrl
    contradict = apply_state_from_verify(state, new_mode, real, now_ts)
    if contradict:
        ctrl = {
            "status": "failed",
            "action": ctrl.get("action", ""),
            "reason": "verify_on_after_off" if real == "on" else "verify_off_after_on",
        }
        # v8.55: 矛盾回读同样是控制失败，计入 streak（旧代码跌进 success 路径，
        # 下方 state.pop("_ctrl_fail_streak") 反而把计数清零 → 反复矛盾永不告警）
        streak = int(state.get("_ctrl_fail_streak", 0)) + 1
        state["_ctrl_fail_streak"] = streak
        if streak >= CTRL_FAIL_ALERT_THRESHOLD:
            _alert_ctrl_failure(state, ctrl, streak)
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
    # v8.59 fix (DeepSeek 交叉审计 T1, P1): 原为无条件执行，会把上面 contradict 分支
    # 刚累加的 _ctrl_fail_streak 立刻清零 → 反复矛盾回读永不告警（20 轮 0 告警）。
    # v8.55 的注释已点明此坑，但只加了累加、漏改本行。改为仅"控制成功"才清零。
    if not contradict:
        state.pop("_ctrl_fail_streak", None)  # v8.51: 控制成功即清零连续失败计数
    save_state(state)
    if tts_reason and not contradict:
        try:
            from ac_watch import tts_speak

            tts_speak(tts_reason)
        except:
            pass
    return ctrl

