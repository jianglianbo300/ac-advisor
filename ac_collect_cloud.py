#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ac_collect_cloud.py —— 云化只读采集（云电脑专用）

每次运行：
  - 云端读净化器温湿度（miservice, DID 875028325）
  - 云端读空调伴侣 开关/模式/目标温度/功率（DID 2056557176）
  - 读和风天气（上海, location=101020100）
追加一行 JSON 到 ac_data/readings_cloud.jsonl。
铁律：纯只读（miot_get_props），绝不 prop/set / action。
容错：单项失败以 null 占位，绝不中断。
"""
import asyncio
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ac_cloud_backend import _get_service, DID_PURIFIER, DID_AC_PARTNER

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_data")
DATA_FILE = os.path.join(DATA_DIR, "readings_cloud.jsonl")
MODES = {0: "auto", 1: "cool", 2: "dry", 3: "heat", 4: "fan"}


def _load_env():
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env):
        with open(env) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


def read_weather():
    """和风天气，失败返回 None。"""
    key = os.environ.get("QW_API_KEY")
    if not key:
        return None
    host = os.environ.get("QW_HOST", "https://devapi.qweather.com")
    if host and not host.startswith("http"):
        host = "https://" + host
    try:
        import requests
        url = "%s/v7/weather/now?location=121.4,31.1&key=%s" % (host, key)  # 上海闵行
        r = requests.get(url, timeout=8)
        d = r.json()
        now = d.get("now", {})
        return {"t": now.get("temp"), "rh": now.get("humidity"),
                "text": now.get("text"), "rain": now.get("precip"), "loc": "minhang"}
    except Exception as e:
        print("  [warn] weather fail: %s: %s" % (type(e).__name__, str(e)[:120]))
        return None


def main():
    _load_env()
    os.makedirs(DATA_DIR, exist_ok=True)
    svc = _get_service()

    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}

    async def _collect():
        out = {}
        # 净化器温湿度（全 None = 设备离线，如断电/断网）
        try:
            vals = await svc.miot_get_props(DID_PURIFIER, [(3, 7), (3, 1)])
            offline = vals is None or all(v is None for v in vals)
            out["indoor"] = {"temp": vals[0] if vals else None,
                             "hum": vals[1] if vals else None}
            out["purifier_offline"] = offline
        except Exception as e:
            print("  [warn] indoor cloud fail: %s: %s" % (type(e).__name__, str(e)[:120]))
            out["indoor"] = {"temp": None, "hum": None}
            out["purifier_offline"] = True
        # 空调伴侣状态（全 None = 伴侣离线，如拔电开窗）
        try:
            vals = await svc.miot_get_props(DID_AC_PARTNER, [(2, 1), (2, 3), (5, 1)])
            offline = vals is None or all(v is None for v in vals)
            out["ac"] = {
                "ac_on": vals[0] if vals else None,
                "ac_mode": None,  # (2,2) 模式云端偶发读不到，已从读取列表移除
                "ac_target": vals[1] if vals else None,
                "ac_watt": vals[2] if vals else None,
            }
            out["ac_offline"] = offline
        except Exception as e:
            print("  [warn] ac cloud fail: %s: %s" % (type(e).__name__, str(e)[:120]))
            out["ac"] = {"ac_on": None, "ac_mode": None, "ac_target": None, "ac_watt": None}
            out["ac_offline"] = True
        return out

    rec.update(asyncio.run(_collect()))
    rec["wx"] = read_weather() or {}

    with open(DATA_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    off_tags = []
    if rec.get("ac_offline"):
        off_tags.append("⚠️伴侣离线(可能开窗拔电)")
    if rec.get("purifier_offline"):
        off_tags.append("⚠️净化器离线")
    print("t=%s rh=%s ac_on=%s ac_watt=%s wx_t=%s%s" % (
        rec["indoor"]["temp"], rec["indoor"]["hum"],
        rec["ac"]["ac_on"], rec["ac"]["ac_watt"],
        (rec["wx"] or {}).get("t"),
        (" " + " ".join(off_tags)) if off_tags else ""))


if __name__ == "__main__":
    main()
