# -*- coding: utf-8 -*-
"""
米家自动化调参建议 — 每日分析，只做建议，绝不控制空调。
读: ac_data/readings.jsonl (近24h) + 最新天气
出: 一条针对当前"27开 26关 26目标"的米家自动化优化建议
铁律: 零 prop/set、零控制指令，纯数据分析 + 文案。
"""
import json, os, datetime, statistics

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "ac_data", "readings.jsonl")

# 当前米家自动化基准(整数实测收敛值) — 注意米家温度只能设整数
CUR = {"on_deg": 27.0, "off_deg": 26.0, "target": 26.0,
       "title": ">27°C开制冷26 / ≤26°C关"}

def load_readings():
    recs = []
    if not os.path.exists(DATA_FILE):
        return recs
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
    with open(DATA_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ts = datetime.datetime.fromisoformat(r.get("ts"))
                if ts >= cutoff and r.get("temp") is not None:
                    recs.append(r)
            except Exception:
                continue
    return recs

def analyze(recs):
    if not recs:
        return None, "no_records"
    temps = [r["temp"] for r in recs if r.get("temp") is not None]
    hums = [r["hum"] for r in recs if r.get("hum") is not None]
    wx_temps = []
    for r in recs:
        wx = r.get("wx") or {}
        try:
            wx_temps.append(float(wx["t"]))
        except Exception:
            pass

    n = len(recs)
    t_max, t_min = max(temps), min(temps)
    t_avg = statistics.mean(temps)
    h_avg = statistics.mean(hums) if hums else None
    wx_avg = statistics.mean(wx_temps) if wx_temps else None

    # 高温超阈值比例：超过27°C(启动线)的读数占比
    over_on = sum(1 for t in temps if t > CUR["on_deg"]) / n * 100
    # 是否长时间停在启动线附近 → 说明迟到导致难降温
    near_on = sum(1 for t in temps if t >= CUR["on_deg"] - 0.3) / n * 100

    return {
        "n": n, "t_max": t_max, "t_min": t_min, "t_avg": t_avg,
        "h_avg": h_avg, "wx_avg": wx_avg,
        "over_on_pct": over_on, "near_on_pct": near_on, "recs": recs,
    }, "ok"

def build_advice(a):
    """基于分析给出一条可执行的米家调参建议。"""
    lines = []
    act = "保持"

    # 1) 室外温度高 + 室内长时间超启动线 → 定频机降不下来, 建议提前/提高启动线收益有限, 应降低目标温差或延长运行
    if a["wx_avg"] is not None and a["wx_avg"] >= 30 and a["over_on_pct"] > 20:
        act = "制冷需求强"
        lines.append("· 上海室外均温已 ≥30°C，室内超27°C启动线的时间占比 " +
                     f"{a['over_on_pct']:.0f}%，说明现有\"27开/26关\"在午后冷不下来。")
        lines.append("· 建议：把启动线保持27°C、但**结束线维持26°C**（米家只支持整数），"
                     "让定频机一次跑长一点，而不是反复启停；或午后定时提前到16:00开机。")
    # 2) 湿度高 + 阴雨 → 湿度门控更重要
    if a["h_avg"] is not None and a["h_avg"] >= 70 and a["wx_avg"] is not None and a["wx_avg"] < 28:
        act = "湿度偏高"
        lines.append("· 近24h平均湿度 " + f"{a['h_avg']:.0f}%，且室外温度不高，"
                     "单纯制冷除湿效率低、还费电。")
        lines.append("· 建议：关闭时保留\"湿度≥75%才禁开\"的门控，或晚间改为除湿优先，"
                     "避免潮湿天盲目制冷。")
    # 3) 一天温差过大(早晨凉午后热) → 用峰谷+分时段
    if a["t_max"] - a["t_min"] >= 4:
        if act == "保持":
            act = "温差大，建议分时段"
        lines.append("· 近24h室温在 " + f"{a['t_min']:.1f}~{a['t_max']:.1f}°C 间波动(" +
                     f"Δ{a['t_max']-a['t_min']:.1f}°C)，夜里凉白天热。")
        lines.append("· 建议：白天(12-18时)才用\"温度触发\"，夜间关闭温控、靠自然降温，"
                     "省电且避免夜里过冷。")
    # 4) 一切正常
    if act == "保持" and not lines:
        lines.append("· 近24h室温 " + f"{a['t_min']:.1f}~{a['t_max']:.1f}°C (均{a['t_avg']:.1f})，"
                     f"湿度 {a['h_avg']:.0f}%，室外{a['wx_avg']:.0f}°C，"
                     "运行平稳，无明显异常。")
        lines.append("· 建议：维持现有 \"" + CUR["title"] + "\" 设置，暂不调整。")

    header = f"📊 空调调参|今日建议 ({datetime.datetime.now():%m-%d})"
    meta = (f"样本{a['n']}条 | 室温{a['t_min']:.1f}~{a['t_max']:.1f}°C | "
            f"均温{a['t_avg']:.1f} | 湿度{a['h_avg']:.0f}% | 室外{a['wx_avg']:.0f}°C")
    return header, meta, act, lines

def main():
    recs = load_readings()
    a, status = analyze(recs)
    if status == "no_records":
        print("暂无可分析数据(采集未运行或样本不足)。")
        return
    header, meta, act, lines = build_advice(a)
    print(header)
    print(meta)
    print(f"建议方向：{act}")
    for l in lines:
        print(l)

if __name__ == "__main__":
    main()
