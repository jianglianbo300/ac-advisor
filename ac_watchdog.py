#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调监控看门狗 v1.0 — ac_watch 心跳检测（2026-08-16，取代 ac_off_alert 提醒职能）

背景：ac_watch.py 已实控空调（v8.2.1+），原 ac_off_alert 每 30 分钟的"该开/该关"
提醒变成噪音（系统 2 分钟后就自己动了）；真正缺的是反向守护——ac_watch 持续失败
（脚本崩/锁死/Python 环境坏/job 被 disable）时无人发现。cron 在 gateway 进程内，
gateway 级守护由 Windows 计划任务 Hermes_Gateway 负责；本脚本覆盖
"gateway 活着但 ac_watch 死了"的空档（schedule 全天化后含凌晨）。

行为：
  - ac_watch.log mtime 距今 >20min → 微信报警（每 2h 最多 1 次，防轰炸）
  - 从失联恢复 → 报一次恢复
  - 正常时零输出（Hermes cron 无输出不推送）
cron：cca8361f1c4c（*/30，原 ac_off_alert job，文件名不变免动 jobs.json）
"""
import json
import os
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
WATCH_LOG = os.path.join(SCRIPT_DIR, "ac_watch.log")
STATE_FILE = os.path.join(SCRIPT_DIR, "ac_watchdog_state.json")
STALE_MIN = 20        # ac_watch.log 静默多久算失联（tick=2min，容忍 10 个 tick 丢失）
RE_ALERT_MIN = 120    # 失联期间重复报警间隔


def log_age_min(log_path, now=None):
    """日志距今多少分钟；文件不存在返回 None（视为从未运行）。"""
    now = now if now is not None else time.time()
    if not os.path.exists(log_path):
        return None
    return (now - os.path.getmtime(log_path)) / 60.0


def evaluate(age_min, now, st):
    """纯决策：返回 (print文本 or None, 新状态 dict)。
    age_min=None 表示日志缺失；now 为 epoch 秒。"""
    stale = age_min is None or age_min > STALE_MIN
    alerting = bool(st.get("alerting"))
    last_alert_ts = st.get("last_alert_ts")

    if stale:
        rearm = True
        if last_alert_ts:
            rearm = (now - last_alert_ts) / 60.0 >= RE_ALERT_MIN
        if rearm:
            age_str = "日志文件缺失" if age_min is None else f"{age_min:.0f} 分钟无心跳"
            text = (f"🚨 空调自动监控疑似失联：ac_watch.log {age_str}（阈值 {STALE_MIN}min）。"
                    f"空调控制已停摆，请检查 hermes cron job 1d6c5460de5e / python 环境。")
            new_st = {"alerting": True, "last_alert_ts": now,
                      "last_alert_at": datetime.fromtimestamp(now).isoformat(timespec="seconds")}
            return text, new_st
        return None, st  # 报警冷静期内，保持状态不重复轰炸

    if alerting:
        text = f"✅ 空调自动监控已恢复心跳（此前失联，最后报警 {st.get('last_alert_at', '?')}）"
        return text, {"alerting": False}
    return None, {"alerting": False}


def load_state(path=STATE_FILE):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st, path=STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    now = time.time()
    text, new_st = evaluate(log_age_min(WATCH_LOG, now), now, load_state())
    if text is not None:
        save_state(new_st)
        print(text)  # 有输出 → cron 推微信


def _selftest():
    now = 1770000000.0
    # 正常心跳 + 无历史报警 → 静默
    t, st = evaluate(2.0, now, {})
    assert t is None and not st["alerting"]
    # 正常心跳 + 历史报警 → 恢复通知一次
    t, st = evaluate(2.0, now, {"alerting": True, "last_alert_at": "x"})
    assert t and "恢复" in t and not st["alerting"]
    # 失联 + 从未报 → 报警
    t, st = evaluate(35.0, now, {})
    assert t and "失联" in t and st["alerting"] and st["last_alert_ts"] == now
    # 失联 + 30min 前已报 → 冷静期静默
    t2, st2 = evaluate(35.0, now, st)
    assert t2 is None and st2 is st
    # 失联 + 3h 前已报 → 再报
    t3, st3 = evaluate(35.0, now, {"alerting": True, "last_alert_ts": now - 3 * 3600})
    assert t3 and "失联" in t3
    # 日志缺失 → 报警
    t4, _ = evaluate(None, now, {})
    assert t4 and "缺失" in t4
    # 边界：恰好 20min 不算失联
    t5, _ = evaluate(float(STALE_MIN), now, {})
    assert t5 is None
    print("ac_watchdog selftest: ALL PASS (v1.0)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
