#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空调修复效果验证脚本（可重复运行）。

用法:
    python verify_v814_effect.py              # 输出当前对比报告
    python verify_v814_effect.py --snapshot   # 追加当前指标到 _effect_snapshots.jsonl

设计：以修复落地时间为锚点分组对比，几周后重跑即可看到累积效果。

v8.12-v8.14 指标：
  A. 压缩机时长虚高（P1 残留 bug）：compressor_runtime_min > duration_min 的周期
  B. 短周期占比（P2 逃生门早停）：duration < 20min
  C. abort_reason 覆盖率（C 透传）：有 abort_reason 字段的周期占比
  D. 假运行空耗关机（F1 盲区兜底）：watch.log 含"不再吹风空耗"的次数
  E. 谷电启动占比（E 谷电积极版）：谷电时段(22-6)结束的周期占比

v8.21 指标（2026-08-19 白天短循环根治，见 §5b）：
  F. 白天启停频率：白天(7-22)周期数 / 白天小时数 —— 核心指标，目标 < 1.5 次/h
     （诊断基线：08-18 下午实测 2.4 次/h，全天 15 次）
  G. 抖振周期占比：白天 10-14min 且 abort=含水量已达标 的周期 —— 病象特征，应趋近 0
  H. 温度达成率：end_temp <= target_temp 的周期占比 —— 检验"温度达标才收手"是否兑现
     （需 v8.21 起的新字段；旧周期无 end_temp，不计入分母）
  I. 平均压缩机时长：应上升（周期变长是机理），配合 F 下降才算真优化
     —— 若 F 降而 I 没升，说明只是"不开机"而非"开得更有效"，属过度抑制
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
# v8.21 落地时间锚点（白天短循环根治 + cycle_log 温度字段；commit 56ff0da 提交时刻）
V821_EPOCH = "2026-08-19T00:41:32"
DAY_START, DAY_END = 7, 22          # 白天时段（与 ac_watch NIGHT=(23,7) 大致对齐）
CHATTER_MIN, CHATTER_MAX = 10, 14   # 抖振周期时长区间(min)


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


def _hour_of(cycle):
    """周期起始小时；取不到返回 None。"""
    ts = cycle.get("start_ts") or cycle.get("end_ts") or ""
    try:
        return datetime.fromisoformat(ts).hour
    except Exception:
        return None


def is_daytime(cycle):
    h = _hour_of(cycle)
    return h is not None and DAY_START <= h < DAY_END


