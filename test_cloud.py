#!/usr/bin/env python3
# 米家云端读取温湿度（绕过局域网UDP防火墙问题）
# 需要小米账号 + 设备did + token
import asyncio
from miio.miotcloud import MiotCloud

# 这些从 Xiaomi Cloud Token 工具拿
USERNAME = "你的小米账号"
PASSWORD = "你的密码"
SERVER = "cn"

# 净化器
PURIFIER_DID = "875028325"  # 从设备清单的 ID 字段
PURIFIER_TOKEN = "c12622c390a94c90e25083e54d36ace0"

# 温湿度计（BLE，只能云端读）
HT2_DID = "blt.3.1codkci7kec00"
HT2_TOKEN = "79181c7220ed05c3c848cb81"

async def main():
    cloud = MiotCloud(USERNAME, PASSWORD, SERVER)
    await cloud.login()

    # 读净化器属性
    r = await cloud.get_props([
        {"did": PURIFIER_DID, "piid": 1, "siid": 2},  # 温度
    ])
    print("purifier:", r)

asyncio.run(main())