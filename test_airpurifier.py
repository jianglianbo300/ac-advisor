#!/usr/bin/env python3
import sys; sys.path.insert(0, 'C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages')
from miio import Device
import json

d = Device('192.168.71.120', 'c12622c390a94c90e25083e54d36ace0', timeout=5)

# 用 miio 的 AirPurifier 类，它知道正确属性映射
try:
    from miio.airpurifier import AirPurifier
    ap = AirPurifier('192.168.71.120', 'c12622c390a94c90e25083e54d36ace0', timeout=5)
    status = ap.status()
    print("AirPurifier.status():")
    print(status)
    # 尝试直接读 humidity 属性
    for attr in ['humidity', 'temperature', 'relative_humidity']:
        if hasattr(status, attr):
            print(f"  {attr} = {getattr(status, attr)}")
except Exception as e:
    print(f"AirPurifier err: {e}")