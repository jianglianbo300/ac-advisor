#!/usr/bin/env python3
import asyncio
from miio import Device
from miio.miioprotocol import MiIOProtocol

IP = "192.168.71.120"
TOKEN = "c12622c390a94c90e25083e54d36ace0"

async def main():
    # 直接走协议层，看原始响应
    proto = MiIOProtocol(IP, TOKEN, timeout=10)
    try:
        header, payload = await proto.send("get_prop", ["temp_dec", "humidity"])
        print("OK:", payload)
    except Exception as e:
        print("ERR:", e)

asyncio.run(main())