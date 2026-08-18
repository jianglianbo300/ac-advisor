#!/usr/bin/env python3
"""v8.20 传感器离线天气兜底回退 — 单元测试。

覆盖 cline 会话遗留的两个 bug：
  bug1: 回退调 A.cached_outdoor（函数其实在 ac_watch 本模块）→ AttributeError 被裸 except 吞掉
  bug2: 回退写在 state = A.load_state() 之前 → UnboundLocalError，同样被吞掉
两个 bug 叠加的效果：传感器一挂，兜底静默失效，且 temp 仍是 None 却走进"继续控制"分支。

全部用例都 monkeypatch 掉真实 IO（传感器/天气/插座），不动真空调。
"""
import sys
import types

import ac_advisor as A
import ac_watch as W


class Result:
    def __init__(self):
        self.ok = 0
        self.fail = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            print(f"  [PASS] {name}")
        else:
            self.fail += 1
            print(f"  [FAIL] {name} {detail}")


R = Result()


def make_env(indoor, outdoor, mode=None, sensor_off_since=None):
    """构造一次 main() 运行所需的全部 stub，返回 (calls, restore)。

    indoor:  (temp, hum) 或 (None, None)
    outdoor: cached_outdoor 的返回值；可以是 dict / None / Exception 实例
    """
    calls = {"apply": [], "saved": [], "logs": []}
    state = {"mode": mode}
    if sensor_off_since is not None:
        state["_sensor_off_since"] = sensor_off_since

    orig = {
        "read_indoor": A.read_indoor,
        "load_state": A.load_state,
        "save_state": A.save_state,
        "apply_and_commit": A.apply_and_commit,
        "reconcile_state": A.reconcile_state,
        "read_ac_power": A.read_ac_power,
        "ac_control_init": A.ac_control_init,
        "cached_outdoor": W.cached_outdoor,
        "log": W.log,
        "decide": W.decide,
    }

    A.read_indoor = lambda: indoor
    A.load_state = lambda: state
    A.save_state = lambda s: calls["saved"].append(dict(s))
    A.apply_and_commit = lambda m, t, s, ts, **kw: calls["apply"].append((m, t))
    A.reconcile_state = lambda s, ts: None
    A.read_ac_power = lambda *a, **k: None
    A.ac_control_init = lambda *a, **k: None

    def _cached(s, now):
        if isinstance(outdoor, Exception):
            raise outdoor
        return outdoor

    W.cached_outdoor = _cached
    W.log = lambda m: calls["logs"].append(str(m))
    # 决策阶段一律不动作，只关心是否走到了决策（=没被跳过）
    W.decide = lambda *a, **kw: (None, None, None)

    def restore():
        for k, v in orig.items():
            if hasattr(A, k) and k not in ("cached_outdoor", "log", "decide"):
                setattr(A, k, v)
        W.cached_outdoor = orig["cached_outdoor"]
        W.log = orig["log"]
        W.decide = orig["decide"]

    return calls, state, restore


def run_main():
    try:
        W.main()
        return None
    except Exception as e:  # 冒泡的异常就是回归，测试要能看见
        return e


def joined(logs):
    return " | ".join(logs)


print("=" * 68)
print("v8.20 传感器离线天气兜底 — 回归测试")
print("=" * 68)

# ── 用例 1：传感器正常 → 不走兜底，断连标记清除 ──
print("\n[1] 传感器正常，不触发兜底")
calls, state, restore = make_env(indoor=(27.0, 60.0), outdoor={"t": 31.0, "rh": 70, "rain": 0})
err = run_main()
restore()
R.check("main 无异常", err is None, f"got {err!r}")
R.check("未打兜底日志", "回退到天气预报" not in joined(calls["logs"]))
R.check("断连标记已清除", "_sensor_off_since" not in state)

# ── 用例 2：传感器挂 + 天气可用 → 兜底生效（bug1+bug2 的核心回归）──
print("\n[2] 传感器离线 + 天气可用 → 兜底生效（核心回归）")
calls, state, restore = make_env(indoor=(None, None), outdoor={"t": 31.0, "rh": 70, "rain": 0})
err = run_main()
restore()
logs = joined(calls["logs"])
R.check("main 无异常", err is None, f"got {err!r}")
R.check("兜底日志出现（bug 修复证据）", "回退到天气预报" in logs, f"logs={logs}")
R.check("兜底温度写入日志", "31" in logs, f"logs={logs}")
R.check("未误判为无兜底跳过", "无天气兜底" not in logs, f"logs={logs}")
R.check("兜底期间保留断连计时", state.get("_sensor_off_since") is not None)
R.check("兜底不执行关机", calls["apply"] == [], f"apply={calls['apply']}")

