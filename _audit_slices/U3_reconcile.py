# 审计单元 U3_reconcile —— 由 mk_slices.py 从 ac_advisor.py (sha:96eb82598019) 生成，请勿手改。
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

# ==== _anchor_oscillating  (原文件行 1077-1100) ====
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

# ==== reconcile_state  (原文件行 1103-1355) ====
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
    # v8.50 fix (Astra外审 pass2#4): 延迟验证到期时旧代码取**当前** state 的
    # rh_history 尾值和 mode——若 10 分钟窗口内用户已关机（真实场景），原本的
    # "手动开机"记录会被写成 mode=off、湿度取错时点 → 两条关机样本、丢掉开机
    # 样本，舒适权重系统性偏低。改为创建待验证事件时保存不可变快照，核验只
    # 决定是否接纳该快照，不再从当前状态重新推断。
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
            # v8.50c (Astra验收二#12): 锚点时刻 rh 快照缺失（rh_history 当时为空）
            # → 不得回退到 10 分钟后的当前湿度学习（会把事件环境张冠李戴）。
            # v8.50d (Astra验收三#12): 只跳过学习，**不得 return 退出整个
            # reconcile_state**——原实现把 pending 消费提前 return，后续
            # 关翻转对账（socket off + 运行态）被整体跳过，幻象关翻转失去
            # 门控。改为仅记录"学习资格降级"标记，继续走下方对账。
            if _pml.get("rh") is None:
                if _k0 is not None and _k1 is not None and (_k1 - _k0) >= 0.005:
                    state["_manual_learn_skipped_rh"] = True  # 电量佐证成立但缺环境快照
                # 快照缺失一律不喂样本；继续对账
            elif _k0 is not None and _k1 is not None and (_k1 - _k0) >= 0.005:
                # v8.50d (Astra验收三#2/#4): 电量证据缺失时也**不学习**——
                # 原条件 `_k0 is None or _k1 is None or ...` 让"缺电量"直接
                # 通过，与 v8.48 确立的「kWh 是唯一不可伪造量」矛盾（电量不可
                # 测 = 无法核验 = 不喂样本，宁缺毋滥；真手动事件在电量恢复后
                # 由后续事件继续学习）。证据不足时保持对账、不 return。
                _learn_from_manual(
                    state, _pml["ts"], rh=_pml.get("rh"), mode=_pml.get("mode")
                )
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
        # v8.50 fix (Astra外审 pass2#2): 手动关机学习此前直接入账、无 kWh 验证
        # ——幻象「on 锚点建立运行态 → 下一拍 off+1W」的关机样本仍会绕过验证
        # （v8.48 只堵了开机路径，kWh 冻结时关机学习照常入账，可反复把舒适
        # 权重拉向"用户倾向关机"）。统一要求：被中断的运行阶段确有电量消耗
        # （_run_start_kwh 起点），零功耗运行 = 幻象配对，静默对账不学习。
        _rs_kwh = state.get("_run_start_kwh")
        if _rs_kwh is not None and state.get("estimated_kwh") is not None:
            if state["estimated_kwh"] - _rs_kwh < 0.005:
                state["mode"] = "off"
                state["last_off_at"] = now_ts
                state["run_start"] = None
                state.pop("_system_off_at", None)
                state.pop("_run_start_kwh", None)
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
            # v8.50e (Astra验收五#2 ❌): 关机学习同样要求电量正准入证——上方的
            # _run_start_kwh 门控只拦"零功耗运行"，但电量缺失（_run_start_kwh 或
            # estimated_kwh 为 None）时直接落入这里照常学习，幻象配对仍可绕过。
            # 与 pending 开机路径对齐：两端电量非空且增量 ≥0.005 才入样本；
            # 电量缺失完成对账（mode=off/manual_off_at 已置），但不喂学习。
            _rs_k2 = state.get("_run_start_kwh")
            _k2 = state.get("estimated_kwh")
            if (
                _rs_k2 is not None
                and _k2 is not None
                and (_k2 - _rs_k2) >= 0.005
            ):
                _learn_from_manual(state, now_ts)
            else:
                state["_manual_learn_skipped_kwh"] = True
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
        state["_run_start_kwh"] = state.get("estimated_kwh")  # v8.50: 关机学习 kWh 佐证起点
        state["_fake_run_count"] = 0
        # v8.48 修复①: 学习喂入统一延迟验证——功率铁证路径不再立即入账。
        # 幻象功率读数与 is_on 同源，可穿透 v8.46「铁证」（09-04 晚实证:
        # kWh今冻结仍 4 锚点+4 次立即学习，comfort_weight 0.5→0.8）；
        # 唯一不可伪造的物理现实是 kWh 增量，真压缩机 10min 必过 0.005 闸。
        # v8.50 fix (Astra外审 pass2#4): pending 快照存 rh/mode——延迟 10min
        # 核验到期时若用户已关机，旧代码用当前 state 会把开机事件学成关机。
        _rh_hist = state.get("rh_history") or []
        _snap_rh = _rh_hist[-1][1] if _rh_hist else None
        state["_pending_manual_on_learn"] = {
            "ts": now_ts,
            "kwh": state.get("estimated_kwh"),
            "rh": _snap_rh,
            "mode": "cooling",
        }

# ==== verify_socket  (原文件行 1358-1365) ====
def verify_socket():
    if AC_CTRL is None:
        return None
    try:
        s = AC_CTRL.status()
        return "on" if s.is_on else "off"
    except:
        return None

# ==== verify_target_temp  (原文件行 1368-1384) ====
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

# ==== apply_state_from_verify  (原文件行 1387-1415) ====
def apply_state_from_verify(state, new_mode, real, now_ts):
    was_on = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    if real == "on":
        if new_mode in ("cooling", "dehumid", "dehumid_alert"):
            if not was_on:
                state["run_start"] = now_ts
                state["last_on_at"] = now_ts
                # v8.59 fix (DeepSeek 交叉审计 T6, P2): 新 run 起点必须同步刷新
                # _run_start_kwh 起点快照。原先只有手动开机锚点（L1327）刷新，
                # 本路径只设 run_start → 留下**旧 run 的 kWh 起点**，
                # 使 reconcile_state 的关机学习门控
                # `estimated_kwh - _run_start_kwh >= 0.005` 用错误起点比对、
                # 增量被高估 → 幻象配对可能被学习（v8.50e 想堵的东西在此漏了）。
                state["_run_start_kwh"] = state.get("estimated_kwh")
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

