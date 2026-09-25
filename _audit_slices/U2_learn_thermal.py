# 审计单元 U2_learn_thermal —— 由 mk_slices.py 从 ac_advisor.py (sha:96eb82598019) 生成，请勿手改。
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

# ==== load_learned  (原文件行 269-277) ====
def load_learned():
    default = {"adjusted_thresholds": {}, "decision_log": []}
    try:
        if os.path.exists(LEARN_FILE):
            with open(LEARN_FILE, encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default

# ==== save_learned  (原文件行 280-284) ====
def save_learned(learned):
    tmp = LEARN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEARN_FILE)

# ==== evaluate_and_learn  (原文件行 287-503) ====
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
        # v8.50 fix (Astra外审 pass1#6/#8): 未执行决策（dry/vent拦截/控制失败）
        # 不参与回评学习——旧代码用"计划"当"效果"学习，假样本污染偏移。
        if entry.get("executed") is False:
            entry["evaluated"] = True
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
            # v8.50 fix (Astra外审 pass2#5): 决策时单帧功率 >300W 不再是成功铁证
            # ——幻象功率读数与 is_on 同源，可穿透旧判据（09-04 实证：kWh 冻结仍
            # 打出高功率幻象帧）。改为「决策后 kWh 增量 ≥0.005」佐证（真压缩机
            # 30-120min 必远超此值）；决策时无功率帧但温湿度无改善才算失败。
            # v8.50c (Astra验收二#13): 无 kwh_at_decision 快照（v8.50 之前的旧
            # 日志条目）且只有单帧高功率 → 证据不足，标记"不可评价"跳过——
            # 不判成功也不判失败（缺证据时不得用高功率帧代替电量佐证）。
            _kd0 = entry.get("kwh_at_decision")
            _kd1 = state.get("estimated_kwh")
            if _kd0 is not None and _kd1 is not None:
                success = (_kd1 - _kd0) >= 0.005
            elif (entry.get("power_at_decision") or 0) > 300:
                entry["evaluated"] = True
                entry["eval_note"] = "insufficient_evidence_no_kwh"
                continue
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
    # v8.50 fix (Astra外审 pass2#3): 预算上调与护栏在**同一次调用**内互相抵消
    # （超预算 +0.5 → 护栏 -0.5 → 偏移锁死在当前值，注释承诺的"高温自愈"永不
    # 发生）。改为护栏优先：护栏触发轮禁止预算上调，保证净回落；预算上调按
    # 小时窗口去重（main 一轮调 evaluate 两次，无去重则同一超支事实重复加码）。
    _guardrail_fired = False
    if adjusted.get("temp_cooling", 0) > 0 and 8 <= datetime.now().hour < 21:
        if (state.get("last_temp") or 0) >= TEMP_COOLING:
            _off_min = minutes_since(state.get("last_off_at"))
            if _off_min is not None and _off_min > 20:
                _guardrail_fired = True
                adjusted["temp_cooling"] = round(
                    max(0, adjusted.get("temp_cooling", 0) - 0.5), 2
                )
    _bump_ok = not _guardrail_fired
    if _bump_ok:
        _last_bump = state.get("_budget_bump_ts")
        if _last_bump is not None:
            _bump_min = minutes_since(_last_bump)
            if _bump_min is not None and _bump_min < 60:
                _bump_ok = False
    if not _has_band:
        # 回退口径：无峰谷分时数据时按原始 kWh 判断
        if _bump_ok and daily_kwh > daily_budget and adjusted.get("temp_cooling", 0) < 3:
            adjusted["temp_cooling"] = min(3, adjusted.get("temp_cooling", 0) + 0.5)
            state["_budget_bump_ts"] = now_ts
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
            if _bump_ok and daily_cost > _cost_budget and adjusted.get("temp_cooling", 0) < 3:
                adjusted["temp_cooling"] = min(3, adjusted.get("temp_cooling", 0) + 0.5)
                state["_budget_bump_ts"] = now_ts
            elif (
                daily_cost < _cost_budget * 0.5 and adjusted.get("temp_cooling", 0) > 0
            ):
                adjusted["temp_cooling"] = max(0, adjusted.get("temp_cooling", 0) - 0.5)
        except Exception:
            pass
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

# ==== log_decision  (原文件行 506-529) ====
def log_decision(
    state, action, pre_temp, pre_hum, now_ts, reason=None, executed=True
):  # v8.39: 加 reason 参数，支持决策归因
    # v8.50 fix (Astra外审 pass1#6/#8): 加 executed 参数——dry/vent拦截/控制失败
    # 的「计划」此前也写入决策日志并计入启动次数与回评学习，污染两处消费方。
    # 现在由 main() 按 apply 结果标注；decide 的启动限次只认 executed=True。
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
            "executed": executed,
            "power_at_decision": power_at_decision,
            "kwh_at_decision": state.get("estimated_kwh"),  # v8.50: 回评 kWh 佐证
            "reason": reason,
        }
    )  # v8.39: 决策原因，支持 grep 归因
    learned["decision_log"] = log[-50:]
    save_learned(learned)

# ==== load_thermal_data  (原文件行 538-553) ====
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

# ==== save_thermal_data  (原文件行 556-560) ====
def save_thermal_data(data):
    tmp = THERMAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, THERMAL_FILE)

# ==== _thermal_event_usable  (原文件行 563-570) ====
def _thermal_event_usable(e):
    if not isinstance(e, dict):
        return False
    for k in ("temp_before", "temp_after", "duration_min"):
        v = e.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
    return e["duration_min"] > 0

# ==== record_thermal_event  (原文件行 573-610) ====
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

# ==== fit_thermal_model  (原文件行 613-657) ====
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

# ==== predict_cooling_time  (原文件行 660-676) ====
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

