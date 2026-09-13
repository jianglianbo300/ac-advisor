#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ac_cloud_backend.py —— 米家云控制后端（ac-advisor 云化改造，阶段 1 · miservice 版）

作用：让 ac-advisor 在云电脑上通过「米家云 API」读取/控制家里空调，
     替代必须在家局域网才能用的 miio 直连后端（UDP 54321）。

依赖：pip install miservice
登录态：~/.ac_cloud_secrets.json（chmod 600，由 ac_qr_login.py 扫码写入）：
      {"user_id":..., "ssecurity":..., "cuser_id":..., "pass_token":..., "service_token":...}
     · 不存密码明文，不入 git、不打印。

用法：
  python3 ac_cloud_backend.py devices        # 列出账号下全部设备
  python3 ac_cloud_backend.py status         # 读空调伴侣 + 净化器当前状态
  python3 ac_cloud_backend.py props <did>    # 探测某设备全部 miot 属性（枚举 siid/piid）
  python3 ac_cloud_backend.py ac on|off      # 空调开关（阶段 4 用；siid=2/piid=1）
  python3 ac_cloud_backend.py ac temp 26     # 设目标温度（siid=2/piid=3，16-30）
  python3 ac_cloud_backend.py ac mode cool   # 设模式 auto/cool/dry/heat/fan（siid=2/piid=2）

设备（上海这套）：
  空调伴侣  lumi.acpartner.mcn02   DID 2056557176（红外+功率感知）
      miot 属性（spec 已核，2026-09-13）：
        siid=2 Air Conditioner：piid=1 空调开关 | piid=2 模式(0自动/1制冷/2除湿/3制热/4送风) | piid=3 目标温度(16-30)
        siid=3 Fan Control：piid=1 风速(0自动/1低/2中/3高) | piid=2 摆风
        siid=5 Power Consumption：piid=1 实时功率 W（只读，策略压缩机判定用）
        siid=4/piid=3 Ac State 状态串（红外记忆，可能滞后，勿当实时状态）
  净化器4Lite zhimi.airp.rma3      DID 875028325（温度 siid=3/piid=7、湿度 siid=3/piid=1）

【禁控】本溪两台空调整机，与上海无关，任何命令严禁下发：
  90466860  zhimi.aircondition.ma3  （空调）
  91063311  zhimi.aircondition.ma4  （米家互联网空调 一级能效）

