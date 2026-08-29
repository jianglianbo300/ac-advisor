"""v8.35 效果层重放（6b DP缓存失效 / 6c 手动锚点）—— 全 mock，不触真机、不写 ac_watch.log / ac_learned.json"""
import io
import sys
from datetime import datetime, timedelta
from unittest import mock

import ac_advisor as A
import ac_watch as W

class _Out(io.StringIO):
    def reconfigure(self, **k):
        pass

out = _Out()
now = datetime.now()

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name} {detail}")
    print(("PASS" if cond else "FAIL"), name, detail)

def base_state(**over):
    st = {
        "mode": None, "run_start": None, "last_off_at": (now - timedelta(minutes=60)).isoformat(timespec="seconds"),
        "temp_history": [], "rh_history": [],
        "estimated_kwh": 50.0, "_prev_kwh": 50.0, "_prev_power": 25,
        "_daily_kwh": 1.0, "_daily_kwh_date": now.date().isoformat(),
        "_fake_run_count": 0,
    }
    st.update(over)
    return st

def run(state, temp, hum, socket_val="off", power=25):
    cap = {"saved": None, "decisions": []}
    def save(s): cap["saved"] = dict(s)
    def logd(*a, **k): cap["decisions"].append((a[1], a[2] if len(a) > 2 else None))
    with mock.patch.object(A, "read_ac_power", side_effect=lambda timeout=4.0: setattr(A, "AC_MEASURED_W", power)), \
         mock.patch.object(A, "ac_control_init", side_effect=lambda: setattr(A, "AC_SOCKET", socket_val)), \
         mock.patch.object(A, "read_indoor", return_value=(temp, hum)), \
         mock.patch.object(A, "load_state", return_value=state), \
         mock.patch.object(A, "save_state", side_effect=save), \
         mock.patch.object(A, "load_learned", return_value={"adjusted_thresholds": {"temp_cooling": 0}, "decision_log": []}), \
         mock.patch.object(A, "current_price", return_value=A.ELECTRIC_VALLEY), \
         mock.patch.object(A, "load_thermal_data", return_value={"thermal_model": {}}), \
         mock.patch.object(W, "cached_outdoor", return_value={"t": 30, "rh": 70, "rain": "0.0"}), \
         mock.patch.object(W, "evaluate", lambda *a, **k: None), \
         mock.patch.object(W, "log", lambda *a, **k: None), \
         mock.patch.object(W, "log_decision", logd), \
         mock.patch.object(W, "tts_speak", lambda *a, **k: None), \
         mock.patch.object(W, "night_hours", return_value=False), \
         mock.patch.object(W, "EVENING", (-1, -1)), \
         mock.patch.object(sys, "argv", ["ac_watch.py", "--dry"]), \
         mock.patch.object(sys, "stdout", out):
        W.main()
    return cap

# ── 6b: DP 蓄冷缓存失效 —— 命中后 temp<=蓄冷目标 → override False ──
cache = {"hour": now.hour, "ts": now.isoformat(timespec="seconds"),
         "override": True, "target": 24, "reason": "DP test cache"}
st = base_state(_dp_schedule_cache=dict(cache))
cap = run(st, temp=24, hum=55)          # temp <= 蓄冷目标 → 必须失效，不得强开
check("6b-a 达蓄冷目标不开机", cap["decisions"] == [],
      f"decisions={cap['decisions']}")

st = base_state(_dp_schedule_cache=dict(cache))
cap = run(st, temp=23, hum=55)          # temp < 目标 同理
check("6b-b 低于蓄冷目标不开机", cap["decisions"] == [],
      f"decisions={cap['decisions']}")

st = base_state(_dp_schedule_cache=dict(cache))
cap = run(st, temp=25, hum=55)          # temp > 目标 → override 保持 → 强制蓄冷开
check("6b-c 未达目标仍走蓄冷覆盖", cap["decisions"] and cap["decisions"][0][0] == "cooling"
      and "target=24" in out.getvalue(), f"decisions={cap['decisions']} stdout_tail={out.getvalue().strip()[-60:]!r}")

st = base_state()                        # 无缓存对照：25°C 不应开机
cap = run(st, temp=25, hum=55)
check("6b-d 无缓存无动作", cap["decisions"] == [], f"decisions={cap['decisions']}")

# ── 6c: manual_on_at 锚点过期清除已移出 mode 前置 ──
expired = (now - timedelta(minutes=721)).isoformat(timespec="seconds")
st = base_state(manual_on_at=expired)    # off 态 + 过期锚点 → 必须被清
cap = run(st, temp=25, hum=55)
check("6c-a off态过期锚点被清", st.get("manual_on_at") is None,
      f"manual_on_at={st.get('manual_on_at')}")

recent = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
st = base_state(manual_on_at=recent)     # 未过期 → 保留；off 态不得误拦 decide
cap = run(st, temp=25, hum=55)
check("6c-b 未过期锚点保留且不拦decide", st.get("manual_on_at") == recent,
      f"manual_on_at={st.get('manual_on_at')!r} decisions={cap['decisions']}")

st = base_state(mode="off", manual_off_at=recent)   # 手动关5min 温升<1 → 尊重意图拦启动
cap = run(st, temp=25, hum=55)
check("6c-c 手动关冷却期拦截启动", cap["decisions"] == [] and st.get("manual_off_at") == recent,
      f"decisions={cap['decisions']}")

st = base_state(mode="off", manual_off_at=expired)  # 手动关过期 → 清除恢复自动
cap = run(st, temp=25, hum=55)
check("6c-d 手动关过期锚点清除", st.get("manual_off_at") is None,
      f"manual_off_at={st.get('manual_off_at')!r}")

print(f"\n=== {len(PASS)} PASS / {len(FAIL)} FAIL ===")
sys.exit(1 if FAIL else 0)
