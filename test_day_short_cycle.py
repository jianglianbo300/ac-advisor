#!/usr/bin/env python3
"""v8.21 白天短循环根治 + cycle_log 温度字段 — 回归测试。

诊断依据（cycle_log.jsonl 51 个真实周期，2026-08-19 分析）：
  08-18 下午出现 10 个「12min 开 / 16min 停」周期，一天 15 次启停。
  - 这批周期启动时 RH 仅 56-59%，低于 HUM_DEHUMID_ON=65 也低于谷电线 62
    → 不可能由湿度分支触发，只能是温度分支 temp>=TEMP_COOLING
  - 却用湿度判据(AH<=14.5)收手：一开机 AH 立刻达标，跑满 DUAL_STOP_MIN_COMP=10
    就停，室温根本没压下去 → 16min 后温度又到 → 抖振
  - 白天 AH 平均只回升 1.70 g/m3 就重新触发（夜间需要 2.0 的闭环同量迟滞）

三项修复：
  1. 双轴停止加"温度须达标"前置（DAY_TEMP_REACHED_SLACK）
  2. 湿度启动分支加 AH 迟滞带（DAY_STOP_AH_HYST），与夜间对称
  3. 白天启停次数上限（DAY_MAX_STARTS_PER_H）

另验证 cycle_log 补齐 start_temp/end_temp/outdoor_temp/duty 字段。
纯函数测试 + tmp 文件，不动真空调。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import ac_advisor as A
import ac_watch as W


class R:
    def __init__(self):
        self.ok = self.fail = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            print(f"  [PASS] {name}")
        else:
            self.fail += 1
            print(f"  [FAIL] {name} {detail}")


R_ = R()

print("=" * 70)
print("v8.21 白天短循环根治 — 回归测试")
print("=" * 70)

# ── 1. 常量存在且与夜间对称 ──
print("\n[1] 常量定义与对称性")
R_.check("DAY_STOP_AH_HYST 存在", hasattr(W, "DAY_STOP_AH_HYST"))
R_.check("DAY_TEMP_REACHED_SLACK 存在", hasattr(W, "DAY_TEMP_REACHED_SLACK"))
R_.check("DAY_MAX_STARTS_PER_H 存在", hasattr(W, "DAY_MAX_STARTS_PER_H"))
night_hyst = W.NIGHT_START_AH + W.NIGHT_START_AH_HYST - W.NIGHT_STOP_AH
R_.check(f"白天迟滞带({W.DAY_STOP_AH_HYST})与夜间({night_hyst})对称",
         abs(W.DAY_STOP_AH_HYST - night_hyst) < 0.01,
         f"day={W.DAY_STOP_AH_HYST} night={night_hyst}")
R_.check("白天启停上限严于夜间", W.DAY_MAX_STARTS_PER_H < W.NIGHT_MAX_STARTS_PER_H,
         f"day={W.DAY_MAX_STARTS_PER_H} night={W.NIGHT_MAX_STARTS_PER_H}")

# ── 2. 核心回归：复刻 08-18 抖振场景 ──
# 实录 12:26 周期：start_AH=14.9 RH=58 target=26，压缩机 10min 即停
print("\n[2] 复刻 08-18 抖振：温度未达标时不得因 AH 达标收手")
# 运行中，AH 已达标(13.5<=14.5)、RH 达标(56<=62)、压缩机跑够 10min，
# 但室温 27.5 高于 target 26 + slack 1.0 = 27.0 → 必须继续跑。
# 实录 08-18 下午正是这种「AH 一开机就达标、室温还没压下去」的形态。
mode, tgt, reason = W.decide(
    temp=27.5, hum=56, running=True, since_on=12, since_off=None, is_night=False,
    compressor="compressor", current_target=26, ah=13.5, compressor_run_min=10.0,
)
R_.check("温度未达标(27.5>26.0) → 不关机", mode != "off", f"got mode={mode} reason={reason}")

# 温度已真正达标（25.8 <= target 26）→ 允许按 AH 收手
mode2, _, reason2 = W.decide(
    temp=25.8, hum=56, running=True, since_on=12, since_off=None, is_night=False,
    compressor="compressor", current_target=26, ah=13.5, compressor_run_min=10.0,
)
R_.check("温度已达标 → 允许 AH 收手", mode2 == "off", f"got mode={mode2} reason={reason2}")
R_.check("收手理由是含水量达标", "含水量已达标" in str(reason2), f"got={reason2}")

# 边界：容差是闭区间 temp <= target + SLACK，SLACK=0 → 恰好等于 target 算达标
mode3, _, _ = W.decide(
    temp=26.6, hum=56, running=True, since_on=12, since_off=None, is_night=False,
    compressor="compressor", current_target=26, ah=13.5, compressor_run_min=10.0,
)
mode4, _, _ = W.decide(
    temp=26.0, hum=56, running=True, since_on=12, since_off=None, is_night=False,
    compressor="compressor", current_target=26, ah=13.5, compressor_run_min=10.0,
)
R_.check("26.6 > target 26 → 不停", mode3 != "off", f"got={mode3}")
R_.check("26.0 == target 达标 → 停", mode4 == "off", f"got={mode4}")

# current_target 缺失 → 退化为旧行为（宁可早停不要一直吹）
mode5, _, _ = W.decide(
    temp=27.0, hum=56, running=True, since_on=12, since_off=None, is_night=False,
    compressor="compressor", current_target=None, ah=13.5, compressor_run_min=10.0,
)
R_.check("缺 current_target → 退化早停（不因缺参数一直吹）", mode5 == "off")

# ── 3. 安全类停止不受影响 ──
print("\n[3] 安全类停止优先级不被削弱")
mode, _, reason = W.decide(
    temp=23.0, hum=70, running=True, since_on=5, since_off=None, is_night=False,
    compressor="compressor", current_target=26, ah=18.0, compressor_run_min=3.0,
)
R_.check("过冷逃生门仍无条件关机", mode == "off", f"got={mode} {reason}")
R_.check("逃生门理由正确", "逃生门" in str(reason) or "过冷" in str(reason), f"got={reason}")

# ── 4. 湿度启动分支的 AH 迟滞 ──
print("\n[4] 湿度启动分支 AH 迟滞带")
hyst_line = W.DAY_STOP_AH + W.DAY_STOP_AH_HYST  # 14.5 + 2.0 = 16.5
# 温度必须落在 [TEMP_DEHUMID_LOW, TEMP_COOLING) 区间才隔离出湿度分支：
# 高于 TEMP_COOLING 会走温度分支（那条不受湿度迟滞管），验不出迟滞效果。
# 2026-08-19 启动线 28→27 后，原用例的 27.0 已越界，改用 26.5。
probe_t = (A.TEMP_DEHUMID_LOW + A.TEMP_COOLING) / 2  # 26.5
assert A.TEMP_DEHUMID_LOW <= probe_t < A.TEMP_COOLING, "probe_t 必须只触发湿度分支"
# AH 未回升过迟滞线 → 湿度分支不得开机
mode, _, reason = W.decide(
    temp=probe_t, hum=66, running=False, since_on=None, since_off=60, is_night=False,
    compressor=None, current_target=26, ah=hyst_line - 0.5, compressor_run_min=None,
)
R_.check(f"AH={hyst_line-0.5} < 迟滞线{hyst_line} → 湿度分支不开",
         mode is None, f"got mode={mode} reason={reason}")
# AH 回升过线 → 允许开
mode, _, reason = W.decide(
    temp=probe_t, hum=66, running=False, since_on=None, since_off=60, is_night=False,
    compressor=None, current_target=26, ah=hyst_line + 0.5, compressor_run_min=None,
)
R_.check(f"AH={hyst_line+0.5} >= 迟滞线 → 湿度分支放行",
         mode == "cooling", f"got mode={mode} reason={reason}")

# 温度分支不被湿度迟滞拦住（热了就该开）
mode, _, reason = W.decide(
    temp=29.0, hum=50, running=False, since_on=None, since_off=60, is_night=False,
    compressor=None, current_target=26, ah=13.0, compressor_run_min=None,
)
R_.check("温度分支不受 AH 迟滞影响（热了就该开）", mode == "cooling",
         f"got mode={mode} reason={reason}")

# ── 5. 白天启停次数上限 ──
print("\n[5] 白天启停次数上限")
now = datetime.now()
recent = [(now - timedelta(minutes=m)).isoformat(timespec="seconds") for m in (10, 30)]
# 用 28.5°C：既触发温度分支(>=TEMP_COOLING 28)，又低于安全阀(29.0)，
# 才能真正验证次数上限的拦截/放行。27.5 达不到任何启动线，验不出闸门效果。
mode, _, reason = W.decide(
    temp=28.5, hum=60, running=False, since_on=None, since_off=60, is_night=False,
    compressor=None, current_target=26, ah=16.0, compressor_run_min=None,
    night_comp_starts=recent,
)
R_.check(f"抖振温区达上限({len(recent)}>={W.DAY_MAX_STARTS_PER_H}) → 不开",
         mode is None, f"got mode={mode} reason={reason}")
mode, _, _ = W.decide(
    temp=28.5, hum=60, running=False, since_on=None, since_off=60, is_night=False,
    compressor=None, current_target=26, ah=16.0, compressor_run_min=None,
    night_comp_starts=recent[:1],
)
R_.check("1h 内仅 1 次 → 允许开", mode == "cooling", f"got={mode}")

# 安全阀：真热(>=29)时突破次数上限，不能因为压次数把人热着
many = [(now - timedelta(minutes=m)).isoformat(timespec="seconds") for m in (5, 20, 40)]
mode, _, reason = W.decide(
    temp=W.DAY_STARTS_OVERRIDE_T, hum=60, running=False, since_on=None, since_off=60,
    is_night=False, compressor=None, current_target=26, ah=16.0,
    compressor_run_min=None, night_comp_starts=many,
)
R_.check(f"安全阀：{W.DAY_STARTS_OVERRIDE_T}°C 且已启动 {len(many)} 次 → 仍放行",
         mode == "cooling", f"got mode={mode} reason={reason}")
mode, _, _ = W.decide(
    temp=W.DAY_STARTS_OVERRIDE_T - 0.1, hum=60, running=False, since_on=None,
    since_off=60, is_night=False, compressor=None, current_target=26, ah=16.0,
    compressor_run_min=None, night_comp_starts=many,
)
R_.check(f"{W.DAY_STARTS_OVERRIDE_T-0.1}°C 未达安全阀 → 仍受上限约束",
         mode is None, f"got={mode}")

# 夜间仍用夜间上限（不被白天上限误伤）
mode, _, _ = W.decide(
    temp=29.0, hum=60, running=False, since_on=None, since_off=60, is_night=True,
    compressor=None, current_target=26, ah=16.0, compressor_run_min=None,
    night_comp_starts=recent,
)
R_.check("夜间 2 次未达夜间上限 4 → 仍可开", mode == "cooling", f"got={mode}")

# ── 6. cycle_log 温度字段 ──
print("\n[6] cycle_log 补齐温度与 duty 字段")
tmpdir = tempfile.mkdtemp(prefix="cyc_")
p = os.path.join(tmpdir, "cycle_log.jsonl")
stt = {"estimated_kwh": 1.0}
W.open_cycle(stt, "2026-08-19T14:00:00", 16.0, 62, temp=28.5, outdoor_temp=33.0)
cs = stt["cycle_start"]
R_.check("open_cycle 记录 temp", cs.get("temp") == 28.5, f"got={cs}")
R_.check("open_cycle 记录 outdoor_temp", cs.get("outdoor_temp") == 33.0)

stt["estimated_kwh"] = 1.5
ok = W.close_cycle(stt, "2026-08-19T14:40:00", 13.5, 55, 26, 38.0,
                   path=p, temp=25.8, outdoor_temp=32.0)
R_.check("close_cycle 写盘成功", ok is True)
rec = json.loads(open(p, encoding="utf-8").read().strip())
for k, v in (("start_temp", 28.5), ("end_temp", 25.8),
             ("start_outdoor_temp", 33.0), ("end_outdoor_temp", 32.0)):
    R_.check(f"{k} = {v}", rec.get(k) == v, f"got={rec.get(k)}")
R_.check("duty 已计算", rec.get("duty") == round(38.0/40.0, 3), f"got={rec.get('duty')}")
R_.check("duty_invalid=False（正常周期）", rec.get("duty_invalid") is False)
R_.check("温差可复盘（降 2.7°C）", abs((rec["start_temp"] - rec["end_temp"]) - 2.7) < 0.01)

# duty > 1 的脏数据要被标记
stt2 = {"estimated_kwh": 0.0}
W.open_cycle(stt2, "2026-08-19T15:00:00", 16.0, 62, temp=28.0, outdoor_temp=33.0)
p2 = os.path.join(tmpdir, "cycle_log2.jsonl")
W.close_cycle(stt2, "2026-08-19T15:12:00", 14.0, 58, 26, 24.0,
              path=p2, temp=27.0, outdoor_temp=33.0)
rec2 = json.loads(open(p2, encoding="utf-8").read().strip())
R_.check("占空比 24/12=2.0 被标记 duty_invalid", rec2.get("duty_invalid") is True,
         f"duty={rec2.get('duty')} invalid={rec2.get('duty_invalid')}")

# ── 7. 闭环模拟：温度达标才收手能否真正减少启停 ──
print("\n[7] 闭环模拟：新旧策略对比（这才是主效应）")
# 前面的用例只喂"未运行"状态，验不到核心改动——温度闸门的作用是让**单个周期变长**，
# 周期长了启停自然少。必须闭环（制冷降温 / 停机回温）才能测出来。
# 速率取实测量级：制冷 0.12°C/min、回温 0.05°C/min（65平米 1.5匹 上海夏天）。
def closed_loop(use_temp_gate, hours=6):
    cool_rate, warm_rate = 0.12, 0.05
    temp, target, ah = 28.5, 26, 15.0
    running, comp, since_off = False, 0.0, 99.0
    t0 = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    cyc_starts, comp_total, t_min = [], 0.0, 99.0
    for tick in range(hours * 60):
        now_t = t0 + timedelta(minutes=tick)
        live = [s for s in cyc_starts
                if datetime.fromisoformat(s) >= now_t - timedelta(hours=1)]
        ah = max(12.5, ah - 0.06) if running else min(16.5, ah + 0.03)
        hum = min(75, 50 + (ah - 12.5) * 2.5)
        m, tg, _ = W.decide(
            temp=temp, hum=hum, running=running,
            since_on=comp if running else None,
            since_off=None if running else since_off,
            is_night=False, compressor="compressor" if running else None,
            # current_target=None 时代码退化为旧行为（AH 达标即停）→ 作为对照组
            current_target=target if use_temp_gate else None,
            ah=ah, compressor_run_min=comp if running else None,
            night_comp_starts=live,
        )
        if not running and m == "cooling":
            running, comp, since_off, target = True, 0.0, 0.0, tg or 26
            cyc_starts.append(now_t.isoformat(timespec="seconds"))
        elif running and m == "off":
            running, since_off = False, 0.0
        if running:
            comp += 1
            comp_total += 1
            temp -= cool_rate
        else:
            temp += warm_rate
            since_off += 1
        temp = max(22.0, min(31.0, temp))
        t_min = min(t_min, temp)
    return {"starts": len(cyc_starts), "comp": comp_total,
            "avg_cycle": comp_total / max(len(cyc_starts), 1), "t_min": t_min}

old = closed_loop(False)
new = closed_loop(True)
print(f"     旧行为(AH达标即停) : {old['starts']:2} 次启停, 压缩机 {old['comp']:.0f}min, "
      f"均周期 {old['avg_cycle']:.1f}min, 最低 {old['t_min']:.1f}°C")
print(f"     v8.21(温度达标收手): {new['starts']:2} 次启停, 压缩机 {new['comp']:.0f}min, "
      f"均周期 {new['avg_cycle']:.1f}min, 最低 {new['t_min']:.1f}°C")
R_.check("启停次数显著下降", new["starts"] < old["starts"],
         f"{old['starts']} → {new['starts']}")
R_.check("单周期变长（这是机理）", new["avg_cycle"] > old["avg_cycle"] * 1.2,
         f"{old['avg_cycle']:.1f} → {new['avg_cycle']:.1f} min")
R_.check("制冷量没有白费（压缩机总时长基本持平）",
         abs(new["comp"] - old["comp"]) / max(old["comp"], 1) < 0.15,
         f"{old['comp']:.0f} → {new['comp']:.0f} min")
R_.check(f"未过冷（最低 {new['t_min']:.1f}°C > 逃生门 {A.TEMP_ABSOLUTE_FLOOR}°C）",
         new["t_min"] > A.TEMP_ABSOLUTE_FLOOR,
         f"t_min={new['t_min']:.1f}")
R_.check(f"未触发过冷保护线 {W.DAY_COOL_STOP_T}°C", new["t_min"] > W.DAY_COOL_STOP_T)

# 对照：真热(29.5)整段都该放行，不能被次数上限压住
starts_hot = []
for i in range(10):
    t = now.replace(hour=14) + timedelta(minutes=16 * i)
    cutoff = t - timedelta(hours=1)
    live = [s for s in starts_hot if datetime.fromisoformat(s) >= cutoff]
    mode, _, _ = W.decide(
        temp=29.5, hum=58, running=False, since_on=None, since_off=16, is_night=False,
        compressor=None, current_target=26, ah=14.9, compressor_run_min=None,
        night_comp_starts=live,
    )
    if mode == "cooling":
        starts_hot.append(t.isoformat(timespec="seconds"))
print(f"     对照 29.5°C 真热: 开机 {len(starts_hot)}/10 次")
R_.check("真热时安全阀全程放行", len(starts_hot) == 10, f"仅 {len(starts_hot)} 次")

print("\n" + "=" * 70)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 70)
sys.exit(1 if R_.fail else 0)
