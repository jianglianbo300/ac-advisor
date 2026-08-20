#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调迭代日报 - 每天 22:00 自动生成，推送到微信
内容：今日决策统计、阈值调整、关键事件、效率快照、明日建议
"""
import json
import os
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = r"D:\work\ac-advisor"
LEARNED_FILE = os.path.join(SCRIPT_DIR, "ac_learned.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "ac_state.json")
REPORT_FILE = os.path.join(SCRIPT_DIR, "ac_efficiency_report.json")
CYCLE_FILE = os.path.join(SCRIPT_DIR, "cycle_log.jsonl")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "ac_daily_report.json")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_cycles_today():
    """读取今天的 cycle 数据"""
    cycles = []
    today = datetime.now().strftime("%Y-%m-%d")
    if not os.path.exists(CYCLE_FILE):
        return cycles
    with open(CYCLE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                ts = c.get("end_ts", c.get("start_ts", ""))
                if ts.startswith(today):
                    cycles.append(c)
            except Exception:
                continue
    return cycles


def generate_report():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    learned = load_json(LEARNED_FILE)
    state = load_json(STATE_FILE)
    report = load_json(REPORT_FILE)
    cycles = load_cycles_today()

    # 决策统计
    decisions = learned.get("decision_log", [])
    today_decisions = [d for d in decisions if d.get("time", "").startswith(today)]
    cooling_count = sum(1 for d in today_decisions if d.get("action") == "cooling")
    fan_count = sum(1 for d in today_decisions if d.get("action") in ("fan", "fan_locked"))
    off_count = sum(1 for d in today_decisions if d.get("action") == "off")
    evaluated_count = sum(1 for d in today_decisions if d.get("evaluated"))

    # 阈值调整
    adjusted = learned.get("adjusted_thresholds", {})

    # 当前状态
    current_mode = state.get("mode", "unknown")
    run_start = state.get("run_start")
    last_on = state.get("last_on_at")
    last_off = state.get("last_off_at")

    # 运行时长
    run_minutes = None
    if run_start and current_mode in ("cooling", "dehumid"):
        try:
            rs = datetime.fromisoformat(run_start)
            run_minutes = int((now - rs).total_seconds() / 60)
        except Exception:
            pass

    # 效率快照
    cycles_total = report.get("cycles", 0)
    by_temp = report.get("by_temp", {})

    # 关键事件
    events = []
    # 低湿保护
    low_hum_events = [d for d in today_decisions if "低湿" in str(d.get("action", ""))]
    if low_hum_events:
        events.append(f"低湿过冷保护触发 {len(low_hum_events)} 次")
    # 过冷收手
    floor_events = [d for d in today_decisions if "温度过低" in str(d.get("reason", ""))]
    if floor_events:
        events.append(f"过冷关机 {len(floor_events)} 次")
    # 湿度告警
    hum_alert = [d for d in today_decisions if "湿度" in str(d.get("reason", "")) and "高" in str(d.get("reason", ""))]
    if hum_alert:
        events.append(f"高湿提醒 {len(hum_alert)} 次")

    # 构建报告
    lines = []
    lines.append(f"🏠 空调迭代日报 · {today}")
    lines.append(f"━━━━━━━━━━━━━━━━━━")
    lines.append(f"")
    lines.append(f"📊 今日决策：{len(today_decisions)} 次")
    lines.append(f"   制冷 {cooling_count} 次 | 风扇 {fan_count} 次 | 关 {off_count} 次")
    lines.append(f"   已回评 {evaluated_count} 条")
    lines.append(f"")

    lines.append(f"⚙️ 当前阈值：")
    if adjusted:
        for k, v in adjusted.items():
            label = "温度偏移" if "temp" in k else k
            sign = "+" if v > 0 else ""
            lines.append(f"   {label}: {sign}{v}°C")
    else:
        lines.append(f"   无调整（基线 TEMP_COOLING=27°C）")
    lines.append(f"")

    lines.append(f"🔄 当前状态：{current_mode}")
    if run_minutes is not None:
        lines.append(f"   已运行 {run_minutes} 分钟")
    lines.append(f"   最后开：{last_on or '无'}")
    lines.append(f"   最后关：{last_off or '无'}")
    lines.append(f"")

    if events:
        lines.append(f"⚡ 关键事件：")
        for e in events:
            lines.append(f"   · {e}")
        lines.append(f"")

    lines.append(f"📈 效率快照（最近 {cycles_total} 个周期）：")
    if by_temp:
        for temp, data in sorted(by_temp.items()):
            lines.append(f"   {temp}°C: {data.get('cycles', 0)} 周期, 平均效率 {data.get('avg_eff', 0):.2f}, 平均 {data.get('avg_comp_min', 0):.1f} 分钟")
    else:
        lines.append(f"   暂无数据")
    lines.append(f"")

    # 明日建议
    lines.append(f"💡 明日建议：")
    if cooling_count > 10:
        lines.append(f"   · 今天制冷频繁，检查温度偏移是否需要进一步下调")
    if fan_count > 15:
        lines.append(f"   · 风扇占比高，可能温度阈值偏低，考虑上调 0.5°C")
    if adjusted.get("temp_cooling", 0) < -3:
        lines.append(f"   · 温度偏移已达 {adjusted['temp_cooling']}°C，关注是否过冷")
    if not events and cooling_count <= 5:
        lines.append(f"   · 今天运行平稳，无需调整")
    lines.append(f"   · 传感器校准偏移: -4°C（固定补偿）")

    report_text = "\n".join(lines)

    # 保存
    output = {
        "date": today,
        "generated_at": now.isoformat(),
        "text": report_text,
        "stats": {
            "total_decisions": len(today_decisions),
            "cooling": cooling_count,
            "fan": fan_count,
            "off": off_count,
            "evaluated": evaluated_count,
            "current_mode": current_mode,
            "adjusted_thresholds": adjusted,
        }
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return report_text


if __name__ == "__main__":
    text = generate_report()
    print(text)
