import json, os

# Check AC partner status
cfg_file = r"D:\work\ac-advisor\miio_config.json"
with open(cfg_file, "r", encoding="utf-8") as f:
    cfg = json.load(f)

ap = cfg.get("ac_partner", {})
print(f"AC Partner IP: {ap.get('ip')}")
print(f"Token: {ap.get('token')[:8]}...")

# Try to connect
try:
    from miio.airconditioningcompanionMCN import AirConditioningCompanionMcn02
    ctrl = AirConditioningCompanionMcn02(ap["ip"], ap["token"])
    st = ctrl.status()
    print(f"Status: on={st.is_on}, mode={st.mode}, target={st.target_temperature}, power={st.load_power}")
except Exception as e:
    print(f"Connection failed: {e}")
