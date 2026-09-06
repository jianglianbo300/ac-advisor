#!/usr/bin/env python3
"""空调控制异常告警投递（v8.51 audit 2026-09-06）。

由 Hermes cron 每 10 分钟调用（no-agent 模式，stdout 直接投递微信）：
- ac_data/ac_alerts.jsonl 出现新告警（未被 ac_data/ac_alerts.seen 记录）→ 打印告警，Hermes 投递
- 无新告警 → 静默（空输出 = 不投递，不刷屏）
"""
import json
import os
import sys

REPO = os.environ.get("AC_ADVISOR_DIR") or r"D:\work\ac-advisor"
ALERTS = os.path.join(REPO, "ac_data", "ac_alerts.jsonl")
SEEN = os.path.join(REPO, "ac_data", "ac_alerts.seen")


def main():
    if not os.path.exists(ALERTS):
        return
    try:
        lines = [ln for ln in open(ALERTS, encoding="utf-8") if ln.strip()]
    except Exception:
        return
    if not lines:
        return
    try:
        last = json.loads(lines[-1])
    except Exception:
        return
    seen = ""
    if os.path.exists(SEEN):
        try:
            seen = open(SEEN, encoding="utf-8").read().strip()
        except Exception:
            seen = ""
    ts = str(last.get("ts", ""))
    if ts and ts == seen:
        return  # 已投递过
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"⚠️ 空调控制异常：连续 {last.get('streak')} 次控制失败（{ts}）")
    print(f"   原因：{str(last.get('reason', ''))[:200]}")
    print(f"   当时状态：{last.get('mode', '?')}；设备恢复后系统会自动继续，无需处理")
    try:
        with open(SEEN, "w", encoding="utf-8") as f:
            f.write(ts)
    except Exception:
        pass


if __name__ == "__main__":
    main()
