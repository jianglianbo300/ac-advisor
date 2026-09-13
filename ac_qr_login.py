#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ac_qr_login.py —— 米家账号「扫码登录」（云电脑版，无 GUI）

流程（参考 xiaomi-cloud-tokens-extractor v1.5.0 QR 实现）：
  1. GET account.xiaomi.com/longPolling/loginUrl  → 拿二维码图片URL + 长轮询URL
  2. 下载二维码 → 保存 qr_login.png（给用户手机米家App扫）
  3. 长轮询等扫码确认 → 拿 userId/ssecurity/passToken
  4. 访问 location → 拿 serviceToken
  5. 全部登录态写入 ~/.ac_cloud_secrets.json（chmod 600）

用法：
  python3 ac_qr_login.py            # 交互式扫码登录（默认）
  python3 ac_qr_login.py status     # 显示当前登录态是否有效
"""
import json
import os
import sys
import time

import requests

SECRETS = os.path.expanduser("~/.ac_cloud_secrets.json")
QR_PNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_login.png")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="mi.com")
    s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="xiaomi.com")
    dev_id = "".join(__import__("random").choices("abcdefghijklmnopqrstuvwxyz", k=6))
    s.cookies.set("deviceId", dev_id, domain="mi.com")
    s.cookies.set("deviceId", dev_id, domain="xiaomi.com")
    return s


def _json(text):
    try:
        return json.loads(text.replace("&&&START&&&", ""))
    except (ValueError, TypeError):
        return None


def qr_login(waiter=None):
    """执行扫码登录。waiter 可选：每轮询一次调用一次（用于向外部汇报进度）。"""
    s = _session()

    # Step 1: 获取二维码信息
    url = "https://account.xiaomi.com/longPolling/loginUrl"
    params = {
        "_qrsize": "480",
        "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
        "callback": "https://sts.api.io.mi.com/sts",
        "_hasLogo": "false",
        "sid": "xiaomiio",
        "serviceParam": "",
        "_locale": "zh_CN",
        "_dc": str(int(time.time() * 1000)),
    }
    r = s.get(url, params=params, timeout=15)
    if r.status_code != 200:
        print("❌ Step1 失败 HTTP %d: %s" % (r.status_code, r.text[:300]))
        return False
    data = _json(r.text)
    if data is None or data.get("code") != 0:
        print("❌ Step1 返回异常 code=%s: %s" % (data and data.get("code"), (r.text or "")[:300]))
        return False
    qr_url = data["qr"]
    lp_url = data["lp"]
    timeout = int(data.get("timeout", 300))
    login_url = data.get("loginUrl", "")
    print("✅ 二维码已获取（有效 %d 秒），等待手机扫码……" % timeout)
    if login_url:
        print("   或浏览器打开（不推荐）: %s" % login_url)

    # Step 2: 下载二维码图片
    img = s.get(qr_url, timeout=15)
    if img.status_code != 200:
        print("❌ Step2 二维码下载失败 HTTP %d" % img.status_code)
        return False
    with open(QR_PNG, "wb") as f:
        f.write(img.content)
    print("✅ 二维码已保存: %s （%d 字节）" % (QR_PNG, len(img.content)))

    # Step 3: 长轮询等扫码
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = s.get(lp_url, timeout=10)
        except requests.exceptions.Timeout:
            if waiter:
                waiter("poll_timeout")
            continue
        except Exception as e:
            print("❌ 长轮询异常: %s" % e)
            return False
        if resp.status_code != 200:
            if waiter:
                waiter("poll_http_%d" % resp.status_code)
            time.sleep(1)  # 非 200 也退避，避免空转打爆服务器
            continue
        data = _json(resp.text)
        if not data or not data.get("userId"):
            if waiter:
                waiter("poll_wait")
            time.sleep(1)
            continue
        # 扫码确认成功
        user_id = data["userId"]
        ssecurity = data["ssecurity"]
        cuser_id = data["cUserId"]
        pass_token = data["passToken"]
        location = data["location"]
        print("✅ 扫码确认成功！userId=%s" % user_id)
        break
    else:
        print("❌ 等待扫码超时（%d 秒），请重试" % timeout)
        return False

    # Step 4: 拿 serviceToken
    resp = s.get(location, headers={"content-type": "application/x-www-form-urlencoded"}, timeout=15)
    if resp.status_code != 200:
        print("❌ Step4 serviceToken 获取失败 HTTP %d" % resp.status_code)
        return False
    service_token = resp.cookies.get("serviceToken")
    if not service_token:
        print("❌ Step4 响应无 serviceToken cookie")
        return False

    # 保存登录态
    secrets = {
        "user_id": user_id,
        "ssecurity": ssecurity,
        "cuser_id": cuser_id,
        "pass_token": pass_token,
        "service_token": service_token,
        "_note": "米家云扫码登录态（云电脑专用，勿提交git）",
    }
    with open(SECRETS, "w", encoding="utf-8") as f:
        json.dump(secrets, f, ensure_ascii=False, indent=2)
    os.chmod(SECRETS, 0o600)
    print("✅ 登录态已保存: %s (chmod 600)" % SECRETS)
    return True


def cmd_status():
    if not os.path.exists(SECRETS):
        print("❌ 尚未登录（无 %s）" % SECRETS)
        return False
    with open(SECRETS, encoding="utf-8") as f:
        s = json.load(f)
    ok = all(s.get(k) for k in ("user_id", "ssecurity", "service_token"))
    # 不打印任何 token 片段，仅显示账号与登录时间
    print("%s 登录态: user_id=%s（token 已就位，不展示）" %
          ("✅" if ok else "❌", s.get("user_id")))
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        sys.exit(0 if cmd_status() else 1)
    ok = qr_login()
    sys.exit(0 if ok else 1)
