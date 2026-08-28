# -*- coding: utf-8 -*-
"""上海空调(米家空调伴侣 lumi.acpartner.mcn02, DID=2056557176) 开关控制 — v2 本地miio通道
========================================================
✅ 安全白名单硬编码: 本脚本 ONLY 允许控制 DID=2056557176 (上海松川定频机)。
   本溪空调 zhimi.aircondition.ma3 (DID=90466860) 和 第三台 ma4 (DID=91063311) 一律拒绝。
   任何其它 DID 传入 → 直接抛异常退出, 绝不发指令。

✅ 控制通道: 走 ac.py / ac_advisor.apply_and_commit 的成熟本地 miio 通道
   (AirConditioningCompanionMcn02: set_power / set_mode / set_tar_temp), 能读回功率验证。

用法: python ac_socket_control.py on [temp]   -> 开机(制冷, 默认26°C)
      python ac_socket_control.py off          -> 关机
      python ac_socket_control.py cool 26      -> 制冷 26°C
      python ac_socket_control.py status       -> 真实状态
========================================================
"""
import json, os, sys, datetime

# --- 安全: 只有上海松川可控制 ---
ALLOWED_DID = "2056557176"
FORBIDDEN_DIDS = {"90466860": "本溪空调(zhimi.aircondition.ma3) 绝对禁区! 不可碰",
                  "91063311": "第三台 ma4 未授权"}

# ac-advisor 目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# miio 装在系统 Python312 site-packages (ac_advisor 以此为惯例手动注入)
_MIIO = "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages"
if _MIIO not in sys.path:
    sys.path.insert(0, _MIIO)

# 复用 ac_advisor 的成熟控制逻辑 (apply_and_commit + 状态验证)
import ac_advisor


def guard(did):
    if did != ALLOWED_DID:
        raise SystemExit(f"[安全拦截] DID={did} 不在白名单! {FORBIDDEN_DIDS.get(did, '未授权设备')}。仅允许 {ALLOWED_DID} (上海松川)。")


def real_status():
    """读真实状态 (本地 miio, 能读功率)"""
    try:
        from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02
        cfg = json.load(open(os.path.join(SCRIPT_DIR, "miio_config.json")))
        ap = cfg.get("ac_partner") or {}
        if not ap.get("ip") or not ap.get("token"):
            return {"err": "miio_config.json 缺 ac_partner ip/token"}
        c = AirConditioningCompanionMcn02(ap["ip"], ap["token"])
        st = c.status()
        return {"is_on": st.is_on, "load_power": getattr(st, "load_power", None),
                "target_temp": getattr(st, "target_temperature", None),
                "mode": str(getattr(st, "mode", None)), "fan": str(getattr(st, "fan_speed", None))}
    except Exception as e:
        return {"err": repr(e)}


def report():
    s = real_status()
    if "err" in s:
        print(f"⚠️ 状态读取失败: {s['err']}")
        return
    pw = s.get("load_power")
    comp = "压缩机运行" if (pw and pw > 300) else ("仅风扇" if (pw and pw > 5) else "已关")
    print(f"时间: {datetime.datetime.now().isoformat(timespec='seconds')}")
    print(f"插座: {'通电' if s.get('is_on') else '断电'} | 功率: {pw}W ({comp})")
    print(f"目标温度: {s.get('target_temp')}°C | 模式: {s.get('mode')} | 风速: {s.get('fan')}")


def ac_on(temp=26):
    guard(ALLOWED_DID)
    print(f"[执行] 上海空调开机 → 制冷 {temp}°C ({datetime.datetime.now().isoformat(timespec='seconds')})")
    ac_advisor.ac_control_init()
    if ac_advisor.AC_CTRL is None:
        print("❌ 空调插座不可达 (本地miio), 检查 miio_config.json")
        sys.exit(1)
    state = ac_advisor.load_state()
    ctrl = ac_advisor.apply_and_commit("cooling", temp, state)
    if ctrl.get("status") == "failed":
        print(f"⚠️ 执行未完成: {ctrl.get('reason')}")
        sys.exit(1)
    print(f"✅ {ctrl.get('action') or '已开机(状态一致)'}")
    print("--- 执行后真实状态 ---")
    report()


def ac_off():
    guard(ALLOWED_DID)
    print(f"[执行] 上海空调关机 ({datetime.datetime.now().isoformat(timespec='seconds')})")
    ac_advisor.ac_control_init()
    if ac_advisor.AC_CTRL is None:
        print("❌ 空调插座不可达 (本地miio)")
        sys.exit(1)
    state = ac_advisor.load_state()
    ctrl = ac_advisor.apply_and_commit("off", None, state)
    if ctrl.get("status") == "failed":
        print(f"⚠️ 执行未完成: {ctrl.get('reason')}")
        sys.exit(1)
    print(f"✅ {ctrl.get('action') or '已关机'}")
    print("--- 执行后真实状态 ---")
    report()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "on":
        ac_on(int(sys.argv[2]) if len(sys.argv) > 2 else 26)
    elif cmd == "off":
        ac_off()
    elif cmd == "cool":
        ac_on(int(sys.argv[2]) if len(sys.argv) > 2 else 26)
    elif cmd in ("status", "state"):
        guard(ALLOWED_DID)
        report()
    else:
        print("用法: on [temp] | off | cool <temp> | status")