# ── 用例 3：传感器挂 + 天气也拿不到 → 明确跳过，不带 None 继续 ──
print("\n[3] 传感器离线 + 天气不可用 → 安全跳过")
calls, state, restore = make_env(indoor=(None, None), outdoor=None)
err = run_main()
restore()
logs = joined(calls["logs"])
R.check("main 无异常", err is None, f"got {err!r}")
R.check("打出无兜底跳过日志", "无天气兜底" in logs, f"logs={logs}")
R.check("记录断连起始时间", state.get("_sensor_off_since") is not None)
R.check("未执行开关机", calls["apply"] == [], f"apply={calls['apply']}")

# ── 用例 4：天气接口抛异常 → 不吞成静默，落 WARN 日志后安全跳过 ──
print("\n[4] 天气接口抛异常 → 落 WARN 不静默")
calls, state, restore = make_env(indoor=(None, None), outdoor=RuntimeError("boom"))
err = run_main()
restore()
logs = joined(calls["logs"])
R.check("main 无异常", err is None, f"got {err!r}")
R.check("WARN 日志可见（不再裸吞）", "天气兜底获取失败" in logs, f"logs={logs}")
R.check("异常类型进日志", "RuntimeError" in logs, f"logs={logs}")
R.check("随后安全跳过", "无天气兜底" in logs, f"logs={logs}")

# ── 用例 5：天气只有温度没湿度 → 湿度缺省 50，不误触除湿 ──
print("\n[5] 天气缺湿度 → 缺省 50 只走温度分支")
calls, state, restore = make_env(indoor=(None, None), outdoor={"t": 31.0, "rh": None, "rain": 0})
err = run_main()
restore()
logs = joined(calls["logs"])
R.check("main 无异常", err is None, f"got {err!r}")
R.check("兜底生效", "回退到天气预报" in logs, f"logs={logs}")
R.check("湿度缺省 50", "RH=50" in logs, f"logs={logs}")

# ── 用例 6：传感器长期断连 + 空调在制冷 + 无兜底 → 保守关机 ──
print("\n[6] 长期断连 + 制冷中 + 无兜底 → 保守关机")
old_ts = A.now_ts() if hasattr(A, "now_ts") else None
import datetime as _dt
long_ago = (_dt.datetime.now() - _dt.timedelta(minutes=W.SENSOR_TIMEOUT_ESCALATE + 10)).isoformat()
calls, state, restore = make_env(indoor=(None, None), outdoor=None,
                                mode="cooling", sensor_off_since=long_ago)
err = run_main()
restore()
R.check("main 无异常", err is None, f"got {err!r}")
R.check("执行保守关机", ("off", None) in calls["apply"], f"apply={calls['apply']}")

# ── 用例 7：越界读数 → 视为不可达，走兜底 ──
print("\n[7] 传感器读数越界 → 视为不可达并走兜底")
calls, state, restore = make_env(indoor=(99.0, 60.0), outdoor={"t": 31.0, "rh": 70, "rain": 0})
err = run_main()
restore()
logs = joined(calls["logs"])
R.check("main 无异常", err is None, f"got {err!r}")
R.check("打出越界日志", "越界" in logs, f"logs={logs}")
R.check("越界后走兜底", "回退到天气预报" in logs, f"logs={logs}")

# ── 用例 8：cached_outdoor 归属校验（bug1 的直接断言）──
print("\n[8] cached_outdoor 归属（bug1 直接断言）")
R.check("ac_watch 有 cached_outdoor", hasattr(W, "cached_outdoor"))
R.check("ac_advisor 无 cached_outdoor（故 A.cached_outdoor 必错）",
        not hasattr(A, "cached_outdoor"))
import inspect
src = inspect.getsource(W.main)
R.check("main 不再引用 A.cached_outdoor", "A.cached_outdoor" not in src)
R.check("main 使用本模块 cached_outdoor", "cached_outdoor(state, now_dt)" in src)
# 作用域校验：回退必须在 load_state 之后
i_load = src.find("A.load_state()")
i_fb = src.find("cached_outdoor(state, now_dt)")
R.check("回退位于 load_state 之后（bug2 修复）", 0 < i_load < i_fb,
        f"load_state@{i_load} fallback@{i_fb}")

print("\n" + "=" * 68)
print(f"结果：{R.ok} passed, {R.fail} failed")
print("=" * 68)
sys.exit(1 if R.fail else 0)
