#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周空调修复效果快照 + 自动解读（cron: ce70b33967b6 周一 03:00, no-agent 微信投递）。

1. 跑 verify_v814_effect.py --snapshot 追加当周数据
2. 对比修复前基线，输出各指标达标结论（✅/⚠️）
3. 修复后周期不足 30 时提示进度，够了才下完整结论
stdout 即投递内容（微信）。
"""
import json
import os
import subprocess
import sys
from datetime import datetime

BASE = r"D:\work\ac-advisor"
SNAP_FILE = os.path.join(BASE, "_effect_snapshots.jsonl")

# 基线（2026-08-16 首份快照，修复前 15 周期）
BASELINE = {"n": 15, "inflated": 1, "short_pct": 40.0, "abort_pct": 0.0,
            "valley_pct": 20.0, "total_kwh": 8.36, "avg_dur": 27.2}
TARGET_N = 30  # 攒够多少修复后周期才下可靠结论

# v8.21 白天短循环基线（2026-08-19 落地前实测，40 个白天周期 / 4 天）
# 来源：verify_v814_effect.py 的 analyze_v821，锚点 2026-08-19T00:41:32
BASELINE_V821 = {"n": 40, "span_days": 4, "starts_per_h": 1.25,
                 "starts_per_day": 10.0, "chatter_pct": 32.0, "avg_comp": 16.9}
TARGET_DAYS_V821 = 3   # 白天数据至少覆盖 3 天才下结论


def run_verify():
    script = os.path.join(BASE, "verify_v814_effect.py")
    out = subprocess.run([sys.executable, script, "--snapshot"], cwd=BASE,
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120)
    return out


def _snaps():
    if not os.path.exists(SNAP_FILE):
        return []
    out = []
    for line in open(SNAP_FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def latest_after():
    """取最新快照里的修复后指标（v8.12 系列）。"""
    snaps = _snaps()
    return snaps[-1].get("after") if snaps else None


def latest_v821():
    """取最新快照里的 v8.21 白天短循环指标。"""
    snaps = _snaps()
    return snaps[-1].get("v821_after") if snaps else None


def judge_v821(a):
    """v8.21 白天短循环判定。返回 (lines, verdict)。

    核心是 F(启停频率) 与 I(平均压缩机时长) 的组合：
    F 降 + I 升 = 周期变长导致启停变少 = 按机理生效；
    F 降 + I 平/降 = 只是"不开机"，属被次数上限饿死的过度抑制，要回调。"""
    lines = []
    if not a or not a.get("n"):
        return ["⏳ v8.21 后暂无白天周期数据（刚落地属正常）"], "pending"
    days = a.get("span_days") or 0
    if days < TARGET_DAYS_V821:
        lines.append(f"⏳ v8.21 后仅 {days} 天白天数据（需 ≥{TARGET_DAYS_V821} 天），继续观察")
        lines.append(f"   当前: 启停 {a.get('starts_per_h')}次/h · 抖振 {a.get('chatter_pct')}% "
                     f"· 均压缩 {a.get('avg_comp')}min")
        return lines, "pending"

    b = BASELINE_V821
    f_now, f_base = a.get("starts_per_h") or 0, b["starts_per_h"]
    i_now, i_base = a.get("avg_comp") or 0, b["avg_comp"]
    g_now, g_base = a.get("chatter_pct") or 0, b["chatter_pct"]
    h_now = a.get("temp_reached_pct")

    f_ok, i_ok, g_ok = f_now < f_base, i_now >= i_base, g_now < g_base * 0.5
    lines.append(f"{'✅' if f_ok else '⚠️'} F 白天启停频率: {f_now}次/h（基线 {f_base}）应下降")
    lines.append(f"{'✅' if g_ok else '⚠️'} G 抖振周期占比: {g_now}%（基线 {g_base}%）应趋近 0")
    if h_now is not None:
        lines.append(f"{'✅' if h_now > 80 else '⚠️'} H 温度达成率: {h_now}%（目标 >80%）")
    else:
        lines.append("➖ H 温度达成率: 无 end_temp 字段（v8.21 前的旧周期）")
    lines.append(f"{'✅' if i_ok else '⚠️'} I 平均压缩机: {i_now}min（基线 {i_base}）应上升")

    if f_ok and i_ok:
        verdict = "ok"
        lines.append("→ 启停下降且周期变长，修复按机理生效")
    elif f_ok and not i_ok:
        verdict = "over_suppressed"
        lines.append("→ ⚠️ 启停降了但周期没变长，疑似被次数上限饿死（过度抑制）")
        lines.append("   建议：调高 DAY_MAX_STARTS_PER_H 或降低 DAY_STARTS_OVERRIDE_T")
    else:
        verdict = "failed"
        lines.append("→ ❌ 启停未下降，需重查判据（DAY_TEMP_REACHED_SLACK / AH 迟滞）")
    return lines, verdict


def judge(after):
    """对比基线输出结论行。返回 (lines, all_ok)。

    2026-08-19：A/B 两项移交 v8.21 段判定，此处降级为「趋势展示」不再计入 all_ok。
    原因：
      B 短周期占比(<20min) —— 当时视为"逃生门早停"缺陷，但 08-19 的 51 周期诊断
        证明白天短周期的根因是判据错配（温度驱动开机却用湿度收手），已由 v8.21
        正面修复，并改用更精准的 F(启停频率)/G(抖振占比)/I(均压缩时长) 三指标衡量。
        继续用"<20min 占比"判定会与 v8.21 段重复告警且口径更粗。
      A 压缩机时长虚高 —— 残留的唯一一条是 08-16T17:14，恰为 v8.12 落地那一刻的
        跨界周期（边界效应，非持续 bug）。v8.21 起 close_cycle 直接写 duty_invalid
        标记，由分析端剔除，不必再靠这里报警。
    """
    lines = []
    checks = [
        ("C abort 覆盖率", after.get("abort_pct", 0), BASELINE["abort_pct"],
         lambda v: v >= 90, "应≥90%（停机原因可审计）"),
        ("E 谷电占比", after.get("valley_pct", 0), BASELINE["valley_pct"],
         lambda v: v > BASELINE["valley_pct"], f"应高于基线 {BASELINE['valley_pct']}%"),
    ]
    all_ok = True
    for name, val, base, ok_fn, expect in checks:
        ok = ok_fn(val)
        all_ok = all_ok and ok
        mark = "✅" if ok else "⚠️"
        lines.append(f"{mark} {name}: {val}（基线 {base}）{expect}")
    # 趋势展示（不判定，详见 v8.21 段）
    lines.append(f"➖ A 压缩机时长虚高: {after.get('inflated', '?')}（边界效应，v8.21 起靠 duty_invalid 剔除）")
    lines.append(f"➖ B 短周期占比: {after.get('short_pct', '?')}%（口径已由 v8.21 的 F/G/I 取代）")
    return lines, all_ok


def main():
    out = run_verify()
    after = latest_after()
    v821 = latest_v821()

    print("🏠 空调修复效果周报")
    print(f"📅 {datetime.now():%m-%d}")
    if out.returncode != 0:
        print("⚠️ 快照脚本异常：")
        print((out.stderr or "")[-300:])
        return

    # ── 第一部分：v8.12-v8.15 系列 ──
    if after is None or after.get("n", 0) == 0:
        print("⚠️ 尚无修复后周期数据")
    else:
        n = after["n"]
        print(f"📊 v8.12+ 已积累 {n} 周期（目标 {TARGET_N}，还需 {max(0, TARGET_N - n)}）")
        print(f"   均时长 {after.get('avg_dur', '?')}m | 总耗电 {after.get('total_kwh', '?')}kWh")
        if n < TARGET_N:
            print("   数据不足，仅存档不判定。")
            print(f"   （趋势：虚高 {after.get('inflated', '?')} | 短周期 {after.get('short_pct', '?')}%"
                  f" | abort {after.get('abort_pct', '?')}% | 谷电 {after.get('valley_pct', '?')}%）")
        else:
            lines, all_ok = judge(after)
            print("")
            for l in lines:
                print(l)
            print("🎉 全部指标达标，v8.12-v8.15 修复效果确认！" if all_ok
                  else "🔍 部分指标未达标，建议看下 ac_watch.log 排查")

    # ── 第二部分：v8.21 白天短循环（独立判定，不受上面 TARGET_N 门槛影响）──
    print("")
    print("── v8.21 白天短循环 ──")
    lines21, verdict = judge_v821(v821)
    for l in lines21:
        print(l)
    if verdict == "over_suppressed":
        print("‼️ 需要回调参数，别放着不管")
    elif verdict == "failed":
        print("‼️ 修复未生效，需重新诊断")


if __name__ == "__main__":
    main()
