#!/usr/bin/env python3
"""空调周期效率分析 - 每天凌晨跑一次，读 cycle_log 数据，算效率，自动调参。
安装: cp 到 ~/.hermes/scripts/ac_efficiency_analysis.py
Cron: 每天 02:00 跑 (no_agent=true)"""
import json, os, sys, statistics
from datetime import datetime, timedelta

SCRIPT_DIR = r"D:\work\ac-advisor"
LOG_FILE = os.path.join(SCRIPT_DIR, "cycle_log.jsonl")
WATCH_SCRIPT = os.path.join(SCRIPT_DIR, "ac_watch.py")
STATE_FILE = os.path.join(SCRIPT_DIR, "ac_state.json")
REPORT_FILE = os.path.join(SCRIPT_DIR, "ac_efficiency_report.json")

MIN_COMP_MIN = 40  # 只统计完整周期（压缩机累计运行 >= ac_advisor.MIN_RUN），短周期/异常停机不参与
# v8.17 修正（2026-08-16 深查）：
# 1) 幸存者偏差——comp>=40 过滤把 24/25°C 短周期对照组剔光，best_temp 永远=26 且无对比意义
#    → 新增 all_cycles 视图（ΔAH/压缩机分钟，与时长无关的无偏比较）
# 2) kWh 纪元——2026-08-16 18:46（v8.16 严格 gap 上线）之前的 kwh_used 虚高 2-4 倍，
#    kWh 类指标只认纪元后
# 3) 污染过滤——rh_spike=True 的周期（开门/晾衣事件）剔除出效率统计（LongCat 待办#5）
HONEST_KWH_SINCE = datetime(2026, 8, 16, 18, 46)

