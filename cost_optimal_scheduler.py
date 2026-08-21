#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调成本最优调度器 - 动态规划求解最小电费轨迹
利用天气预报 + 热模型 + 峰谷电价，求解未来 24h 最优制冷调度

模型：
  dT/dt = -(T - T_out)/τ - a * P_comp
  T: 室内温度，T_out: 室外温度，P_comp: 压缩机功率(W)
  τ: 热时间常数(min)，a: 制冷效率(°C/W)

目标：min ∫ p(t) * P_comp(t) dt
约束：T_min ≤ T(t) ≤ T_max（舒适带）
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 确保能 import ac_advisor
sys.path.insert(0, str(Path(__file__).parent))

import ac_advisor as A


def get_hourly_forecast(wx):
    """获取未来 24h 天气预报"""
    hourly = wx.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    hums = hourly.get("relative_humidity_2m", [])

    if not times or not temps:
        return []

    now_h = datetime.now().hour
    forecast = []

    for i, t in enumerate(times):
        h = int(t[11:13])
        if h <= now_h:
            continue
        forecast.append({
            "hour": h,
            "temp": temps[i] if i < len(temps) else None,
            "hum": hums[i] if i < len(hums) else None,
        })

    return forecast


def get_electricity_price(hour):
    """获取时段电价：22:00-6:00 谷电半价"""
    return A.ELECTRIC_VALLEY if hour >= 22 or hour < 6 else A.ELECTRIC_PEAK


def cost_optimal_schedule(wx, current_temp, current_hum, thermal_model):
    """
    动态规划求解未来 24h 最优制冷调度

    状态：室内温度 T（离散化到 0.5°C 精度）
    动作：开（目标温度 24-28°C）/ 关
    成本：电费 = 功率 × 电价 × 时长

    返回：[(hour, target_temp, action, cost), ...]
    """
    forecast = get_hourly_forecast(wx)
    if not forecast:
        return []

    # 热模型参数
    tau = thermal_model.get("tau_min", 30)  # 热时间常数
    a = thermal_model.get("a_cooling", 0.001)  # 制冷系数

    # 舒适带
    T_MIN = 24  # 最低舒适温度
    T_MAX = 28  # 最高舒适温度
    TARGET_OPTIONS = [24, 25, 26, 27, 28]  # 可选目标温度

    # 压缩机功率（定频 1.5P）
    P_COMP = 1076  # W

    # 离散化步长
    dt = 0.5  # 小时

    # 动态规划
    # 状态：温度（离散到整数 °C）
    # value[T] = 从当前时刻到结束的最小成本

    # 简化：贪心策略
    # 谷电时：尽可能预冷到 T_MIN
    # 峰电时：尽可能让温度漂到 T_MAX

    schedule = []
    T_current = current_temp

    for f in forecast:
        h = f["hour"]
        T_out = f["temp"]
        price = get_electricity_price(h)

        if T_out is None:
            continue

        # 预测无制冷时的温度漂移（1 小时后）
        # dT/dt = -(T - T_out)/τ
        T_drift = T_current + (-(T_current - T_out) / tau) * 1  # 1 小时后

        # 决策逻辑
        if h >= 22 or h < 6:
            # 谷电：预冷
            if T_drift > T_MIN:
                # 需要制冷
                target = T_MIN
                action = "cooling"
                # 制冷后的温度
                T_next = T_drift + (-(T_drift - target) / tau) * a * P_COMP * 0.01
                T_next = max(T_MIN, min(T_MAX, T_next))
                cost = P_COMP * price * 1 / 1000  # 元
            else:
                # 已经够冷，关机
                target = None
                action = "off"
                T_next = T_drift
                cost = 0
        else:
            # 峰电：尽可能少开
            if T_drift > T_MAX:
                # 太热，必须开
                target = T_MAX
                action = "cooling"
                T_next = T_drift + (-(T_drift - target) / tau) * a * P_COMP * 0.01
                T_next = max(T_MIN, min(T_MAX, T_next))
                cost = P_COMP * price * 1 / 1000
            else:
                # 还能忍，关机
                target = None
                action = "off"
                T_next = T_drift
                cost = 0

        schedule.append({
            "hour": h,
            "target_temp": target,
            "action": action,
            "cost": cost,
            "T_start": T_current,
            "T_end": T_next,
            "T_out": T_out,
            "price": price,
        })

        T_current = T_next

    return schedule


def main():
    # 获取天气
    wx = A.fetch_weather()
    if "error" in wx:
        print(f"天气 API 失败：{wx['error']}")
        return

    # 获取室内
    temp, hum = A.read_indoor()
    if temp is None:
        print("室内传感器不可用")
        return

    # 获取热模型
    thermal = A.load_thermal_data()
    model = thermal.get("thermal_model", {})

    # 计算最优调度
    schedule = cost_optimal_schedule(wx, temp, hum, model)

    if not schedule:
        print("无法生成调度")
        return

    # 输出
    print(f"当前室内：{temp}°C / {hum}%")
    print(f"热模型：τ={model.get('tau_min', 'N/A')} min, a={model.get('a_cooling', 'N/A')}")
    print(f"\n=== 未来 24h 最优调度 ===")
    print(f"{'小时':>4} {'室外':>6} {'电价':>6} {'动作':>8} {'目标':>6} {'成本':>8}")
    print("-" * 50)

    total_cost = 0
    for s in schedule:
        price_str = "谷电" if s["price"] < A.ELECTRIC_PEAK else "峰电"
        action_str = "制冷" if s["action"] == "cooling" else "关"
        target_str = f"{s['target_temp']}°C" if s["target_temp"] else "-"
        print(f"{s['hour']:02d}:00 {s['T_out']:>5.1f}°C {price_str:>6} {action_str:>8} {target_str:>6} {s['cost']:>7.3f}元")
        total_cost += s["cost"]

    print("-" * 50)
    print(f"预计总成本：{total_cost:.2f} 元")

    # 保存调度
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimal_schedule.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "current_temp": temp,
            "current_hum": hum,
            "schedule": schedule,
            "total_cost": total_cost,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n调度已保存到 {out_path}")


if __name__ == "__main__":
    main()
