#!/usr/bin/env python3
"""v8.22 夜间「湿度达标关机」补温度前置 — 回归测试。

真实故障（2026-08-19 凌晨，用户："咋给我关了？"）：
  室内 27.0°C 用户觉得热，手动/系统开机后 20 秒即被 cron 关掉。
  ac_watch.log:
    01:20:37 执行 off target=None → action 关机 · 压缩机运行 夜间 T=27.0 ...
  decide 返回的理由是「夜间室内湿度已达标（AH=13.4），关机省电」。

根因：夜间停止分支只看 AH（<=NIGHT_STOP_AH 14.0），完全不看温度。屋里干
      != 屋里不热。且该分支位于 MIN_RUN 守卫之前（当"安全类"处理），
      NIGHT_MIN_COMP_ON=20min 地板也拦不住 → 开机即被关。
      与 v8.21 修的白天病同源：用湿度判据关一台因为热而开的空调。

同时验证同批阈值调整（用户在 27.0°C 反馈热）：
  NIGHT_START_T 28.0 -> 27.0
  TEMP_COOLING     28 -> 27
  TEMP_COOLING_HOT_DAY 废弃（1°C 分辨率下 26.5 等价 27 = 伪精度），
    改为 HOT_DAY_TARGET_FLOOR=24：炎热日不提前开、而是目标多压 1°C（死区 3°C）
  白天开机目标下限 26 -> 25（恢复 2°C 死区，见 test_day_short_cycle [7]）

纯函数测试，不动真空调。
"""
import sys

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
print("v8.22 夜间湿度关机补温度前置 — 回归测试")
print("=" * 70)

# ── 1. 核心回归：复刻真实故障 ──
print("\n[1] 复刻故障：27°C 热着 + AH 达标 → 不得关机")
# 真实现场：T=27.0 RH=51% AH=13.4，target=25，压缩机刚转起来
mode, _, reason = W.decide(
    temp=27.0, hum=51, running=True, since_on=0.3, since_off=None, is_night=True,
    compressor="compressor", current_target=25, ah=13.4, compressor_run_min=0.3,
)
R_.check("27°C 时不再因湿度达标关机", mode != "off", f"got mode={mode} reason={reason}")
R_.check("不再出现'湿度已达标'关机理由", "湿度已达标" not in str(reason), f"got={reason}")

# ── 2. 温度真达标后，省电逻辑必须仍然生效 ──
print("\n[2] 温度达标后按湿度收手（省电不能丢）")
mode, _, reason = W.decide(
    temp=24.5, hum=48, running=True, since_on=30, since_off=None, is_night=True,
    compressor="compressor", current_target=25, ah=13.0, compressor_run_min=30,
)
R_.check("24.5°C <= target 25 → 允许关机", mode == "off", f"got mode={mode}")
R_.check("理由仍是夜间湿度达标", "湿度已达标" in str(reason), f"got={reason}")

# ── 3. 边界：SLACK=0 是闭区间 ──
print("\n[3] 温度边界（SLACK=%.1f）" % W.DAY_TEMP_REACHED_SLACK)
edge = 25 + W.DAY_TEMP_REACHED_SLACK
mode_at, _, _ = W.decide(
    temp=edge, hum=48, running=True, since_on=30, since_off=None, is_night=True,
    compressor="compressor", current_target=25, ah=13.0, compressor_run_min=30,
)
mode_over, _, _ = W.decide(
    temp=edge + 0.1, hum=48, running=True, since_on=30, since_off=None, is_night=True,
    compressor="compressor", current_target=25, ah=13.0, compressor_run_min=30,
)
R_.check(f"{edge}°C 恰好达标 → 关", mode_at == "off", f"got={mode_at}")
R_.check(f"{edge + 0.1}°C 未达标 → 不关", mode_over != "off", f"got={mode_over}")

# ── 4. 缺 current_target 时退化旧行为（不要整夜空转） ──
print("\n[4] 缺 target 退化保护")
mode, _, _ = W.decide(
    temp=27.0, hum=51, running=True, since_on=30, since_off=None, is_night=True,
    compressor="compressor", current_target=None, ah=13.0, compressor_run_min=30,
)
R_.check("current_target=None → 退化早停（宁可早停不空转）", mode == "off", f"got={mode}")

