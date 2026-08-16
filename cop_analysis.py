#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COP 寻优分析（LongCat 方案3）：从 cycle_log.jsonl 计算每轮除湿效率 COP，
按目标温度分组，找最省电的设定点。

用法: python3 cop_analysis.py
输出: 各组 COP 均值 + 最优 target 建议（样本≥MIN_SAMPLES 才可信）
更新: 2026-08-15 v8.8
"""
import json
import os
import statistics

BASE = os.path.dirname(os.path.realpath(__file__))
CYCLE_LOG = os.path.join(BASE, "cycle_log.jsonl")
MIN_SAMPLES = 3          # 每组至少 N 个样本才参与寻优
COP_VALID_RANGE = (1.0, 8.0)  # COP 合理区间，过滤异常（数据缺失/功率波动）

def load_cycles():
    cycles = []
    if not os.path.exists(CYCLE_LOG):
        return cycles
    with open(CYCLE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    cycles.append(json.loads(line))
                except Exception:
                    pass
    return cycles

def compute_cop(c):
    """ΔAH/kWh。数据缺失或异常返回 None。"""
    d_ah = c.get("start_AH") - c.get("end_AH")
    kwh = c.get("kwh_used") or 0.0
    comp = c.get("compressor_runtime_min") or 0
    if kwh <= 0.1 or comp <= 0:      # 数据缺失（监控中断轮）→ 不可信
        return None
    if d_ah is None or d_ah <= 0:
        return None
    cop = d_ah / kwh
    lo, hi = COP_VALID_RANGE
    if not (lo < cop < hi):          # 异常值（功率数据不全）
        return None
    return cop

def analyze():
    cycles = load_cycles()
    if not cycles:
        print("cycle_log.jsonl 无数据")
        return
    # 按目标温度分组
    groups = {}   # target -> list of cop
    valid_total, invalid = [], 0
    print(f"共 {len(cycles)} 轮，按 target 分组 COP 分析：")
    print(f"{'target':<8} {'样本':<6} {'COP均值':<10} {'COP中位':<10} {'范围'}")
    for c in cycles:
        tgt = c.get("target_temp")
        cop = compute_cop(c)
        if cop is None:
            invalid += 1
            continue
        groups.setdefault(tgt, []).append(cop)
        valid_total.append(cop)
    for tgt in sorted(groups, key=lambda t: (t is None, t)):
        cops = groups[tgt]
        line = f"  {str(tgt):<8} {len(cops):<6} {statistics.mean(cops):<10.2f} {statistics.median(cops):<10.2f} {min(cops):.2f}-{max(cops):.2f}"
        flag = ""
        if len(cops) < MIN_SAMPLES:
            flag = " ⚠️ 样本不足"
        print(line + flag)
    if invalid:
        print(f"\n过滤异常/缺失 {invalid} 轮（kwh/compressor 缺失或 COP 越界）")
    print(f"\n有效样本 {len(valid_total)} 个，全组 COP 均值 {statistics.mean(valid_total):.2f}")
    print(f"\n建议目标温度：", end="")
    reliable = {t: g for t, g in groups.items() if t is not None and len(g) >= MIN_SAMPLES}
    if not reliable:
        print(f"样本不足（各 target 需 ≥{MIN_SAMPLES} 轮），维持现状，continue 积累数据")
    else:
        best = max(reliable, key=lambda t: statistics.mean(reliable[t]))
        print(f"选 {best}°C（COP {statistics.mean(reliable[best]):.2f}），比其他组对比：")
        for t, g in sorted(reliable.items()):
            print(f"    {t}°C: COP {statistics.mean(g):.2f}（{len(g)} 样本）")

if __name__ == "__main__":
    analyze()