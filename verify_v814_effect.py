#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v8.12-v8.14 修复效果验证脚本（可重复运行）。

用法:
    python verify_v814_effect.py              # 输出当前对比报告
    python verify_v814_effect.py --snapshot   # 追加当前指标到对比存档 _effect_snapshots.jsonl

设计：以 v8.12 落地时间（2026-08-16 17:14 前的周期为"修复前基线"）分两组，
对比 5 个关键指标。几周后重跑即可看到修复后阶段的累积效果。
指标含义：
  A. 压缩机时长虚高（P1 残留 bug）：compressor_runtime_min > duration_min 的周期
  B. 短周期占比（P2 逃生门早停）：duration < 20min
  C. abort_reason 覆盖率（C 透传）：有 abort_reason 字段的周期占比
  D. 假运行空耗关机（F1 盲区兜底）：watch.log 含"不再吹风空耗"的次数
  E. 谷电启动占比（E 谷电积极版）：谷电时段(22-6)结束的周期占比
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CYCLE_LOG = os.path.join(BASE, "cycle_log.jsonl")
WATCH_LOG = os.path.join(BASE, "ac_watch.log")
SNAPSHOT_FILE = os.path.join(BASE, "_effect_snapshots.jsonl")

# v8.12 落地时间锚点（OpenCode 会话 2026-08-16 17:14 起有 abort_reason 透传）
V812_EPOCH = "2026-08-16T17:14:13"


def load_cycles():
    cycles = []
    if not os.path.exists(CYCLE_LOG):
        return cycles
    for line in open(CYCLE_LOG, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            cycles.append(json.loads(line))
        except Exception:
            pass
    return cycles


def load_watch_log():
    if not os.path.exists(WATCH_LOG):
        return ""
    try:
        with open(WATCH_LOG, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def is_after_fix(cycle):
    """按 end_ts 判断周期是否属于 v8.12 修复后。"""
    ts = cycle.get("end_ts") or cycle.get("start_ts") or ""
    return ts >= V812_EPOCH


def analyze(cycles, label, watch_log):
    """对一组周期计算指标。返回 dict。"""
    n = len(cycles)
    if n == 0:
        return {"label": label, "n": 0}
    inflated = 0      # A: comp 时长虚高
    short = 0         # B: 短周期
    abort_n = 0       # C: 有 abort_reason
    valley_n = 0      # E: 谷电结束
    total_kwh = 0.0
    for c in cycles:
        dur = c.get("duration_min") or 0
        comp = c.get("compressor_runtime_min")
        if comp is not None and dur > 0 and comp > dur + 2:
            inflated += 1
        if dur < 20:
            short += 1
        if c.get("abort_reason"):
            abort_n += 1
        ts = c.get("end_ts") or ""
        try:
            h = datetime.fromisoformat(ts).hour
            if h >= 22 or h < 6:
                valley_n += 1
        except Exception:
            pass
        total_kwh += c.get("kwh_used") or 0
    return {
        "label": label,
        "n": n,
        "inflated": inflated,              # A
        "short": short,                    # B
        "short_pct": round(short / n * 100, 1),
        "abort_pct": round(abort_n / n * 100, 1),  # C
        "valley_pct": round(valley_n / n * 100, 1),  # E
        "total_kwh": round(total_kwh, 2),
        "avg_dur": round(sum(c.get("duration_min") or 0 for c in cycles) / n, 1),
    }


def main():
    cycles = load_cycles()
    watch_log = load_watch_log()
    if not cycles:
        print("[warn] cycle_log.jsonl 为空或不存在")
        cycles = []

    before = [c for c in cycles if not is_after_fix(c)]
    after = [c for c in cycles if is_after_fix(c)]

    # D: 假运行空耗关机次数（F1 兜底，仅修复后应有）
    f1_off = watch_log.count("不再吹风空耗")
    d_before = 0
    d_after = f1_off

    print("=" * 72)
    print("v8.12-v8.14 修复效果对比（锚点: v8.12 落地 " + V812_EPOCH + "）")
    print("=" * 72)
    rows = [analyze(before, "修复前(基线)", watch_log), analyze(after, "修复后", watch_log)]
    print(f"{'':12s} {'周期':>4s} {'虚高A':>5s} {'短周B':>6s} {'abortC':>6s} {'谷电E':>6s} {'均时长':>6s} {'总kWh':>7s}")
    for r in rows:
        if r["n"] == 0:
            print(f"{r['label']:12s} {'-':>4s}  （无数据）")
            continue
        print(f"{r['label']:12s} {r['n']:>4d} {r['inflated']:>5d} {r['short_pct']:>5.0f}% "
              f"{r['abort_pct']:>5.0f}% {r['valley_pct']:>5.0f}% {r['avg_dur']:>5.0f}m {r['total_kwh']:>7.2f}")
    print(f"\nD. 假运行空耗关机次数（F1 兜底）：修复前 ~{d_before}，修复后 {d_after}")

    print("\n判定参考：")
    print("  A 虚高应趋近 0（P1 残留 bug 已修）")
    print("  B 短周期(<20min)占比应下降（P2 逃生门早停缓解）")
    print("  C abort_reason 覆盖率应上升（停机原因可审计）")
    print("  D 修复后应出现非零空耗关机（盲区兜底生效）")
    print("  E 谷电占比应逐步上升（谷电积极版生效）")

    if "--snapshot" in sys.argv:
        snap = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "before": rows[0] if rows[0]["n"] else None,
            "after": rows[1] if rows[1]["n"] else None,
            "f1_off_total": f1_off,
        }
        with open(SNAPSHOT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        print(f"\n[snapshot] 已追加到 {SNAPSHOT_FILE}")


if __name__ == "__main__":
    main()
