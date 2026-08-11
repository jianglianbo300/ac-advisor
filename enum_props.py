#!/usr/bin/env python3
import sys; sys.path.insert(0, 'C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages')
from miio import Device
import json

d = Device('192.168.71.120', 'c12622c390a94c90e25083e54d36ace0', timeout=5)

# 枚举所有可能的 siid/piid 组合
results = []
for siid in range(1, 20):
    for piid in range(1, 20):
        try:
            r = d.send("get_properties", [{"siid": siid, "piid": piid}])
            if isinstance(r, list) and len(r) > 0:
                v = r[0]
                if isinstance(v, dict) and v.get("code") == 0 and v.get("value") is not None:
                    val = v["value"]
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        results.append((siid, piid, val))
        except Exception:
            pass

# 按 siid/piid 排序输出
for siid, piid, val in sorted(results, key=lambda x: (x[0], x[1])):
    print(f"siid={siid:2d}  piid={piid:2d}  value={val}")