#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Xiaomi Sound TTS 播报 adapter（云端 MiNA）。

供 ac_watch 动作成功后播报「空调已自动开机/关机」；TTS 失败一律静默返回 False，
绝不影响空调控制主链路（command → verify → commit）。

凭据来源（本地已落盘）:
  C:\\Users\\Administrator\\xiaomi_auth.json   micoapi 的 ssecurity/serviceToken
  C:\\Users\\Administrator\\.mi.token           passToken/userId/deviceId（micoapi token 过期时静默刷新）

用法:
  python xiaomi_tts.py "播报文本"   # 直接播报
  python xiaomi_tts.py --test       # 测试播报
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, r"D:\work\ac-advisor\.venv\Lib\site-packages")

import aiohttp

AUTH_FILE = r"C:\Users\Administrator\xiaomi_auth.json"
TOKEN_FILE = r"C:\Users\Administrator\.mi.token"
SOUND_DID = "501560617"          # Xiaomi Sound (miotDID)
SOUND_NAME_HINT = ("sound", "speaker", "小爱")
_USER = "13125554911"
_PASS = "Bo7812700874"
_device_id_cache = None          # 解析出的 Xiaomi Sound deviceID(UUID)，进程内缓存


def _load_creds():
    auth = {}
    if os.path.isfile(AUTH_FILE):
        with open(AUTH_FILE, encoding="utf-8") as f:
            auth = json.load(f)
    tok = {}
    if os.path.isfile(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                tok = json.load(f)
        except Exception:
            tok = {}
    return auth, tok


async def _resolve_sound(na):
    """device_list → 按 miotDID 命中 Xiaomi Sound 的 deviceID(UUID)。失败返回 None。"""
    global _device_id_cache
    if _device_id_cache:
        return _device_id_cache
    try:
        devs = await na.device_list()
    except Exception:
        return None
    for d in devs or []:
        if str(d.get("miotDID", "")) == str(SOUND_DID).strip():
            _device_id_cache = d.get("deviceID")
            return _device_id_cache
    for d in devs or []:
        nm = (d.get("name") or "").lower()
        if any(h in nm for h in SOUND_NAME_HINT) or "小爱" in (d.get("name") or ""):
            _device_id_cache = d.get("deviceID")
            return _device_id_cache
    return None


async def _speak_async(text: str) -> bool:
    auth, tok = _load_creds()
    if not auth.get("ssecurity") or not auth.get("serviceToken"):
        return False
    timeout = aiohttp.ClientTimeout(total=12)
    session = aiohttp.ClientSession(timeout=timeout)
    try:
        from miservice import MiAccount, MiNAService

        acct = MiAccount(session, _USER, _PASS, token_store=TOKEN_FILE, otp_callback=None)
        acct.token = {
            "deviceId": tok.get("deviceId") or "QRLOGIN",
            "userId": auth.get("userId") or tok.get("userId", ""),
            "cUserId": auth.get("cUserId") or tok.get("cUserId", ""),
            "passToken": tok.get("passToken", ""),
            "micoapi": (auth.get("ssecurity", ""), auth.get("serviceToken", "")),
        }
        na = MiNAService(acct)
        device_id = await _resolve_sound(na)
        if not device_id:
            return False
        return await na.text_to_speech(device_id, text)
    finally:
        await session.close()


def speak(text: str) -> bool:
    """播报文本到 Xiaomi Sound。成功 True；任何失败静默返回 False（绝不上抛）。"""
    try:
        return asyncio.run(_speak_async(text))
    except Exception:
        return False


if __name__ == "__main__":
    text = "波哥你好，空调语音提醒系统已接入小米音箱"
    if len(sys.argv) > 1 and sys.argv[1] != "--test":
        text = sys.argv[1]
    ok = speak(text)
    print("✅ 播报成功" if ok else "❌ 播报失败（已静默）")
    sys.exit(0 if ok else 1)
