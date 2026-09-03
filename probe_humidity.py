#!/usr/bin/env python3
import sys

sys.path.insert(
    0,
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
)
import json

from miio import Device

d = Device("192.168.71.120", "c12622c390a94c90e25083e54d36ace0", timeout=5)

# 查 rma3 的 MIoT 规格（在线）
# 先读设备 spec
try:
    r = d.send("get_device_prop", [])
    print("device_prop:", json.dumps(r, indent=2)[:500])
except Exception as e:
    print(f"device_prop err: {e}")

# 读所有已知属性
print("\n=== 全量读数 ===")
for siid, piid, name in [
    (3, 1, "AQI"),
    (3, 2, "空气"),
    (3, 3, "空气"),
    (3, 4, "空气"),
    (3, 5, "空气"),
    (3, 6, "AQI"),
    (3, 7, "温度"),
    (3, 8, "温度2"),
    (3, 9, "环境"),
    (3, 10, "环境"),
    (4, 1, "湿度"),
    (4, 2, "湿度2"),
    (4, 3, "气压"),
    (4, 4, "其他"),
    (4, 5, "其他"),
    (2, 1, "开关"),
    (2, 2, "电源"),
    (2, 3, "模式"),
    (2, 4, "风速"),
    (2, 5, "模式2"),
]:
    try:
        r = d.send("get_properties", [{"siid": siid, "piid": piid}])
        if r and isinstance(r[0], dict) and r[0].get("code") == 0:
            v = r[0]["value"]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                print(f"siid={siid} piid={piid} ({name:6s}) = {v}")
    except Exception:
        pass
