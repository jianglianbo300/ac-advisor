#!/usr/bin/env python3
"""米家云端读取温湿度（绕过局域网UDP防火墙问题）"""
try:
    from miio.miotcloud import MiotCloud
except (ImportError, ModuleNotFoundError):
    print("SKIP: miio.miotcloud 未安装（0.5.12 --no-deps 装法无 cloud 模块）")
    import sys; sys.exit(0)

import asyncio

# 这些从 Xiaomi Cloud Token 工具拿
USERNAME = "13125554911"
PASSWORD = "Bo7812700874"
SERVER = "cn"

PURIFIER_DID = "875028325"
PURIFIER_TOKEN = "<REDACTED-miio-token>  # 原明文 token 因公开仓库泄漏已移除，改从环境变量 MIIO_TOKEN 读取"

async def main():
    mc = MiCloud(USERNAME, PASSWORD, SERVER)
    await mc.login()
    devices = await mc.get_devices()
    print(f"云端设备数: {len(devices)}")

asyncio.run(main())
