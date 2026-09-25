# 审计单元 U5_control —— 由 mk_slices.py 从 ac_advisor.py (sha:96eb82598019) 生成，请勿手改。
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

# ==== ac_control_init  (原文件行 945-967) ====
def ac_control_init():
    global AC_CTRL, AC_CONTROL_PAUSED
    AC_CTRL = None
    AC_CONTROL_PAUSED = False
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # v8.55: 显式区分「用户暂停」（ac_control=false，用户意图，静默跳过决策）
        # 与「控制不可用」（ac_control=true 但伴侣掉线/初始化失败，是真故障，
        # 需照常走到 apply_and_commit 产生 streak/告警）。旧代码两者都只有
        # AC_CTRL=None 一个信号，无法区分（v8.55 早退块曾用 AC_CTRL is None
        # 判定 → test_sensor_fallback 19 例全灭：测试 stub 后 AC_CTRL=None
        # 被误当暂停；真实伴侣掉线也会被误静默——正是 v8.51 修的告警盲区）。
        if not cfg.get("ac_control", True):
            AC_CONTROL_PAUSED = True
            return
        ap = cfg.get("ac_partner") or {}
        if ap.get("ip") and ap.get("token"):
            from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02

            AC_CTRL = AirConditioningCompanionMcn02(ap["ip"], ap["token"])
    except:
        AC_CTRL = None

# ==== ac_apply  (原文件行 1007-1074) ====
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

# ==== _alert_ctrl_failure  (原文件行 987-1004) ====
def _alert_ctrl_failure(state, ctrl, streak):
    """v8.51: 空调控制持续失败告警 —— 写告警日志 + Windows toast + stdout 标记。
    由 hermes_cron/ac_alert_wrapper.py 每 10 分钟去重投递微信。"""
    try:
        os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "streak": int(streak),
            "action": ctrl.get("action", ""),
            "reason": (ctrl.get("reason") or "")[:300],
            "mode": state.get("mode"),
        }
        with open(ALERTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _notify_toast("空调控制异常", f"连续 {streak} 次控制失败（{rec['reason']}）")
        print(f"[ALERT] 空调控制持续失败 {streak} 次: {rec['reason']}")
    except Exception:
        pass

# ==== read_indoor  (原文件行 904-918) ====
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

# ==== _read_indoor_once  (原文件行 921-934) ====
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

# ==== _notify_toast  (原文件行 970-984) ====
def _notify_toast(title, text):
    """Windows toast, best-effort (never raises)."""
    try:
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$text = $t.GetElementsByTagName('text');"
            "$text.Item(0).AppendChild($t.CreateTextNode('{0}')) > $null;"
            "$text.Item(1).AppendChild($t.CreateTextNode('{1}')) > $null;"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PiAgent').Show($toast)"
        ).format(title, text.replace("'", "").replace('"', ''))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=10, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass

# ==== miio_discover  (原文件行 1521-1544) ====
def miio_discover(timeout=6):
    """广播扫描局域网 miio 设备，返回 IP 列表（UDP 54321 握手）。"""
    import socket
    import time

    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(timeout)
        s.sendto(b"\x21\x31\x00\x20" + b"\xff" * 28, ("255.255.255.255", 54321))
        end = time.time() + timeout
        while time.time() < end:
            try:
                _data, addr = s.recvfrom(1024)
                if addr[0] not in ips:
                    ips.append(addr[0])
            except Exception:
                break
        s.close()
    except Exception:
        pass
    return ips

# ==== heal_partner_ip  (原文件行 1547-1599) ====
def heal_partner_ip(token):
    """伴侣 IP 漂移自愈：广播扫描 + 原 token 逐个验证，命中则回写 CONFIG_FILE。

    返回新 IP；未找到、或在限流窗口内则返回 None。
    """
    # 限流：避免每 2 分钟一轮的巡检反复做 6 秒广播扫描
    try:
        with open(_HEAL_STATE) as f:
            last = json.load(f).get("ts")
        if last and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < HEAL_MIN_INTERVAL_SEC:
            return None
    except Exception:
        pass
    try:
        with open(_HEAL_STATE, "w") as f:
            json.dump({"ts": datetime.now().isoformat(timespec="seconds")}, f)
    except Exception:
        pass

    from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02

    candidates = miio_discover()
    for ip in candidates:
        try:
            st = AirConditioningCompanionMcn02(ip, token).status()
            # 伴侣的特征：能读到 load_power（净化器等设备没有这个属性）
            if getattr(st, "load_power", None) is None:
                continue
        except Exception:
            continue
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            old = (cfg.get("ac_partner") or {}).get("ip")
            if old != ip:
                cfg.setdefault("ac_partner", {})["ip"] = ip
                cfg["ac_partner"]["_ip_history"] = (
                    "%s 自动自愈：%s -> %s（伴侣 IP 被路由器重新分配；"
                    "根治需在路由器做 DHCP 静态绑定）"
                    % (datetime.now().strftime("%Y-%m-%d %H:%M"), old, ip)
                )
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                ac_warn("伴侣 IP 自愈成功：%s -> %s（已回写 miio_config.json）" % (old, ip))
        except Exception as e:
            ac_warn("伴侣 IP 自愈：命中 %s 但回写配置失败 %s" % (ip, type(e).__name__))
        return ip

    ac_warn(
        "伴侣 IP 自愈：扫描 %d 台设备，未找到匹配伴侣（可能真离线/断电，需人工排查）"
        % len(candidates)
    )
    return None

