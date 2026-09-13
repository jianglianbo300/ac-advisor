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
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ac_advisor as A
import ac_watch
from ac_cloud_backend import _get_service, DID_PURIFIER, DID_AC_PARTNER

# miot 模式映射（siid=2/piid=2）
MODE_INT2STR = {0: "auto", 1: "cool", 2: "dry", 3: "heat", 4: "fan"}
MODE_STR2INT = {"auto": 0, "cool": 1, "dry": 2, "heat": 3, "fan": 4}


def _cloud_get(did, iids):
    svc = _get_service()
    return asyncio.run(svc.miot_get_props(did, iids))


def _cloud_set(did, props):
    """云端下发，props=[(siid,piid,value),...]。code 非 0 或异常抛错（对齐 miio 版）。"""
    svc = _get_service()
    codes = asyncio.run(svc.miot_set_props(did, props))
    if any(c != 0 for c in codes):
        raise RuntimeError("miot set code=%s" % codes)


class CloudACCtrl:
    """云端空调伴侣控制对象 —— 接口对齐 miio AirConditioningCompanionMcn02：
    send_command(name, [args]) / status()（is_on / mode.value / target_temperature / load_power）。
    """

    def status(self):
        vals = _cloud_get(DID_AC_PARTNER, [(2, 1), (2, 2), (2, 3), (5, 1)])
        return SimpleNamespace(
            is_on=vals[0] is True,
            mode=SimpleNamespace(value=MODE_INT2STR.get(vals[1])),
            target_temperature=vals[2],
            load_power=vals[3],
        )

    def send_command(self, cmd, args):
        if cmd == "set_power":
            _cloud_set(DID_AC_PARTNER, [(2, 1, args[0] == "on")])
        elif cmd == "set_mode":
            _cloud_set(DID_AC_PARTNER, [(2, 2, MODE_STR2INT[args[0]])])
        elif cmd == "set_tar_temp":
            _cloud_set(DID_AC_PARTNER, [(2, 3, float(args[0]))])
        else:
            raise RuntimeError("unsupported command: %s" % cmd)


def cloud_read_ac_power(timeout=4.0):
    """云端读空调伴侣。语义对齐 miio 版：设置 AC_SOCKET/AC_MEASURED_W/AC_COMPANION_TARGET。"""
    try:
        vals = _cloud_get(DID_AC_PARTNER, [(2, 1), (2, 2), (2, 3), (5, 1)])
        A.AC_SOCKET = "on" if vals[0] is True else ("off" if vals[0] is False else None)
        A.AC_MEASURED_W = None
        A.AC_COMPANION_TARGET = vals[2]
        if vals[3]:
            A.AC_MEASURED_W = round(vals[3])
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
    """云端回读 socket 状态（带重试，容忍云端属性缓存延迟 ~5s）。"""
    for _ in range(4):
        try:
            s = A.AC_CTRL.status()
            v = "on" if s.is_on else "off"
            if v in ("on", "off"):
                return v
        except Exception:
            pass
        time.sleep(3)
    return None


# ---- monkey-patch 数据源与控制 ----
A.read_ac_power = cloud_read_ac_power
A.read_indoor = cloud_read_indoor
A.ac_control_init = cloud_control_init
A.verify_socket = cloud_verify_socket

if __name__ == "__main__":
    real = "--real" in sys.argv
    args = ["ac_watch"] + ([] if real else ["--dry"])
    sys.argv = args
    ac_watch.main()
