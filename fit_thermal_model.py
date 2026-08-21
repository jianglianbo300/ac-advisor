#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调热模型拟合 - 纯压缩机制冷周期拟合
只用压缩机实际运行（compressor_runtime_min > 0）的周期数据
"""

import json
import os
import sys
from pathlib import Path

# 确保能 import ac_advisor
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np


def load_cycle_log(path=None):
    """读取周期数据（只用压缩机运行的周期）"""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cycle_log.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        all_cycles = [json.loads(line) for line in f]
    # 只用压缩机实际运行的周期（compressor_runtime_min > 5min）
    return [c for c in all_cycles if c.get("compressor_runtime_min", 0) > 5]


def fit_rc_model(events):
    """
    拟合 RC 热模型：T[k+1] = T_out + (T[k] - T_out) * exp(-dt/τ) + a * P_comp * (1 - exp(-dt/τ))
    只用压缩机运行的周期（P_comp 准确）
    """
    data = []
    for e in events:
        T_start = e.get("start_temp")
        T_end = e.get("end_temp")
        T_out = e.get("start_outdoor_temp")
        P = e.get("kwh_used", 0) * 1000 / e.get("duration_min", 1) * 60  # W
        dt = e.get("duration_min", 0)
        if T_start is not None and T_end is not None and T_out is not None and dt > 0 and P > 0:
            data.append({"T_start": T_start, "T_end": T_end, "T_out": T_out, "P": P, "dt": dt})

    if len(data) < 5:
        print(f"[WARN] 有效数据不足（{len(data)} 条）")
        return None

    T_start_arr = np.array([d["T_start"] for d in data])
    T_end_arr = np.array([d["T_end"] for d in data])
    T_out_arr = np.array([d["T_out"] for d in data])
    P_arr = np.array([d["P"] for d in data])
    dt_arr = np.array([d["dt"] for d in data])

    def model(x, tau, a):
        T_start, T_out, P, dt = x
        return T_out + (T_start - T_out) * np.exp(-dt / tau) + a * P * (1 - np.exp(-dt / tau))

    try:
        from scipy.optimize import curve_fit
        xdata = np.row_stack([T_start_arr, T_out_arr, P_arr, dt_arr])
        popt, _ = curve_fit(model, xdata, T_end_arr, p0=[30, 0.001], maxfev=10000)
        tau, a = popt

        T_pred = model(xdata, tau, a)
        rmse = np.sqrt(np.mean((T_end_arr - T_pred) ** 2))
        mae = np.mean(np.abs(T_end_arr - T_pred))

        return {
            "tau_min": float(tau),
            "a_cooling": float(a),
            "rmse": float(rmse),
            "mae": float(mae),
            "n_samples": len(data),
        }
    except Exception as e:
        print(f"[ERROR] 拟合失败：{e}")
        return None


def main():
    events = load_cycle_log()
    print(f"压缩机运行周期数：{len(events)}")
    if not events:
        return

    params = fit_rc_model(events)
    if params:
        print(f"\n=== RC 热模型参数 ===")
        print(f"热时间常数 τ：{params['tau_min']:.1f} min")
        print(f"制冷效率 a：{params['a_cooling']:.6f} °C/W")
        print(f"拟合 RMSE：{params['rmse']:.4f} °C")
        print(f"拟合 MAE：{params['mae']:.4f} °C")
        print(f"有效样本数：{params['n_samples']}")

        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_thermal_rc.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2, ensure_ascii=False)
        print(f"\n参数已保存到 {out_path}")
    else:
        print("[FAIL] 拟合失败")


if __name__ == "__main__":
    main()
