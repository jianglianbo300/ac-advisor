#!/usr/bin/env python3
import asyncio
from miio import Device
from miio.miioprotocol import MiIOProtocol

IP = "192.168.71.120"
TOKEN = "<REDACTED-miio-token>  # 原明文 token 因公开仓库泄漏已移除，改从环境变量 MIIO_TOKEN 读取"

async def main():
    # 直接走协议层，看原始响应
    proto = MiIOProtocol(IP, TOKEN, timeout=10)
    try:
        header, payload = await proto.send("get_prop", ["temp_dec", "humidity"])
        print("OK:", payload)
    except Exception as e:
        print("ERR:", e)

asyncio.run(main())