# ── 5. 安全类关机不受削弱 ──
print("\n[5] 安全类关机优先级保持")
mode, _, reason = W.decide(
    temp=23.0, hum=70, running=True, since_on=5, since_off=None, is_night=True,
    compressor="compressor", current_target=25, ah=18.0, compressor_run_min=3,
)
R_.check("过冷逃生门仍无条件关机", mode == "off", f"got={mode} {reason}")

# ── 6. 阈值调整生效（用户在 27.0°C 反馈热）──
print("\n[6] 启动阈值下调")
R_.check(f"NIGHT_START_T = 27.0（原 28.0）", W.NIGHT_START_T == 27.0,
         f"got={W.NIGHT_START_T}")
R_.check(f"TEMP_COOLING = 27（原 28）", A.TEMP_COOLING == 27, f"got={A.TEMP_COOLING}")
# 炎热日改为「压低目标」而非「提前启动」：传感器 1°C 分辨率下提前一档会把死区
# 压到 1°C 重新引发抖振，故 TEMP_COOLING_HOT_DAY 已废弃，改用 HOT_DAY_TARGET_FLOOR。
R_.check("炎热日目标下限严于常规（多压一度）",
         W.HOT_DAY_TARGET_FLOOR < 25,
         f"hot_floor={W.HOT_DAY_TARGET_FLOOR}")
R_.check("废弃的 TEMP_COOLING_HOT_DAY 已移除（1°C 分辨率下是伪精度）",
         not hasattr(W, "TEMP_COOLING_HOT_DAY"))
# 炎热日实际行为：室外 32°C、室内 27°C → 目标应为 24（死区 3°C）
_m, _t, _r = W.decide(
    temp=27.0, hum=50, running=False, since_on=None, since_off=99, is_night=False,
    compressor=None, current_target=26, ah=13.0, compressor_run_min=None,
    night_comp_starts=[], outdoor_temp=32.0,
)
R_.check(f"炎热日 27°C 开机目标 = {W.HOT_DAY_TARGET_FLOOR}（死区 {27 - W.HOT_DAY_TARGET_FLOOR}°C）",
         _m == "cooling" and _t == W.HOT_DAY_TARGET_FLOOR, f"got mode={_m} target={_t}")
# 非炎热日同温度 → 目标 25
_m2, _t2, _ = W.decide(
    temp=27.0, hum=50, running=False, since_on=None, since_off=99, is_night=False,
    compressor=None, current_target=26, ah=13.0, compressor_run_min=None,
    night_comp_starts=[], outdoor_temp=25.0,
)
R_.check("非炎热日 27°C 开机目标 = 25", _m2 == "cooling" and _t2 == 25,
         f"got mode={_m2} target={_t2}")

# 27.0°C 必须能触发夜间开机（这是用户的核心诉求）
ah_27 = W.absolute_humidity(27.0, 51)
mode, tgt, reason = W.decide(
    temp=27.0, hum=51, running=False, since_on=None, since_off=99, is_night=True,
    compressor=None, current_target=26, ah=ah_27, compressor_run_min=None,
    night_comp_starts=[],
)
R_.check("27.0°C 夜间会自动开机", mode == "cooling", f"got mode={mode} reason={reason}")
R_.check(f"目标 {tgt}°C 低于室温至少 2°C（定频机才转得起来）",
         tgt is not None and 27.0 - tgt >= 2, f"target={tgt}")

# ── 7. 死区：启动线与收手线之间要留够回温时间 ──
print("\n[7] 温度死区（决定启停频率）")
# 白天路径：27°C 开机的目标
day_target = round(max(25, min(28, 27.0 - 2)))
day_dead = A.TEMP_COOLING - day_target
R_.check(f"白天死区 {day_dead}°C >= 2（27 开机 → 目标 {day_target}）",
         day_dead >= 2, f"死区仅 {day_dead}°C，回温 {day_dead/0.05:.0f}min 就重触发")
# 夜间路径
night_target = max(W.NIGHT_MIN_TARGET, min(W.NIGHT_TARGET, round(27.0 - 2)))
night_dead = W.NIGHT_START_T - night_target
R_.check(f"夜间死区 {night_dead}°C >= 2（27 开机 → 目标 {night_target}）",
         night_dead >= 2, f"死区仅 {night_dead}°C")

print("\n" + "=" * 70)
print(f"结果：{R_.ok} passed, {R_.fail} failed")
print("=" * 70)
sys.exit(1 if R_.fail else 0)