def analyze_v821(cycles, label):
    """v8.21 白天短循环指标。只看白天周期（夜间走另一套阈值，混算会稀释信号）。"""
    day = [c for c in cycles if is_daytime(c)]
    n = len(day)
    if n == 0:
        return {"label": label, "n": 0}

    # F: 启停频率 = 白天周期数 / 覆盖的白天小时数（按实际出现过的"日期+小时"去重计）
    day_hours = {(c.get("start_ts") or "")[:10] + f"#{_hour_of(c)}" for c in day}
    span_days = len({(c.get("start_ts") or "")[:10] for c in day}) or 1
    starts_per_h = n / max(len(day_hours), 1)
    starts_per_day = n / span_days

    # G: 抖振周期 = 10-14min 且因湿度达标收手（病象特征）
    chatter = sum(
        1 for c in day
        if (c.get("duration_min") or 0) >= CHATTER_MIN
        and (c.get("duration_min") or 0) <= CHATTER_MAX
        and "含水量已达标" in str(c.get("abort_reason") or "")
    )

    # H: 温度达成率（仅 v8.21 起有 end_temp 的周期计入分母）
    with_temp = [c for c in day
                 if c.get("end_temp") is not None and c.get("target_temp") is not None]
    reached = sum(1 for c in with_temp if c["end_temp"] <= c["target_temp"])

    # I: 平均压缩机时长（周期变长是机理；F 降而 I 不升 = 过度抑制）
    comps = [c["compressor_runtime_min"] for c in day
             if c.get("compressor_runtime_min") is not None]

    return {
        "label": label,
        "n": n,
        "span_days": span_days,
        "starts_per_h": round(starts_per_h, 2),        # F
        "starts_per_day": round(starts_per_day, 1),    # F'
        "chatter": chatter,                            # G
        "chatter_pct": round(chatter / n * 100, 1),
        "temp_n": len(with_temp),                      # H 分母
        "temp_reached_pct": (round(reached / len(with_temp) * 100, 1)
                             if with_temp else None),  # H
        "avg_comp": round(sum(comps) / len(comps), 1) if comps else None,  # I
    }


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

    # ── v8.21 白天短循环对比 ──
    b21 = [c for c in cycles if (c.get("end_ts") or c.get("start_ts") or "") < V821_EPOCH]
    a21 = [c for c in cycles if (c.get("end_ts") or c.get("start_ts") or "") >= V821_EPOCH]
    r21 = [analyze_v821(b21, "v8.21前(基线)"), analyze_v821(a21, "v8.21后")]

    print()
    print("=" * 72)
    print("v8.21 白天短循环根治效果（锚点: " + V821_EPOCH + "，仅白天 07-22 周期）")
    print("=" * 72)
    print(f"{'':14s} {'周期':>4s} {'天数':>4s} {'启停F/h':>8s} {'次/天':>6s} "
          f"{'抖振G':>7s} {'温度达成H':>10s} {'均压缩I':>7s}")
    for r in r21:
        if r["n"] == 0:
            print(f"{r['label']:14s} {'-':>4s}  （无白天周期数据）")
            continue
        h = f"{r['temp_reached_pct']}%({r['temp_n']})" if r["temp_reached_pct"] is not None else "无字段"
        print(f"{r['label']:14s} {r['n']:>4d} {r['span_days']:>4d} {r['starts_per_h']:>8.2f} "
              f"{r['starts_per_day']:>6.1f} {r['chatter_pct']:>6.0f}% {h:>10s} "
              f"{(str(r['avg_comp']) + 'm') if r['avg_comp'] is not None else '-':>7s}")

    print("\nv8.21 判定标准（数据够 3+ 天才有统计意义）：")
    print(f"  F 白天启停频率  目标 < 1.5 次/h（诊断基线 2.4 次/h，08-18 全天 15 次）")
    print(f"  G 抖振周期占比  目标趋近 0（10-14min + 因湿度达标收手 = 病象特征）")
    print(f"  H 温度达成率    目标 > 80%（检验'温度达标才收手'兑现；需 v8.21 新字段）")
    print(f"  I 平均压缩机    应**上升**（周期变长是机理）")
    print(f"  ⚠ 若 F 降而 I 未升 → 只是'不开机'而非'开得更有效'，属过度抑制，需回调")

    if a21 and r21[1]["n"] > 0 and r21[0]["n"] > 0:
        f_ok = r21[1]["starts_per_h"] < r21[0]["starts_per_h"]
        i_ok = (r21[1]["avg_comp"] or 0) >= (r21[0]["avg_comp"] or 0)
        if r21[1]["span_days"] < 3:
            print(f"\n  → 结论：数据仅 {r21[1]['span_days']} 天，样本不足，继续观察")
        elif f_ok and i_ok:
            print("\n  → 结论：✅ 启停下降且周期变长，修复按机理生效")
        elif f_ok and not i_ok:
            print("\n  → 结论：⚠ 启停降了但周期没变长，疑似过度抑制，检查是否被次数上限饿死")
        else:
            print("\n  → 结论：❌ 启停未下降，修复未达预期，需重查判据")

    if "--snapshot" in sys.argv:
        snap = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "before": rows[0] if rows[0]["n"] else None,
            "after": rows[1] if rows[1]["n"] else None,
            "f1_off_total": f1_off,
            "v821_before": r21[0] if r21[0]["n"] else None,
            "v821_after": r21[1] if r21[1]["n"] else None,
        }
        with open(SNAPSHOT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        print(f"\n[snapshot] 已追加到 {SNAPSHOT_FILE}")


if __name__ == "__main__":
    main()
