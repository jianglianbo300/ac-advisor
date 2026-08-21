#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调策略成本对比基 - 当前策略 vs 理论最优
对比当前策略实际成本 vs 成本最优调度器理论成本
验证当前策略效果，差距 <5% 即够用

用法：
  python cost_benchmark.py --date 2026-08-20  # 对比指定日期
  python cost_benchmark.py                  # 对比今天
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 确保能 import ac_advisor
sys.path.insert(0, str(Path(__file__).parent))

import ac_advisor as A


def load_cycle_log(path=None):
    """读取周期数据"""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cycle_log.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def calculate_actual_cost(cycles, target_date):
    """
    计算指定日期的实际电费成本
    基于 cycle_log.jsonl 的 kwh_used 和时段电价
    """
    total_cost = 0
    total_kwh = 0
    cooling_kwh = 0
    off_kwh = 0
    cooling_hours = 0
    off_hours = 0

    for c in cycles:
        ts = c.get("start_ts", "")
        if not ts.startswith(target_date):
            continue

        kwh = c.get("kwh_used", 0)
        duration_h = c.get("duration_min", 0) / 60
        hour = int(ts[11:13])

        # 时段电价
        price = A.ELECTRIC_VALLEY if hour >= 22 or hour < 6 else A.ELECTRIC_PEAK
        cost = kwh * price

        total_cost += cost
        total_kwh += kwh

        if c.get("compressor_runtime_min", 0) > 5:
            cooling_kwh += kwh
            cooling_hours += duration_h
        else:
            off_kwh += kwh
            off_hours += duration_h

    return {
        "date": target_date,
        "total_cost": total_cost,
        "total_kwh": total_kwh,
        "cooling_kwh": cooling_kwh,
        "off_kwh": off_kwh,
        "cooling_hours": cooling_hours,
        "off_hours": off_hours,
        "avg_price": total_cost / total_kwh if total_kwh > 0 else 0,
    }


def run_cost_optimal(target_date, cycles):
    """
    对指定日期运行成本最优调度器
    用当天的实际室外温度 + 谷电预冷策略
    """
    # 获取当天的室外温度
    outdoor_temps = []
    for c in cycles:
        ts = c.get("start_ts", "")
        if ts.startswith(target_date) and c.get("start_outdoor_temp") is not None:
            outdoor_temps.append(c["start_outdoor_temp"])

    if not outdoor_temps:
        return None

    avg_outdoor = sum(outdoor_temps) / len(outdoor_temps)
    max_outdoor = max(outdoor_temps)
    min_outdoor = min(outdoor_temps)

    # 简化 DP：贪心策略
    # 谷电：尽可能预冷到 24°C
    # 峰电：尽可能让温度漂到 28°C
    T_MIN = 24
    T_MAX = 28
    P_COMP = 1076  # W
    tau = 30  # 热时间常数（假设）

    T_current = 26  # 假设初始温度
    total_cost = 0

    for hour in range(24):
        if hour < 6 or hour >= 22:
            # 谷电：预冷
            target = T_MIN
            price = A.ELECTRIC_VALLEY
            T_out = min_outdoor
            T_drift = T_current + (-(T_current - T_out) / tau)
            if T_drift > target:
                cost = P_COMP * price / 1000  # 元/小时
                T_next = T_drift + (-(T_drift - target) / tau) * 0.001 * P_COMP
                T_next = max(T_MIN, min(T_MAX, T_next))
            else:
                cost = 0
                T_next = T_drift
        else:
            # 峰电：尽可能少开
            target = T_MAX
            price = A.ELECTRIC_PEAK
            T_out = max_outdoor
            T_drift = T_current + (-(T_current - T_out) / tau)
            if T_drift > target:
                cost = P_COMP * price / 1000
                T_next = T_drift + (-(T_drift - target) / tau) * 0.001 * P_COMP
                T_next = max(T_MIN, min(T_MAX, T_next))
            else:
                cost = 0
                T_next = T_drift

        total_cost += cost
        T_current = T_next

    return {
        "date": target_date,
        "optimal_cost": total_cost,
        "avg_outdoor": avg_outdoor,
        "max_outdoor": max_outdoor,
        "min_outdoor": min_outdoor,
    }


def main():
    # 读取周期数据
    cycles = load_cycle_log()
    if not cycles:
        print("无周期数据")
        return

    # 日期
    if len(sys.argv) > 1 and sys.argv[1] != "--date":
        target_date = sys.argv[1]
    elif "--date" in sys.argv:
        idx = sys.argv.index("--date")
        target_date = sys.argv[idx + 1]
    else:
        target_date = datetime.now().strftime("%Y-%m-%d")

    print(f"=== 空调策略成本对比：{target_date} ===\n")

    # 实际成本
    actual = calculate_actual_cost(cycles, target_date)
    if actual["total_kwh"] == 0:
        print(f"指定日期 {target_date} 无数据")
        return

    print(f"【当前策略】")
    print(f"  实际成本：{actual['total_cost']:.2f} 元")
    print(f"  总用电：{actual['total_kwh']:.2f} 度")
    print(f"  制冷用电：{actual['cooling_kwh']:.2f} 度（{actual['cooling_hours']:.1f} 小时）")
    print(f"  停机用电：{actual['off_kwh']:.2f} 度（{actual['off_hours']:.1f} 小时）")
    print(f"  平均电价：{actual['avg_price']:.3f} 元/度")

    # 理论最优
    optimal = run_cost_optimal(target_date, cycles)
    if optimal:
        print(f"\n【理论最优】")
        print(f"  理论成本：{optimal['optimal_cost']:.2f} 元")
        print(f"  室外温度：{optimal['min_outdoor']:.1f}~{optimal['max_outdoor']:.1f}°C（平均 {optimal['avg_outdoor']:.1f}°C）")

        # 对比
        gap = actual["total_cost"] - optimal["optimal_cost"]
        gap_pct = (gap / actual["total_cost"] * 100) if actual["total_cost"] > 0 else 0
        print(f"\n【对比】")
        print(f"  成本差距：{gap:.2f} 元（{gap_pct:.1f}%）")

        if gap_pct < 5:
            print(f"  结论：✅ 当前策略已接近最优（差距 <5%），无需调整")
        elif gap_pct < 15:
            print(f"  结论：⚠️ 当前策略有 {gap_pct:.1f}% 优化空间，可考虑微调")
        else:
            print(f"  结论：❌ 当前策略与最优差 {gap_pct:.1f}%，建议调整策略")

    # 保存报告
    report = {
        "generated_at": datetime.now().isoformat(),
        "actual": actual,
        "optimal": optimal,
        "gap": gap if optimal else None,
        "gap_percent": gap_pct if optimal else None,
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cost_benchmark.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n报告已保存到 {out_path}")


if __name__ == "__main__":
    main()
