#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米云 QR 扫码登录 + Xiaomi Sound TTS 验证（正式工具，无短信验证码）。

用途：`xiaomi_tts.py` / `ac_watch.py` 依赖的云端凭据（micoapi serviceToken）过期时，
一键重新登录。**无需短信验证码**——手机米家 App 扫码确认即可（小米 2FA 已灰度切换
到 userCross 新流程，旧 /identity/* 短信流程 10012 失效，QR 登录是唯一干净路径，
详见 AGENTS.md「Xiaomi Sound TTS 语音接入专项」）。

流程（复用 XiaomiTokenTool 的 QrCodeXiaomiCloudConnector）：
  1. longPolling/loginUrl -> 取 QR 图片 URL + 长轮询 URL（响应带 &&&START&&& 前缀需剥离）
  2. 保存并打开 QR 图片，等用户用米家 App 扫码确认
  3. 长轮询返回 userId/ssecurity/cUserId/passToken/location
  4. 跟随 location -> xiaomiio serviceToken
  5. 用 passToken 静默登录 micoapi -> micoapi serviceToken（无验证码）
  6. 落盘凭据 -> MiNA device_list -> 解析 Xiaomi Sound -> TTS 实播验证

写入的凭据：
  C:\\Users\\Administrator\\xiaomi_auth.json   micoapi ssecurity/serviceToken + userId/cUserId
  C:\\Users\\Administrator\\.mi.token           passToken/userId/cUserId/deviceId + micoapi
  本目录 qr_session.json                       扫码会话缓存（10 分钟内免重复扫码，含 passToken，已 gitignore）

用法:
  python qr_login_tts.py           # 正常登录（10 分钟内复用缓存会话）
  python qr_login_tts.py --force   # 强制重新扫码（即使缓存会话还在有效期内）
  python qr_login_tts.py --no-tts  # 只刷新凭据，不做 TTS 验证（音箱不可达时用）
"""
import asyncio
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Lib", "site-packages"))

import requests
import aiohttp
from miservice import MiAccount, MiNAService

TOKEN_FILE = r"C:\Users\Administrator\.mi.token"
AUTH_FILE = r"C:\Users\Administrator\xiaomi_auth.json"
QR_FILE = r"C:\Users\Administrator\Desktop\mi_qr_login.png"
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_session.json")
SOUND_DID = "501560617"          # Xiaomi Sound (miotDID)
SESSION_TTL = 600                # 扫码会话缓存有效期（秒）
_USER = "13125554911"
_PASS = "Bo7812700874"

# aiohttp cookie 白名单（避免畸形 cookie 名触发 CookieError）
COOKIE_WHITELIST = {"passToken", "userId", "cUserId", "deviceId", "sdkVersion",
                    "pass_ua", "uLocale", "passInfo", "pass_sign"}


def _strip_prefix(text: str) -> str:
    if text.startswith("&&&START&&&"):
        return text[len("&&&START&&&"):]
    return text


def qr_login() -> dict:
    """QR 登录，返回 {passToken, userId, cUserId, ssecurity, location, serviceToken, cookies, ts}。"""
    s = requests.Session()
    # Step 1: 获取 QR 信息
    url = "https://account.xiaomi.com/longPolling/loginUrl"
    data = {
        "_qrsize": "480",
        "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
        "callback": "https://sts.api.io.mi.com/sts",
        "_hasLogo": "false",
        "sid": "xiaomiio",
        "serviceParam": "",
        "_locale": "zh_CN",
        "_dc": str(int(time.time() * 1000)),
    }
    r = s.get(url, params=data, timeout=20)
    r.raise_for_status()
    d = json.loads(_strip_prefix(r.text))
    qr_url = d["qr"]
    lp_url = d["lp"]
    timeout = d["timeout"]
    print(f"[1] 已获取二维码 URL，长轮询超时 {timeout}s")

    # Step 2: 下载并打开二维码
    r = s.get(qr_url, timeout=20)
    with open(QR_FILE, "wb") as f:
        f.write(r.content)
    print(f"[2] 二维码已保存: {QR_FILE}（正在打开，请用手机米家 App 扫码确认）")
    os.startfile(QR_FILE)

    # Step 3: 长轮询等待扫码
    print("[3] 等待手机扫码确认...")
    start = time.time()
    while True:
        try:
            r = s.get(lp_url, timeout=10)
            if r.status_code == 200:
                break
        except requests.exceptions.Timeout:
            if time.time() - start > timeout:
                raise Exception("扫码超时（二维码已过期，请重新运行）")
            continue
        except Exception as e:
            print("  长轮询异常:", e)
            if time.time() - start > timeout:
                raise Exception("扫码超时（二维码已过期，请重新运行）")
    d = json.loads(_strip_prefix(r.text))
    print("[3] 扫码确认成功！")
    ck = {
        "userId": d["userId"],
        "ssecurity": d["ssecurity"],
        "cUserId": d["cUserId"],
        "passToken": d["passToken"],
        "location": d["location"],
    }
    print(f"    userId={ck['userId']} passToken={ck['passToken'][:20]}...")

    # Step 4: 跟随 location 拿 xiaomiio serviceToken
    r = s.get(ck["location"], headers={"content-type": "application/x-www-form-urlencoded"}, timeout=20)
    st = s.cookies.get("serviceToken")
    if not st:
        raise Exception("未拿到 xiaomiio serviceToken")
    ck["serviceToken"] = st
    print(f"[4] xiaomiio serviceToken={st[:20]}...")

    # 记录 session cookies（白名单过滤），缓存到项目目录
    ck["cookies"] = {c.name: c.value for c in s.cookies if c.name in COOKIE_WHITELIST}
    ck["ts"] = time.time()
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(ck, f, ensure_ascii=False, indent=2)
    print(f"    QR 会话已缓存: {SESSION_FILE}")
    return ck


async def _main(force: bool, do_tts: bool) -> int:
    # 优先复用 TTL 内的扫码会话，避免重复扫码
    ck = None
    if not force and os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            if time.time() - saved.get("ts", 0) < SESSION_TTL:
                ck = saved
                print(f"复用已缓存的 QR 会话（{SESSION_TTL // 60} 分钟内有效）")
        except Exception:
            pass
    if ck is None:
        try:
            ck = qr_login()
        except Exception as e:
            print("QR 登录失败:", type(e).__name__, str(e)[:300])
            return 1

    session = aiohttp.ClientSession()
    try:
        from yarl import URL as YURL
        session.cookie_jar.update_cookies(ck["cookies"], response_url=YURL("https://account.xiaomi.com/"))

        acct = MiAccount(session, _USER, _PASS, token_store=TOKEN_FILE, otp_callback=None)
        acct.token = {
            "deviceId": ck["cookies"].get("deviceId", "QRLOGIN"),
            "userId": ck["userId"],
            "passToken": ck["passToken"],
            "cUserId": ck["cUserId"],
        }

        print("静默登录 micoapi（passToken 复用，无验证码）...")
        ok = await acct.login("micoapi")
        print(f"login('micoapi') 返回: {ok}")
        if not ok:
            print("登录失败:", acct._login_error)
            return 1

        tok = acct.token or {}
        xa = tok.get("micoapi", ("", ""))
        auth = {
            "ssecurity": xa[0],
            "serviceToken": xa[1],
            "userId": tok.get("userId", ""),
            "cUserId": tok.get("cUserId", ""),
        }
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(auth, f, ensure_ascii=False, indent=2)
        print(f"✅ xiaomi_auth.json 已更新（micoapi 凭据，serviceToken={xa[1][:20]}...）")
        print(f"✅ .mi.token 已更新（passToken + micoapi，token_store 写入）")
        print(f"   AUTH_FILE={AUTH_FILE}\n   TOKEN_FILE={TOKEN_FILE}")

        if not do_tts:
            print("\n（--no-tts，跳过 TTS 验证）")
            return 0

        na = MiNAService(acct)
        try:
            devs = await na.device_list()
            print("\n设备列表:")
            for d in devs or []:
                print(f"  {d.get('name')} | deviceID={d.get('deviceID')} | miotDID={d.get('miotDID')}")
        except Exception as e:
            print(f"device_list 异常: {type(e).__name__}: {str(e)[:300]}")
            devs = None

        target = None
        for d in devs or []:
            if str(d.get("miotDID", "")) == str(SOUND_DID).strip():
                target = d.get("deviceID")
                print(f"\n命中 Xiaomi Sound: deviceID={target}")
                break
        if not target:
            for d in devs or []:
                nm = (d.get("name") or "").lower()
                if "sound" in nm or "speaker" in nm or "小爱" in (d.get("name") or ""):
                    target = d.get("deviceID")
                    print(f"\n按名称回退: {d.get('name')} deviceID={target}")
                    break
        if not target:
            print("未找到 Xiaomi Sound 设备（凭据已刷新，TTS 验证跳过）")
            return 0

        try:
            r = await na.text_to_speech(target, "波哥你好，空调语音提醒系统已重新登录成功")
            print(f"TTS 返回: {r}")
            if r:
                print("✅ TTS 播报指令已发送，请听音箱！")
        except Exception as e:
            print(f"TTS 异常: {type(e).__name__}: {str(e)[:300]}（凭据已刷新，可稍后手动验证）")
        return 0
    except Exception as e:
        print("异常:", type(e).__name__, str(e)[:300])
        traceback.print_exc()
        return 1
    finally:
        await session.close()


if __name__ == "__main__":
    force = "--force" in sys.argv
    do_tts = "--no-tts" not in sys.argv
    sys.exit(asyncio.run(_main(force, do_tts)))
