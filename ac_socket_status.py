# -*- coding: utf-8 -*-
"""上海空调(米家空调伴侣 lumi.acpartner.mcn02 DID=2056557176)状态查询 — 纯只读, 绝不控制"""
import json, os, sys, datetime
sys.path.insert(0, os.path.join(os.environ["USERPROFILE"], "homeassistant-venv", "Lib", "site-packages"))
from micloud import MiCloud

AUTH = json.load(open(os.path.join(os.environ["USERPROFILE"], "xiaomi_auth.json")))
mc = MiCloud("13125554911", "Bo7812700874")
mc.service_token = AUTH["serviceToken"]
mc.user_id = AUTH["userId"]
mc.ssecurity = AUTH["ssecurity"]
mc.default_server = "cn"
base = mc._get_api_url("cn")

DID = "2056557176"
params = [
    {"did": DID, "siid": 2, "piid": 1},   # 插座电源
    {"did": DID, "siid": 3, "piid": 1},   # 空调电源(power)
    {"did": DID, "siid": 3, "piid": 2},   # 已配对/可发码
    {"did": DID, "siid": 4, "piid": 1},   # 运行(是否开机)
    {"did": DID, "siid": 4, "piid": 3},   # 状态码 P_M_T_S_D
    {"did": DID, "siid": 5, "piid": 1},   # 累计电量 kWh
]
resp = mc.request(base + "/miotspec/prop/get", {"data": json.dumps({"params": params})})
r = json.loads(resp)

# 解析
def find(siid, piid):
    for x in r.get("result", []):
        if x.get("siid") == siid and x.get("piid") == piid and x.get("code") == 0:
            return x.get("value")
    return None

socket_power = find(2, 1)
ac_power = find(3, 1)
pair_ok = find(3, 2)
running = find(4, 1)
status_code = find(4, 3)
kwh = find(5, 1)

ac_on = (ac_power and str(ac_power) not in ("0", "False")) or (status_code and str(status_code).startswith("P1"))

print(f"查询时间: {datetime.datetime.now().isoformat(timespec='seconds')}")
print(f"插座电源: {socket_power}  ({'通电' if socket_power else '断电'})")
print(f"可发红外码: {pair_ok}")
print(f"空调运行: {running}  →  {'开机中' if ac_on else '已关机'}")
print(f"状态码: {status_code}  (P=Power,M=Mode,T=Target,C=...)")
print(f"累计耗电: {kwh} kWh")

# 从状态码解析目标温度
if status_code and "T" in str(status_code):
    import re
    m = re.search(r"T(\d+)", str(status_code))
    if m:
        print(f"目标温度: {m.group(1)}°C")
