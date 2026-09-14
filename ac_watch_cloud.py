#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ac_watch_cloud.py —— ac_watch 云化驱动（阶段 3→4：决策 + 云端控制）

原理：ac_watch.main() 通过 `import ac_advisor as A` 读取数据源与控制：
  - A.read_ac_power()   → 功率/伴侣状态（云端）
  - A.read_indoor()     → 净化器温湿度（云端）
  - A.ac_control_init() → 控制对象（云端 CloudACCtrl）
  - verify_socket / verify_target_temp → 走 AC_CTRL.status()（云端回读）

用法：
  python3 ac_watch_cloud.py          # 默认 --dry：只决策不执行
  python3 ac_watch_cloud.py --real   # 真实控制（下发空调命令 + 云端回读验证）
"""
import asyncio
import sys
import os
import time
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ac_advisor as A
import ac_watch
from ac_watch import COMPRESSOR_POWER_THRESHOLD, FAN_ONLY_POWER_MAX
from ac_cloud_backend import _get_service, DID_PURIFIER, DID_AC_PARTNER

# miot 模式映射（siid=2/piid=2）
MODE_INT2STR = {0: "auto", 1: "cool", 2: "dry", 3: "heat", 4: "fan"}
MODE_STR2INT = {"auto": 0, "cool": 1, "dry": 2, "heat": 3, "fan": 4}

# 云端写风控（米家 prop/set 拒绝码 -704042011，读不受影响）：窗口内跳过下发防再触发
CLOUD_WRITE_BLOCKED = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cloud_write_blocked")
CLOUD_WRITE_BLOCK_WINDOW = 1800  # 秒，30 分钟自动恢复


def _mark_write_blocked():
    try:
        with open(CLOUD_WRITE_BLOCKED, "w") as f:
            f.write(datetime.now().isoformat(timespec="seconds"))
    except Exception:
        pass


def cloud_write_blocked():
    """云端写被拒（风控/限流）状态：窗口内返回 True（跳过下发），超时自动恢复。"""
    try:
        with open(CLOUD_WRITE_BLOCKED) as f:
            ts = f.read().strip()
        t0 = datetime.fromisoformat(ts)
        return (datetime.now() - t0).total_seconds() < CLOUD_WRITE_BLOCK_WINDOW
    except Exception:
        return False


def cloud_clear_write_blocked():
    try:
        if os.path.exists(CLOUD_WRITE_BLOCKED):
            os.remove(CLOUD_WRITE_BLOCKED)
    except Exception:
        pass


def _cloud_get(did, iids):
    svc = _get_service()
    return asyncio.run(svc.miot_get_props(did, iids))


def _cloud_set(did, props):
    """云端下发，props=[(siid,piid,value),...]。code 非 0 或异常抛错（对齐 miio 版）。
    -704042011 = 云端写被拒（疑似风控/限流），打标记供决策前跳过下发。"""
    svc = _get_service()
    codes = asyncio.run(svc.miot_set_props(did, props))
    if any(c != 0 for c in codes):
        if any(c == -704042011 for c in codes):
            _mark_write_blocked()
        raise RuntimeError("miot set code=%s" % codes)
    cloud_clear_write_blocked()


class CloudACCtrl:
    """云端空调伴侣控制对象 —— 接口对齐 miio AirConditioningCompanionMcn02：
    send_command(name, [args]) / status()（is_on / mode.value / target_temperature / load_power）。
    _last_set 记录最近一次电源命令，供 verify 按期望等待物理状态到位（绕开云端属性缓存延迟）。
    """

    def __init__(self):
        self._last_set = None

    def status(self):
        vals = _cloud_get(DID_AC_PARTNER, [(2, 1), (2, 3), (5, 1)])
        return SimpleNamespace(
            is_on=vals[0] is True,
            mode=None,  # (2,2) 模式云端读写均不可用，返回 None 让 ac_apply 跳过 set_mode
            target_temperature=vals[1],
            load_power=vals[2],
        )

    def send_command(self, cmd, args):
        if cmd == "set_power":
            _cloud_set(DID_AC_PARTNER, [(2, 1, args[0] == "on")])
            self._last_set = ("power", args[0])
        elif cmd == "set_mode":
            _cloud_set(DID_AC_PARTNER, [(2, 2, MODE_STR2INT[args[0]])])
        elif cmd == "set_tar_temp":
            _cloud_set(DID_AC_PARTNER, [(2, 3, float(args[0]))])
        else:
            raise RuntimeError("unsupported command: %s" % cmd)


def cloud_read_ac_power(timeout=4.0):
    """云端读空调伴侣。语义对齐 miio 版：设置 AC_SOCKET/AC_MEASURED_W/AC_COMPANION_TARGET。
    坑位记录（2026-09-14）：
    - (5,1) 功率曾卡死恒定 97.27W（00:52-15:1x）→ 关机态功率按 None 处理
    - (2,1) 开关位下发后缓存滞后 60-120s（17:2x 实测读到 False 但功率 1011W 在制冷）
    → 交叉校验：开关位=off 但功率>300W 明显矛盾（关了不可能 300W+），判缓存滞后按 on 处理；
      97W 卡死值/39W 启动值均 <300W 不会误判。"""
    try:
        vals = _cloud_get(DID_AC_PARTNER, [(2, 1), (2, 3), (5, 1)])
        switch = vals[0]
        if switch is False and vals[2] and vals[2] > 300:
            A.ac_warn("cloud read_ac_power: (2,1)=off 但功率 %.0fW>300W，判缓存滞后按 on 处理" % vals[2])
            switch = True
        A.AC_SOCKET = "on" if switch is True else ("off" if switch is False else None)
        A.AC_MEASURED_W = None
        A.AC_COMPANION_TARGET = vals[1]
        if A.AC_SOCKET == "on" and vals[2]:
            A.AC_MEASURED_W = round(vals[2])
            return A.AC_MEASURED_W
    except Exception as e:
        A.ac_warn("cloud read_ac_power fail: %s: %s" % (type(e).__name__, str(e)[:120]))
    return None


def cloud_read_indoor(timeout=3.0):
    """云端读净化器温湿度。"""
    try:
        vals = _cloud_get(DID_PURIFIER, [(3, 7), (3, 1)])
        if vals[0] is not None and vals[1] is not None:
            return round(vals[0], 1), round(vals[1], 0)
    except Exception as e:
        A.ac_warn("cloud read_indoor fail: %s: %s" % (type(e).__name__, str(e)[:120]))
    return None, None


def cloud_control_init():
    """云端控制对象（真实控制时使用）。"""
    A.AC_CTRL = CloudACCtrl()


def cloud_verify_socket():
    """云端回读 socket 状态 —— 以开关位 (2,1) 为准（实测准确），功率 (5,1) 仅辅助
    （2026-09-14 起 (5,1) 卡死恒定 97.27W 不可信，不能再用功率物理裁决）。
    注意：(2,1) 属性下发后缓存滞后实测可达 60-120s，等待窗口取 15×8s=120s；
    超时返回 None（无法确认，不误报 on/off，避免污染 state/manual 锚点）。"""
    want = getattr(A.AC_CTRL, "_last_set", None)
    for _ in range(15):
        try:
            s = A.AC_CTRL.status()
            if want and want[0] == "power":
                if want[1] == "on" and s.is_on is True:
                    return "on"
                if want[1] == "off" and s.is_on is False:
                    return "off"
                # 期望命令未到位（下发后属性缓存滞后）：继续等待收敛，不落 no-want 分支
                continue
            # 无期望命令：以开关位为准，读不到再用功率兜底
            if s.is_on is not None:
                return "on" if s.is_on else "off"
            if s.load_power is not None:
                return "on" if s.load_power > 50 else "off"
        except Exception:
            pass
        time.sleep(8)
    return None


def cloud_compressor_state(load_power):
    """云端压缩机状态判定（monkey-patch A.compressor_state）。

    本地 miio 伴侣有真实压缩机状态属性；云端读不到，只能靠功率。
    实测语义：关=2W、低载制冷=97W（变频维持 25°C）、启动=157→1038W。
    50-300W 低载段判 compressor——避免 97W 判 unknown 导致
    compressor_on_min 永不累计、cycle_comp_total 残留，误触发
    WATCH_MAX_RUN=90min 保护强行关机（2026-09-14 01:47 实录：残留
    101.4min 被当连续运行，T=27 时判 off 强制关机）。"""
    if load_power is None:
        return "unknown"
    if load_power > COMPRESSOR_POWER_THRESHOLD:
        return "compressor"
    if load_power > FAN_ONLY_POWER_MAX:
        return "compressor"  # 云端低载段：变频压缩机低载制冷 > 纯风扇
    if load_power > 5:
        return "fan_only"
    return "off"


# ---- monkey-patch 数据源与控制 ----
A.read_ac_power = cloud_read_ac_power
A.read_indoor = cloud_read_indoor
A.ac_control_init = cloud_control_init
A.verify_socket = cloud_verify_socket
ac_watch.compressor_state = cloud_compressor_state  # 定义在 ac_watch 模块内，须 patch ac_watch
A.compressor_state = cloud_compressor_state

if __name__ == "__main__":
    real = "--real" in sys.argv
    # ── 预检：空调伴侣离线（拔电/断电）时跳过本轮决策与控制 ──
    # 全 None = 设备离线（实测：不存在 DID 返回 [None,...]，伴侣在线时
    # 开关位必有 True/False）。离线通常=用户开窗拔掉伴侣，策略不应
    # 尝试控制（会下发失败/误告警），只提示不动作。
    # 注意：云端属性缓存延迟 ~5s，需带重试，避免单 prop 撞旧值误判在线。
    _offline = False
    for _try in range(3):
        try:
            _vals = _cloud_get(DID_AC_PARTNER, [(2, 1), (2, 3), (5, 1)])
            if _vals is None or all(v is None for v in _vals):
                _offline = True
                time.sleep(3)
                continue
            _offline = False  # 开关位有值 → 在线
            break
        except Exception as e:
            _offline = True
            time.sleep(3)
    if _offline:
        print("ac_watch: 空调伴侣离线（可能开窗拔电），跳过本轮决策与控制")
        sys.exit(0)

    # ── 云端写风控检查：prop/set 被拒（-704042011）窗口内跳过下发，防再触发更严风控 ──
    if cloud_write_blocked():
        try:
            with open(CLOUD_WRITE_BLOCKED) as f:
                _t = f.read().strip()
        except Exception:
            _t = "?"
        print("ac_watch: 云端写被拒（疑似风控/限流，%s 起 30 分钟内跳过下发），本轮维持现状" % _t)
        sys.exit(0)

    args = ["ac_watch"] + ([] if real else ["--dry"])
    sys.argv = args
    ac_watch.main()