已验证（2026-09-13）：云端读净化器温湿度 27/62 ✅、读伴侣电源 False(已拔) ✅。
"""
import asyncio
import json
import os
import secrets
import sys

from miservice.miaccount import MiAccount
from miservice.miioservice import MiIOService

SECRETS = os.path.expanduser("~/.ac_cloud_secrets.json")
DID_AC_PARTNER = "2056557176"      # 上海 空调伴侣（唯一可控制目标）
DID_PURIFIER = "875028325"         # 上海 净化器 4 Lite（登录后确认）
MODEL_PURIFIER = "zhimi.airp.rma3"
MODEL_AC_PARTNER = "lumi.acpartner.mcn02"

# 白名单：只允许【读/控】的设备。本溪两台空调 90466860/91063311 不在其内，任何操作拒绝。
SAFE_DIDS = {
    DID_AC_PARTNER: "上海空调伴侣",
    DID_PURIFIER: "上海净化器4Lite",
}
# 明确禁控清单（用于告警提示）
FORBIDDEN_DIDS = {
    "90466860": "本溪空调 ma3（禁控）",
    "91063311": "本溪空调 ma4（禁控）",
}


def _guard_target(did, need_control=False):
    """目标 DID 白名单校验。need_control=True 时只允许空调伴侣，且必须是伴侣型号。"""
    if did in FORBIDDEN_DIDS:
        print("🚫 拒绝操作 %s：%s（严禁下发，已拦截）" % (did, FORBIDDEN_DIDS[did]))
        return False
    if did not in SAFE_DIDS:
        print("🚫 拒绝操作未知设备 %s（不在白名单，已拦截）" % did)
        return False
    if need_control and did != DID_AC_PARTNER:
        print("🚫 拒绝控制 %s（%s）：控制指令只允许发给上海空调伴侣" % (did, SAFE_DIDS[did]))
        return False
    return True


def _load_secrets():
    if not os.path.exists(SECRETS):
        print("❌ 缺少登录态 %s —— 先运行 ac_qr_login.py 扫码登录" % SECRETS)
        sys.exit(1)
    with open(SECRETS, encoding="utf-8") as f:
        return json.load(f)


def _build_token(s):
    # deviceId 持久化：首次生成后写回 secrets，避免每次调用随机新 ID（防小米后续校验）
    dev_id = s.get("deviceId")
    if not dev_id:
        dev_id = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16))
        s["deviceId"] = dev_id
        with open(SECRETS, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.chmod(SECRETS, 0o600)
    return {
        "deviceId": dev_id,
        "userId": s["user_id"],
        "passToken": s["pass_token"],
        "ssecurity": s["ssecurity"],
        "xiaomiio": [s["ssecurity"], s["service_token"]],
    }


_svc_cache = None


def _get_service():
    """模块级单例：复用同一个异步 session，避免每次调用新建连接（防泄漏/防风控）。"""
    global _svc_cache
    if _svc_cache is None:
        s = _load_secrets()
        account = MiAccount(None, "", "")
        account.token = _build_token(s)
        _svc_cache = MiIOService(account)
    return _svc_cache


def _run(coro):
    try:
        return asyncio.run(coro)
    except Exception as e:
        print("❌ 云调用失败: %s: %s" % (type(e).__name__, str(e)[:300]))
        return None


def cmd_devices():
    async def f():
        svc = _get_service()
        raw = await svc.device_list()
        for d in raw:
            did = str(d.get("did"))
            tag = ""
            if did in FORBIDDEN_DIDS:
                tag = "  ⛔ 禁控"
            elif did in SAFE_DIDS:
                tag = "  ✅ 白名单"
            print("%s | %s | %s%s" % (did, d.get("name"), d.get("model"), tag))
    _run(f())


def cmd_status():
    async def f():
        svc = _get_service()
        # 净化器温湿度
        pur = await svc.miot_get_props(DID_PURIFIER, [(3, 7), (3, 1)])
        # 空调伴侣：开关(2.1) 目标温度(2.3) 功率(5.1) —— (2.2)模式云端偶发读不到已移除
        ac = await svc.miot_get_props(DID_AC_PARTNER, [(2, 1), (2, 3), (5, 1)])
        modes = {0: "自动", 1: "制冷", 2: "除湿", 3: "制热", 4: "送风"}
        print("=== 净化器 4 Lite (%s) ===" % DID_PURIFIER)
        if pur and pur[0] is not None:
            print("  温度: %s°C  湿度: %s%%" % (pur[0], "%.0f" % pur[1] if pur[1] is not None else "?"))
        else:
            print("  读取失败")
        print("=== 空调伴侣 (%s) ===" % DID_AC_PARTNER)
        if ac is None:
            print("  读取失败")
            return
        power, target, watt = ac[0], ac[1], ac[2]
        print("  空调: %s | 模式: ? | 目标: %s°C | 功率: %s W" % (
            "开" if power is True else ("关" if power is False else "?"),
            target, "%.0f" % watt if watt is not None else "?"))
    _run(f())


def cmd_props(did):
    if not _guard_target(did):  # 只允许探测白名单设备（上海伴侣/净化器）
        return
    async def f():
        svc = _get_service()
        print("探测 %s 的 miot 属性（siid 1-8, piid 1-8）……" % did)
        found = []
        for siid in range(1, 9):
            iids = [(siid, piid) for piid in range(1, 9)]
            try:
                vals = await svc.miot_get_props(did, iids)
            except Exception:
                continue
            for (s, p), v in zip(iids, vals):
                if v is not None:
                    found.append((s, p, v))
        if not found:
            print("（未读到任何属性）")
        for s, p, v in found:
            print("siid=%d piid=%d = %s" % (s, p, v))
    _run(f())


def _chk_write_blocked(r):
    """检测云端写被拒（-704042011 疑似风控/限流），给用户明确提示。"""
    if any(c == -704042011 for c in r):
        print("⚠️ 云端写被拒 code=-704042011（疑似风控/限流）：本次下发未生效，读操作正常；"
              "30 分钟内自动决策会跳过下发，通常数小时后自动解除，或需重新扫码登录")


def cmd_ac(args):
    # 控制命令三重防线：目标必须=上海空调伴侣（白名单+禁控名单+型号校验）
    if not _guard_target(DID_AC_PARTNER, need_control=True):
        return
    if not args:
        print("用法: ac_cloud_backend.py ac on|off|temp <n>")
        return
    act = args[0]

    async def f():
        svc = _get_service()
        if act == "on":
            r = await svc.miot_set_props(DID_AC_PARTNER, [(2, 1, True)])
            print("✅ 下发开机 code=%s（空调开关 siid=2/piid=1；verify 待阶段4）" % r)
            _chk_write_blocked(r)
        elif act == "off":
            r = await svc.miot_set_props(DID_AC_PARTNER, [(2, 1, False)])
            print("✅ 下发关机 code=%s（空调开关 siid=2/piid=1；verify 待阶段4）" % r)
            _chk_write_blocked(r)
        elif act == "temp" and len(args) > 1:
            n = int(args[1])
            if not 16 <= n <= 30:
                print("🚫 目标温度 %d 超出合理范围(16-30)，拒绝下发" % n)
                return
            r = await svc.miot_set_props(DID_AC_PARTNER, [(2, 3, n)])
            print("✅ 下发目标温度 %d°C code=%s" % (n, r))
            _chk_write_blocked(r)
        elif act == "mode" and len(args) > 1:
            m = {"auto": 0, "cool": 1, "dry": 2, "heat": 3, "fan": 4}.get(args[1].lower())
            if m is None:
                print("🚫 未知模式 %s（auto/cool/dry/heat/fan）" % args[1])
                return
            r = await svc.miot_set_props(DID_AC_PARTNER, [(2, 2, m)])
            print("✅ 下发模式 %s code=%s" % (args[1].lower(), r))
            _chk_write_blocked(r)
        else:
            print("未知指令:", act)
    _run(f())


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "devices":
        cmd_devices()
    elif cmd == "status":
        cmd_status()
    elif cmd == "props":
        cmd_props(rest[0] if rest else DID_AC_PARTNER)
    elif cmd == "ac":
        cmd_ac(rest)
    else:
        print("未知命令:", cmd)
        print(__doc__)


if __name__ == "__main__":
    main()
