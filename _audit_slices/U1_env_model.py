# 审计单元 U1_env_model —— 由 mk_slices.py 从 ac_advisor.py (sha:96eb82598019) 生成，请勿手改。
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

# ==== comfort_index  (原文件行 56-57) ====
def comfort_index(temp, hum):
    return temp if hum is None else temp + 0.05 * (hum - 10)

# ==== dew_point  (原文件行 60-65) ====
def dew_point(temp, hum):
    if hum is None or hum <= 0:
        return None
    a, b = 17.27, 237.7
    alpha = (a * temp) / (b + temp) + math.log(hum / 100.0)
    return (b * alpha) / (a - alpha)

# ==== muggy_level  (原文件行 68-79) ====
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

# ==== seasonal_adjustments  (原文件行 82-91) ====
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

# ==== compute_optimal_schedule  (原文件行 95-183) ====
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

# ==== find_pre_cool_window  (原文件行 186-213) ====
def find_pre_cool_window(schedule, current_hour):
    # v8.50 fix (Astra外审 pass2#1): 生产端 compute_optimal_schedule 自 v8.29 起
    # append 四元组 (h, action, cost, t_current)，原实现按三元组解包 →
    # 任何非空 schedule 传入即 ValueError（潜伏地雷，无调用点未炸）。
    # 且 h 是 local_times 的数组索引（相对当前小时的偏移 0..23，v8.36 注释
    # 已明确），原实现当绝对小时判 6<=h<=21 → 语义错误。改为
    # 绝对小时 = (current_hour + h) % 24，current_hour 参数从此真正被使用。
    hot_start = None
    for i, (h, action, cost, _t) in enumerate(schedule):
        h_abs = (current_hour + h) % 24
        if action == "cool" and 6 <= h_abs <= 21:
            hot_start = h_abs
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

# ==== predict_dehumidify_need  (原文件行 216-258) ====
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
            # v8.50 fix (Astra外审 pass2#6): 天气接口可能返回带时区偏移的 ISO
            # 时间（+08:00）→ aware datetime 减 naive datetime.now() 抛 TypeError，
            # 被 except:continue 吞掉 → future_rh 永远空 → 预除湿预测静默失效。
            # 统一为同"时区感知"基准比较。
            ref_now = datetime.now(t_dt.tzinfo) if t_dt.tzinfo else datetime.now()
            hours_ahead = (t_dt - ref_now).total_seconds() / 3600
            if 6 <= hours_ahead <= 30:
                future_rh.append(hums[i] if i < len(hums) else None)
        except:
            continue
    # v8.50 fix (Astra外审 pass2#6): 窗口内湿度全为 None 时旧代码 max() 对空
    # 生成器抛 ValueError——应正常返回"数据不足"，而不是崩。
    valid_rh = [r for r in future_rh if r is not None]
    if not valid_rh:
        return False, None, None
    max_future_rh = max(valid_rh)
    avg_future_rh = sum(valid_rh) / len(valid_rh)
    if max_future_rh > 70 or avg_future_rh > 65:
        if current_hum and current_hum > 55:
            # v8.59 fix (DeepSeek 交叉审计 D2, P1): 第二返回值此前硬编码 55，
            # 语义是"%RH 目标"，但唯一消费点 ac_watch.py:1866 把它当**温度**
            # 用（v8.39 的 int(min(26,max(24,55)))=26 只夹住了数值，没改语义）。
            # 结果是"预除湿到 55%RH"被下发成"制冷 26°C"，日志却仍写"预除湿至55%"。
            # 2026-08-21 22:38 实证：预除湿分支把 55 填进 _schedule_target，
            # 覆盖了 DP 原本的 target=24，下发 target=55°C 并持续 24h07m。
            # 契约修正：第二返回值统一为**除湿制冷温度**，与消费点同名同量纲。
            return (
                True,
                DEHUMIDIFY_COOL_TARGET,
                f"谷电预湿：明日最高RH{max_future_rh:.0f}%，当前{current_hum:.0f}%，"
                f"预除湿制冷至{DEHUMIDIFY_COOL_TARGET}°C",
            )
    return False, None, None

