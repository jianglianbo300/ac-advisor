#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""miservice 注入测试：用扫码登录态读净化器温湿度"""
import asyncio
import json
import os
import secrets

from miservice.miaccount import MiAccount
from miservice.miioservice import MiIOService


def build_token(s):
    dev_id = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16))
    return {
        "deviceId": dev_id,
        "userId": s["user_id"],
        "passToken": s["pass_token"],
        "ssecurity": s["ssecurity"],
        "xiaomiio": [s["ssecurity"], s["service_token"]],
    }


async def main():
    s = json.load(open(os.path.expanduser("~/.ac_cloud_secrets.json")))
    account = MiAccount(None, "", "")
    account.token = build_token(s)
    svc = MiIOService(account)
    # 净化器 875028325 温湿度 (siid=3: piid=7 温度, piid=1 湿度)
    try:
        vals = await svc.miot_get_props("875028325", [(3, 7), (3, 1)])
        print("净化器温湿度:", vals)
    except Exception as e:
        print("读净化器失败:", type(e).__name__, str(e)[:200])
    # 空调伴侣（用户已拔，预期失败/离线，验证错误可观测）
    try:
        vals = await svc.miot_get_props("2056557176", [(2, 1)])
        print("空调伴侣电源:", vals)
    except Exception as e:
        print("读空调伴侣失败(预期离线):", type(e).__name__, str(e)[:200])


if __name__ == "__main__":
    asyncio.run(main())