def load_cycles(days=7):
    """读取最近 N 天的周期数据"""
    cycles = []
    cutoff = datetime.now() - timedelta(days=days)
    if not os.path.exists(LOG_FILE):
        return cycles
    with open(LOG_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                ts = c.get("end_ts", c.get("start_ts", ""))
                if ts:
                    dt = datetime.fromisoformat(ts)
                    if dt >= cutoff:
                        cycles.append(c)
            except (json.JSONDecodeError, ValueError):
                continue
    return cycles

def _eff_ok(c):
    """效率统计准入：无 rh_spike 污染 + ΔAH 可算。"""
    if c.get("rh_spike"):
        return False
    return c.get("start_AH") is not None and c.get("end_AH") is not None

def analyze(cycles):
    """按目标温度分组，算效率指标"""
    if not cycles:
        return {"status": "no_data", "cycles": 0}

    groups = {}       # 完整周期（comp>=40，kWh 纪元后才有 kwh 指标）
    groups_all = {}   # 全部周期（无偏 ΔAH/comp_min 比较）
    n_spike = 0
    n_pre_epoch = 0
    for c in cycles:
        comp = c.get("compressor_runtime_min")
        if not _eff_ok(c):
            if c.get("rh_spike"):
                n_spike += 1
            continue
        t = c.get("target_temp", 26)
        dah = c["start_AH"] - c["end_AH"]
        if comp and comp > 0:
            g = groups_all.setdefault(t, [])
            g.append({"dah_pm": dah / comp, "comp": comp, "dah": dah})
        if comp is None or comp < MIN_COMP_MIN:
            continue
        try:
            post_epoch = datetime.fromisoformat(c.get("end_ts", "")) >= HONEST_KWH_SINCE
        except ValueError:
            post_epoch = False
        kwh = c.get("kwh_used")
        if kwh is not None and kwh > 0 and not post_epoch:
            n_pre_epoch += 1
        groups.setdefault(t, []).append({
            "dah": dah, "kwh": kwh if (kwh and post_epoch) else None,
            "comp": comp, "dur": c.get("duration_min"),
        })

    results = {}
    for temp, group in sorted(groups.items()):
        dah_list = [g["dah"] for g in group]
        kwh_list = [g["kwh"] for g in group if g["kwh"]]
        eff_list = [g["dah"] / g["kwh"] for g in group if g["kwh"]]
        results[temp] = {
            "cycles": len(group),
            "avg_delta_AH": round(statistics.mean(dah_list), 2) if dah_list else None,
            "avg_kwh": round(statistics.mean(kwh_list), 3) if kwh_list else None,
            "avg_eff": round(statistics.mean(eff_list), 3) if eff_list else None,
            "avg_comp_min": round(statistics.mean([g["comp"] for g in group]), 1),
            "avg_duration_min": round(statistics.mean([g["dur"] for g in group if g["dur"] is not None]), 1) if any(g["dur"] is not None for g in group) else None,
        }

    results_all = {}
    for temp, group in sorted(groups_all.items()):
        results_all[temp] = {
            "cycles": len(group),
            "dah_per_comp_min": round(statistics.mean([g["dah_pm"] for g in group]), 3),
            "avg_comp_min": round(statistics.mean([g["comp"] for g in group]), 1),
        }

    # 无偏比较：ΔAH/压缩机分钟（all_cycles，>=2 样本才参与）
    best_temp = None
    best_eff = -1
    for temp, r in results_all.items():
        if r["cycles"] >= 2 and r["dah_per_comp_min"] > best_eff:
            best_eff = r["dah_per_comp_min"]
            best_temp = temp

    return {
        "status": "ok",
        "cycles": len(cycles),
        "by_temp": results,
        "all_cycles_by_temp": results_all,
        "excluded_spikes": n_spike,
        "excluded_pre_epoch_kwh": n_pre_epoch,
        "best_temp": best_temp,
        "best_eff": round(best_eff, 3) if best_temp else None,
        "best_metric": "dah_per_comp_min",
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }

def main():
    cycles = load_cycles()
    report = analyze(cycles)

    # 存报告
    with open(REPORT_FILE, 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 输出人类可读摘要
    if report["status"] == "no_data":
        print(f"📊 空调效率报告 | 数据不足（cycle_log 仅 {report['cycles']} 条，需至少 1 条）")
        print(f"   继续攒数据中...")
        return

    print(f"📊 空调效率报告 | 共 {report['cycles']} 个周期")
    print(f"   {'─'*50}")
    for temp, r in sorted(report["by_temp"].items()):
        eff = r["avg_eff"]
        eff_str = f"{eff:.2f} AH/kWh" if eff else "N/A(纪元前kWh)"
        print(f"   {temp}°C: {r['cycles']}轮 | ΔAH={r['avg_delta_AH']} | 电费={r['avg_kwh']}kWh/轮 | 效率={eff_str} | 压缩机={r['avg_comp_min']}min/轮")
    print(f"   {'─'*50}")
    print(f"   无偏比较（全部周期 ΔAH/压缩机分钟，不剔短周期）:")
    for temp, r in sorted(report.get("all_cycles_by_temp", {}).items()):
        print(f"   {temp}°C: {r['cycles']}轮 | ΔAH/min={r['dah_per_comp_min']} | 平均压缩机={r['avg_comp_min']}min")
    if report.get("excluded_spikes"):
        print(f"   剔除污染周期(rh_spike 开门/晾衣): {report['excluded_spikes']} 轮")

    bt = report.get("best_temp")
    be = report.get("best_eff")
    if bt and be:
        print(f"   {'─'*50}")
        print(f"   ✅ 目前最省电: {bt}°C（{be:.2f} AH/kWh）")

    # 建议是否调参
    by_temp = report.get("by_temp", {})
    if len(by_temp) >= 2:
        effs = [(int(t), r["avg_eff"]) for t, r in by_temp.items() if r["avg_eff"] is not None and r["cycles"] >= 2]
        if len(effs) >= 2:
            effs.sort(key=lambda x: -x[1])
            best = effs[0]
            second = effs[1]
            diff_pct = (best[1] - second[1]) / second[1] * 100
            if diff_pct > 15:
                print(f"   ⚠️ {best[0]}°C 比 {second[0]}°C 省 {diff_pct:.0f}%，差异显著")
                print(f"   建议关注是否应调整默认目标温度")
            else:
                print(f"   ℹ️ {best[0]}°C 与 {second[0]}°C 效率差异 {diff_pct:.0f}%，暂不需要调整")

    # 检查数据量是否足够
    total = report["cycles"]
    if total < 5:
        print(f"\n   ℹ️ 数据量偏少（{total}轮），建议攒够 10+ 轮再调参")

if __name__ == "__main__":
    main()