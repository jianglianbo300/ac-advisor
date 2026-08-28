# -*- coding: utf-8 -*-
"""
空调数据采集器 — 纯只读，绝不控制空调。
读取: 米家净化器 4 Lite (DID 875028325) 室内温湿度 + (可选)和风天气
输出: 追加一行 JSONL 到 ac_data/readings.jsonl
铁律: 只用 prop/get 读取，绝不发 prop/set / action，无任何开关/温度设置指令。
"""
import json, os, sys, time, datetime

AUTH_PATH = os.path.join(os.environ["USERPROFILE"], "xiaomi_auth.json")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_data")
DATA_FILE = os.path.join(DATA_DIR, "readings.jsonl")
PURIFIER_DID = "875028325"   # 米家空气净化器 4 Lite（温湿度源）

# 正确属性映射（2026-08-23 实测确认）
SIID_ENV = 3
PIID_TEMP = 7   # 温度 °C (直接值，非华氏)
PIID_HUM = 1    # 湿度 %
PIID_CO2 = 4    # CO2/PM2.5

def _retry(fn, tries=4, base_delay=2, label="op"):
    """简单指数退避重试：失败重试 base_delay*2^n 秒，全败抛最后异常。"""
    import time
    last = None
    for n in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if n < tries - 1:
                time.sleep(base_delay * (2 ** n))
    raise last

def _load_env():
    """复用 ac-advisor 惯例：读同目录 .env 注入环境变量（和风天气 key）"""
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env):
        with open(env) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

def read_indoor(mc, base):
    """只读净化器温湿度+CO2。纯 prop/get，无任何写操作。带自动重试。"""
    def _one():
        params = [
            {"did": PURIFIER_DID, "siid": SIID_ENV, "piid": PIID_TEMP},
            {"did": PURIFIER_DID, "siid": SIID_ENV, "piid": PIID_HUM},
            {"did": PURIFIER_DID, "siid": SIID_ENV, "piid": PIID_CO2},
        ]
        resp = mc.request(base + "/miotspec/prop/get", {"data": json.dumps({"params": params})})
        result = json.loads(resp)
        out = {}
        for r in result.get("result", []):
            if r.get("code") == 0:
                out[r.get("piid")] = r.get("value")
        if not out:
            raise RuntimeError("empty result")
        return {
            "temp": out.get(PIID_TEMP),
            "hum": out.get(PIID_HUM),
            "co2": out.get(PIID_CO2),
        }
    return _retry(_one, tries=4, base_delay=2, label="indoor")

def read_weather():
    """(可选) 读和风天气，带自动重试；全败返回 None，绝不抛异常影响主流程。"""
    def _one():
        import requests
        key = os.environ.get("QW_API_KEY")
        if not key:
            return None
        host = os.environ.get("QW_HOST", "https://devapi.qweather.com")
        if host and not host.startswith("http"):
            host = "https://" + host
        lat, lon = 31.1, 121.4  # 上海闵行
        url = f"{host}/v7/weather/now?location={lon},{lat}&key={key}"
        r = requests.get(url, timeout=8)
        d = r.json()
        now = d.get("now", {})
        return {"t": now.get("temp"), "rh": now.get("humidity"),
                "text": now.get("text"), "rain": now.get("precip")}
    try:
        return _retry(_one, tries=4, base_delay=2, label="weather")
    except Exception:
        return None

def main():
    _load_env()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AUTH_PATH) as f:
        auth = json.load(f)
    mc = MiCloud("13125554911", "Bo7812700874")
    mc.service_token = auth["serviceToken"]
    mc.user_id = auth["userId"]
    mc.ssecurity = auth["ssecurity"]
    mc.default_server = "cn"
    base = mc._get_api_url("cn")

    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        indoor = read_indoor(mc, base)
        rec.update(indoor)
    except Exception as e:
        rec["err"] = f"indoor:{e!r}"
    rec["wx"] = read_weather()

    with open(DATA_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # 静默：只输出一行精简摘要（cron 设为 local 不推送）
    print(f"t={rec.get('temp')} rh={rec.get('hum')} co2={rec.get('co2')} "
          f"wx_t={ (rec.get('wx') or {}).get('t') } err={rec.get('err')}")

if __name__ == "__main__":
    # 延迟 import 以兼容脚本在无 micloud 环境下的报错信息
    from micloud import MiCloud
    main()
