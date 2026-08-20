import json
state_file = r"D:\work\ac-advisor\ac_state.json"
with open(state_file, "r", encoding="utf-8") as f:
    state = json.load(f)

state["mode"] = "cooling"
state["run_start"] = "2026-08-20T09:18:00"
state["target_temp"] = 26
state["last_on_at"] = "2026-08-20T09:18:00"
state["manual_off_at"] = None  # 清除手动关锁定

with open(state_file, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

print("State updated: cooling 26C")
