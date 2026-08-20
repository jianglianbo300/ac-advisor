#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空调自动监控 v8.16 — 每 2 分钟自动闭环（Hermes cron: */2 * * *）

v8.16 数据可信度 + 白天双轴停止（2026-08-16，ZCode 深查落地）：
  1. kWh 严格 gap：积分仅在相邻有效采样 ≤10min 内（KWH_MAX_GAP_MIN）——
     原跨空窗用 stale 压缩机功率外推，开机首读虚记 ~0.5 度（16:30→16:32 实录 2min 虚记 0.62 度），
     cycle_log kwh_used 系统性虚高 2-4 倍，污染 ΔAH/kWh 效率模型
  2. 白天双轴停止：AH≤14.5 且 RH≤62 且压缩机≥10min → 关（DAY_STOP_AH/DAY_EXIT_RH_MAX/
     DUAL_STOP_MIN_COMP）——RH 单轴在低热负荷时段会拖到过冷逃生门才停（16:30 周期：
     AH 6min 达标，T 12min 冲 23°C）；AH 达标即收手，少吹冷少耗电，与夜间 AH 逻辑对齐

v8.15 fail-safe（2026-08-16，AGENTS.md 待办落地）：
  1. load_state 损坏不再静默 → 打印 ERROR + _state_load_failed 标记
  2. ac_watch 检测到标记 → 本次 tick 不执行开/关（防丢 MIN_OFF 锚点）

v8.14 E 方案（谷电积极版，2026-08-16）：
  1. 峰谷电价感知：22-6 谷电半价时段除湿启动阈值 65→62，更早压一轮省电费
  2. 峰电时段维持原阈值，不推迟不牺牲舒适（E 原案"峰电不新开除湿"风险大，改用积极版）

v8.13 修复（2026-08-16，glm-5.2 + nemotron-3-ultra-free 交叉审查）：
  1. P1: 假运行盲区 56~66%RH —— 压缩机停后湿度落在该区间既不重启也不关机
     → 兜底：停运≥10min 且 RH≤66% 直接关机，杜绝风扇空耗
  2. P2: 虚拟变频升温不写冷却锁 → Tier2 立即降回造成震荡
     → 升温/降温都更新 last_dehumid_adjust_at
  3. P2: 夜间 AH 启动目标被 clamp 到室温 → 定频机压缩机不转
     → 守卫：temp - night_target >= 1°C 才允许启动

v8.11 修复（2026-08-16）：
  1. P0: 高湿+T≥28 时纯制冷26°C优先于除湿24°C，导致湿度反弹→逃生门关机的死循环
     → 修复：T≥28且RH≥65时强制24°C除湿优先，不再走26°C纯制冷
  2. P0: 夜间停止条件 T≤26°C 太激进，导致60%RH就停、反复短循环（01:38/03:10/04:44）
     → 修复：夜间停止改为湿度驱动（RH≤55%才停），温度只保留24°C逃生门

v8.10 审计修复（Qwen3.8-Max 交叉审查 2026-08-16）：
  1. P0: 假运行加计数器，连续 3 次后停机告警（防无限循环）
  2. P1: 传感器断连超时升级（>20min 且运行中 → 保守关机）
  3. P2: 传感器越界值校验（10°C≤T≤45°C, 20%≤RH≤98%）
  4. P2: 手动锚点加 12h TTL（过期后恢复自动逻辑）
  5. P3: cron 并发加 flock 文件锁（防 2 分钟周期堆叠）

v8.9 修复：
  1. P0: cycle_comp_total 跨启停累加失效（死代码 → 改读 state.compressor_on_min）
  2. P2: 加 manual_on 30 分钟保护（对称 manual_off 2 小时）

v8.5 新增：夜间模式 TTS 静音 + kWh 梯形积分 + 压缩机运行时间统计
"""
import os
import sys
import json
import math
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_advisor as A
from ac_advisor import evaluate_and_learn as evaluate
from ac_advisor import log_decision as log_decision

from ac_advisor import evaluate_and_learn as evaluate
from ac_advisor import log_decision as log_decision


# ── 并发锁（v8.10 Windows 兼容版 + atexit 清理） ──
# v8.21：改为惰性获取。原先在 module import 阶段就抢锁，并在抢不到时直接
# sys.exit(0)——于是任何 import ac_watch 的进程（单元测试、离线分析脚本）都会被
# 当作"重复运行的 ac_watch"静默退出，表现为测试无输出却 exit 0 的假通过。
# 现在只有真正要执行控制循环时（main / --selftest）才抢锁。
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ac_watch.lock")
_LOCK_FD = None
_LOCK_STALE_SEC = 120      # 锁文件超过该秒数视为前一轮异常退出，可覆盖
import atexit


def _cleanup_lock():
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            _LOCK_FD.close()
        except Exception:
            pass
        _LOCK_FD = None
    try:
        if os.path.exists(_LOCK_FILE):
            os.remove(_LOCK_FILE)
    except Exception:
        pass


def acquire_lock():
    """抢并发锁。返回 True=拿到（或平台不支持锁，放行），False=上一轮仍在跑。

    调用方负责决定拿不到时怎么办；本函数不再 sys.exit，以免影响 import 者。"""
    global _LOCK_FD
    try:
        import msvcrt
    except ImportError:
        return True          # 非 Windows：不加锁，交给 cron 自身串行
    try:
        _LOCK_FD = open(_LOCK_FILE, "w")
        try:
            msvcrt.locking(_LOCK_FD.fileno(), msvcrt.LK_NBLCK, 1)
            atexit.register(_cleanup_lock)
            return True
        except OSError:
            _LOCK_FD.close()
            _LOCK_FD = None
            age = ((datetime.now() - datetime.fromtimestamp(os.path.getmtime(_LOCK_FILE)))
                   .total_seconds()) if os.path.exists(_LOCK_FILE) else 0
            if age < _LOCK_STALE_SEC:
                return False
            # 锁文件过期（前一轮异常退出没清理）→ 覆盖接管
            _LOCK_FD = open(_LOCK_FILE, "w")
            msvcrt.locking(_LOCK_FD.fileno(), msvcrt.LK_NBLCK, 1)
            atexit.register(_cleanup_lock)
            return True
    except Exception:
        return True          # 锁机制本身故障不该阻断控制逻辑

WATCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_watch.log")
WATCH_MAX_RUN = 90      # 硬上限，防死锁

# ── v8.5 夜间模式：控制继续，只静音 TTS ──
QUIET_TTS = (23, 7)         # TTS 静音时段
NIGHT = (23, 7)             # 夜间节能模式时段
NIGHT_START_T = 27.0        # 夜间启动温度阈值（2026-08-19：28→27，用户在 27.0°C 实测反馈"热了"
                            # 却因 27<28 不触发，且关机后仍不会自动重开。旧值 28 是按女儿屋调的）
NIGHT_START_AH = 15.5       # 夜间启动绝对湿度阈值 (g/m3)（2026-08-16：17→15.5，用户 26°C/AH16.3 体感刺挠，压湿提前；迟滞带 14→16）
NIGHT_STOP_AH = 14.0        # 夜间停止绝对湿度阈值
NIGHT_START_AH_HYST = 0.5   # 夜间启动迟滞（停 AH≤14，升到 AH≥16(NIGHT_START_AH+HYST) 才重开）
NIGHT_TARGET = 26           # 夜间目标温度上限（=clamp(室温-2,24,26)，分支 A 夜间对齐）
NIGHT_MIN_TARGET = 24       # 夜间目标温度下限（防过冷）
DAY_COOL_STOP_T = 22       # 白天制冷过冷保护：≤22°C 且 AH≤15 → 停（防过冷不停）
DAY_COOL_STOP_AH = 15.0    # 白天过冷保护 AH 阈值（独立常量，不与夜间 NIGHT_STOP_AH 耦合）
DAY_STOP_AH = 14.5         # v8.16 白天双轴停止：AH（真实含水量）达标线
DAY_EXIT_RH_MAX = 62       # 双轴停止 RH 门（须 RH≤62 且 AH 达标；防"高温不潮"误停，T≥27 时 AH 天然 >15）
DUAL_STOP_MIN_COMP = 10    # 双轴停止压缩机地板(min)：低于 MIN_RUN 因"目标完成≈安全类"允许早停
NIGHT_MIN_COMP_ON = 20      # 夜间最小压缩机累计运行(min)
NIGHT_MAX_STARTS_PER_H = 4  # 每小时启动次数上限

# ── v8.21 白天短循环根治（2026-08-19，cycle_log 51 周期实测诊断）──
# 病象：08-18 下午 10 个「12min 开 / 16min 停」周期，一天 15 次启停。
# 诊断（数据支撑）：
#   1. 判据错配是根因。这批周期启动时 RH 仅 56-59%，低于 HUM_DEHUMID_ON=65 也低于
#      谷电线 62 —— 不可能由湿度分支触发，只能是温度分支 temp>=TEMP_COOLING。
#      即「温度驱动开机，却用湿度(AH<=14.5)判据收手」，一开机 AH 立刻达标，
#      跑满 10min 地板就停，室温根本没压下去 → 16min 后温度又到 → 循环。
#   2. 白天无迟滞带，且与夜间不对称：夜间停 AH<=14.0 / 启 AH>=16.0（2.0 闭环同量），
#      白天停 AH<=14.5 / 启却看 RH 或温度（跨量对比）。实测白天 AH 平均只回升
#      1.70 g/m3 就重新触发。
#   3. 原注释「配合 MIN_OFF=30 → 白天最多 ~2 次启动/小时」算错了：第 559 行实走
#      DAY_MIN_OFF=15，真实下限是 10min 开 + 15min 停 = 2.4 次/小时，与实录 16min
#      间隔吻合。DUAL_STOP_MIN_COMP=10 事实上架空了 MIN_RUN=40。
# 对策：温度驱动的周期必须把室温压到目标附近才允许用湿度判据收手；
#       白天补 AH 迟滞带与夜间对称；并给白天启停次数加显式上限。
DAY_STOP_AH_HYST = 2.0       # 白天 AH 迟滞带（对齐夜间 2.0）：AH 达标停机后，
                             # 需回升到 DAY_STOP_AH + 该值才允许湿度分支再开
DAY_TEMP_REACHED_SLACK = 1.0 # 温度驱动周期的收手条件：室温须降到 target+1 才允许
                             # 用湿度判据收手。SLACK=1.0 而非 0.0 的理由：
                             #   定频机热负荷=制冷量时，室温永远降不到 target（如 27°C 稳定，
                             #   target=25）。SLACK=0.0 时湿度判据每 2 分钟达标一次 → 停机 →
                             #   16min 后又触发 → 短周期抖振（实测 91.7% 周期<20min）。
                             #   SLACK=1.0 创建 1°C 死区（传感器分辨率 1°C），
                             #   室温≤target+1 才收手，让空调真正把室温压到目标附近。
                             #   这不是"拿舒适换启停"：target 本就是选定的舒适温度，
                             #   降到 target+1 是履约，再往负才是过度追求。

# ── v8.24 稳态运行 + 热负荷自适应（2026-08-20 审计：91.7% 周期<20min）──
STEADY_STATE_MIN_MIN = 15       # 温度稳定在 target+1 内持续多久进入稳态运行(min)
THERMAL_FAIL_WINDOW = 3          # 连续几个周期未达标触发热负荷自适应
THERMAL_FAIL_RISE = 1            # 热负荷自适应：目标温度上调(°C)


DAY_STARTS_OVERRIDE_T = 29.0 # 启停上限的安全阀：室温 >= 该值说明真的热（不是抖振），
                             # 无条件放行开机。抖振要压，但不能因为"压次数"把人热着——
                             # 抖振的特征是 26-27°C 反复触发，29°C 是真实热负荷。

# ── v8.19 天气感知：晴天/室外高温提前制冷（用户反馈：阴天调参后晴天觉得热）──
# v8.22 重做：传感器分辨率实测为 1°C（2384 次采样 0 个小数），故 26.5 这类值是
# 伪精度——26 不触发、27 触发，与常规线 27 完全等价，"炎热日提前开"实际失效。
# 若真降到 26 则死区只剩 1°C（目标 25），会重新引发刚修掉的抖振。
# 改为【不动启动线、压低目标】：室外炎热时房间回温快，一次多降 1°C 换更长的
# 停机间隔，比提前开机更省电且不抖振。死区 27-24=3°C。
OUTDOOR_HOT_T = 30            # 室外≥30°C 视为炎热晴天
HOT_DAY_TEMP_DROP = 3         # 炎热日目标偏移：室温-3（常规 -2）→ 27°C 时目标 24，死区 3°C
HOT_DAY_TARGET_FLOOR = 24     # 炎热日目标地板（防过冷；常规 25）

# ── v8.23 持续判据（2026-08-19 用户："其实刚才也不太热"）──
# 用户在 27.0°C 先说"热了不给我开"、降到 26.0°C 后又说"刚才也不太热"。
# 两句不矛盾：27 = 有点热但能忍，26 = 不热。真正该改的不是启动线（回到 28 就变回
# "基本不自动开"，覆盖率仅 0.7%，那是他最初的抱怨），而是**加时间维度**——
# 碰一下 27 不算热，持续 27 才算。
# 历史 2384 采样：≥27°C 共 54 段，13 段短于 10min（开门/走动/日照的短暂触碰），
# 真热中位持续 18min、最长 182min → 10min 门槛滤噪声且不漏真热。
SUSTAIN_MIN = 10             # 室温须持续 >= 该分钟数不低于启动线才开机
SUSTAIN_URGENT_T = 29.0      # 但室温 >= 该值属明确过热，不等持续时长直接开

# fallback：传感器不可达时的保守动作
SENSOR_FALLBACK_OFF_ALLOWED = True  # 读不到 → 允许关（安全动作）
SENSOR_FALLBACK_ON_ALLOWED = False  # 读不到 → 禁止开（危险动作）

# ── v8.3 压缩机状态识别层 ──
COMPRESSOR_POWER_THRESHOLD = 300   # 高于此值 = 压缩机在转
FAN_ONLY_POWER_MAX = 50            # 5~50W = 仅风扇，压缩机停
COMPRESSOR_FALSE_RUN_MIN = 10      # 压缩机停连续多久判定为"假运行"(min)
COMPRESSOR_RESTART_COOLDOWN = 30   # 压缩机重启后 30min 内不再次调温
COMPRESSOR_RESTART_DROP = 2        # 假运行时降 2C 重启压缩机

# ── v8.16 kWh 严格 gap 语义 ──
# 实锤（2026-08-16）：关机期 read_ac_power 返回 None，_prev_power 残留压缩机 1167W，
# 下次开机跨 30+min 空窗被梯形外推 → 16:30→16:32 两分钟虚记 0.62 度（≈18.7kW，物理不可能），
# cycle_log 的 kwh_used 系统性虚高 2-4 倍，污染 ΔAH/kWh 效率模型。
# 修法：只在相邻有效采样间隔 ≤10min（5 个 tick 容忍度）内积分，跨 gap 只重置锚点不外推。
KWH_MAX_GAP_MIN = 10

# ── v8.17 室外免费干燥门控（联动决策模型 2b 露点差判据）──
VENT_GATE_DP_DIFF = 1.5   # 室外露点 ≤ 室内-1.5°C = 🟢 开窗可顺带除湿（省空调电）
VENT_GATE_MAX_RH = 69     # RH≥70（紧急闷）自动放行，门控自限
VENT_GATE_HOURS = (8, 22) # 夜间关窗睡觉不适用
VENT_WX_TTL_MIN = 30      # 天气缓存（仅门控触发时才拉，防 2min tick 打爆 API）
VENT_TTS_COOLDOWN = 30    # TTS 提示最短间隔(min)；门控决策本身不受此限

# ── v8.18 晚间恒温巡航（2026-08-16 实测定档）──
# 依据：晚间锯齿（0.37kWh/h 实测）与持续26°C（0.32-0.54 估算）电耗打平，但温度 24-27 摆动
# 体感差（用户两晚间喊热均发生在锯齿波谷）→ 20-23 点切"温度优先巡航"，23 点后交还夜间省电逻辑。
EVENING = (20, 23)        # 晚间巡航时段
EVENING_TARGET = 26       # 巡航设定（定频机靠自身温控器占空比调制）
EVENING_START_T = 26.5    # 温度到线即开巡航（不等湿度线）

# ── v8.4 除湿效率参数 ──
DEHUMID_EXIT_RH = 55               # 湿度达标退出线（与 HUM_DEHUMID_ON=65 保持 10% 滞回，防频繁启停）
DEHUMID_LOW_EFF_RH = 66            # 低效检测湿度阈值
DEHUMID_DELTA_RH_MIN = -1.0        # ΔRH/20min <= -1% = 有效除湿
DEHUMID_ADJUST_COOLDOWN = 20       # 调温后冷却锁(min)，防每 10min 连续降
DEHUMID_FORCE_MIN = 40             # 强制除湿所需连续运行(min)
DEHUMID_FORCE_RH = 68              # 强制除湿湿度阈值
DEHUMID_STALL_MIN = 60             # 判定无效的连续运行(min)
DEHUMID_STALL_RH_BAND = 1.5        # 60min 内 RH 波动 < 1.5% = 无效
DEHUMID_STEP_C = 1                 # 每级降 1C（不是 2C）
DEHUMID_MIN_TARGET = 16            # 最低目标温度
DEHUMID_START_TARGET = 25          # 除湿起步目标温度（v8.12：24→25，防过冲触发逃生门短周期）
VALLEY_START_RH = 62             # 谷电时段(22-6 半价)除湿启动阈值（v8.14 E 方案：65→62 更早压湿省钱）

# ── v8.8 虚拟变频（方案5）：接近达标时升温降负载，缓拖到 55%，防过冷 + 平滑曲线 ──
VIRTUAL_INV_APPROACH_RH = 58       # RH 低于此值且跑够 MIN_RUN → 进入缓除模式
VIRTUAL_INV_MAX_TARGET = 26        # 虚拟变频目标温度上限（缓除时最多升到这里）
VIRTUAL_INV_RECOVER_RH = 62        # 升温缓除后湿度回升至此 → 降回24°C重启（填 58-66 无人区）

# ── v8.10 审计修复常量（Qwen3.8-Max 交叉审查 2026-08-16） ──
SENSOR_PLAUSIBLE_T_MIN = 10        # 传感器合理温度下限
SENSOR_PLAUSIBLE_T_MAX = 45        # 传感器合理温度上限
SENSOR_PLAUSIBLE_RH_MIN = 20       # 传感器合理湿度下限
SENSOR_PLAUSIBLE_RH_MAX = 98       # 传感器合理湿度上限
SENSOR_TIMEOUT_ESCALATE = 20       # 传感器断连超过此分钟数 → 升级动作
FAKE_RUN_MAX_CYCLES = 3            # 假运行连续重试上限次数
MANUAL_ANCHOR_TTL = 720            # 手动锚点有效时间(min) = 12h
ACTION_TTS_COOLDOWN = 10           # TTS 播报最短间隔(min)，防每2分钟喊一次



def log(msg):
    try:
        with open(WATCH_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def tts_mute(now=None):
    """23-7 TTS 静音（通知层静默，不影响控制层）。"""
    h = (now or datetime.now()).hour
    return h >= QUIET_TTS[0] or h < QUIET_TTS[1]


def night_hours(now=None):
    """23-7 夜间节能模式时段。"""
    h = (now or datetime.now()).hour
    return h >= NIGHT[0] or h < NIGHT[1]


def dew_point(temp_c, rh):
    if temp_c is None or rh is None:
        return None
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    return (b * gamma) / (a - gamma)


def absolute_humidity(temp_c, rh):
    if temp_c is None or rh is None:
        return None
    e = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
    return (e * rh * 2.1674) / (273.15 + temp_c)


def compressor_state(load_power):
    if load_power is None:
        return "unknown"
    if load_power > COMPRESSOR_POWER_THRESHOLD:
        return "compressor"
    if load_power > FAN_ONLY_POWER_MAX:
        return "unknown"
    if load_power > 5:
        return "fan_only"
    return "off"


def compute_delta_rh(rh_history, now_ts, window_min=20):
    if not rh_history or len(rh_history) < 2:
        return None, len(rh_history) if rh_history else 0
    now = datetime.fromisoformat(now_ts)
    cutoff = now - timedelta(minutes=window_min)
    window_start = None
    for ts_str, rh_val in rh_history:
        ts = datetime.fromisoformat(ts_str)
        if ts >= cutoff:
            window_start = (ts, rh_val)
            break
    if window_start is None:
        return None, len(rh_history)
    latest = rh_history[-1][1]
    delta = latest - window_start[1]
    return round(delta, 1), len(rh_history)


def update_rh_history(state, now_ts, rh):
    hist = state.get("rh_history") or []
    hist.append([now_ts, rh])
    if len(hist) > 12:
        hist = hist[-12:]
    state["rh_history"] = hist


def update_temp_history(state, now_ts, temp):
    """v8.17：室温轨迹（与 rh_history 同构），给回弹/自然降温学习铺数据。"""
    if temp is None:
        return
    hist = state.get("temp_history") or []
    hist.append([now_ts, temp])
    if len(hist) > 12:
        hist = hist[-12:]
    state["temp_history"] = hist


def is_temp_stable(state, target, slack, window_min):
    """v8.24：温度是否已在 target±slack 范围内稳定持续 window_min 分钟。
    用于稳态运行判定——定频机热负荷=制冷量时，室温永远降不到 target，
    但不代表空调没干活（它在维持温度不上升）。"""
    th = state.get("temp_history") or []
    if not th or len(th) < 2:
        return False
    try:
        now_ts = datetime.fromisoformat(th[-1][0])
    except Exception:
        return False
    cutoff = now_ts - timedelta(minutes=window_min)
    in_window = []
    for ts_str, t in th:
        try:
            if datetime.fromisoformat(ts_str) >= cutoff:
                in_window.append(t)
        except Exception:
            pass
    if len(in_window) < 2:
        return False
    return all(abs(t - target) <= slack for t in in_window)


def sustained_above(state, line, need_min, now_ts=None):
    """室温是否已持续 >= need_min 分钟不低于 line。数据不足时返回 None（调用方决定）。

    v8.23：用户反馈「27°C 那会儿其实也不太热」——碰一下 27 不算热，持续 27 才算。
    历史 2384 采样里 ≥27°C 共 54 段，其中 13 段短于 10 分钟（开门/走动/日照造成的
    短暂触碰）；真热的中位持续 18 分钟、最长 182 分钟，所以 10 分钟门槛滤掉噪声
    而不会漏掉真热。
    """
    hist = state.get("temp_history") or []
    if len(hist) < 2:
        return None
    try:
        now = (datetime.fromisoformat(now_ts) if isinstance(now_ts, str)
               else (now_ts or datetime.now()))
    except Exception:
        now = datetime.now()
    cutoff = now - timedelta(minutes=need_min)
    in_window = []
    for ts_str, t in hist:
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if ts >= cutoff and t is not None:
            in_window.append(t)
    # 窗口内至少要有 2 个采样才算"持续"，否则视为数据不足
    if len(in_window) < 2:
        return None
    # 窗口跨度也要够：只有最近 2 分钟的两条采样不能证明持续了 10 分钟
    try:
        span = (now - datetime.fromisoformat(hist[0][0])).total_seconds() / 60
    except Exception:
        span = 0
    if span < need_min:
        return None
    return all(t >= line for t in in_window)


def vent_gate_decision(hour, hum, temp, rain, dp_out, dp_in):
    """v8.17 纯决策：室外免费干燥是否拦下本次开机。
    判据（00_联动决策模型 2b 🟢 区 + 换气闸门）：白天 + 非紧急(RH<70) + 室温≥24 +
    无雨(<45%) + 室外露点比室内低 ≥1.5°C。任何输入 None → False（fail-open）。"""
    if not (VENT_GATE_HOURS[0] <= hour < VENT_GATE_HOURS[1]):
        return False
    if hum is None or temp is None or hum >= VENT_GATE_MAX_RH or temp < A.TEMP_ABSOLUTE_FLOOR:
        return False
    if rain is not None and rain >= 45:
        return False
    if dp_out is None or dp_in is None:
        return False
    return dp_out <= dp_in - VENT_GATE_DP_DIFF


def cached_outdoor(state, now_dt):
    """门控专用天气缓存（30min TTL，存标量不存全量防状态膨胀）。
    返回 {"t":..,"rh":..,"rain":..} 或 None；获取失败 None（fail-open）。"""
    c = state.get("_vent_wx_cache")
    if c:
        try:
            if (now_dt - datetime.fromisoformat(c["ts"])).total_seconds() < VENT_WX_TTL_MIN * 60:
                return c["wx"]
        except Exception:
            pass
    try:
        w = A.fetch_weather()
        if not w or w.get("error") or "current" not in w:
            return None
        t_out = w["current"].get("temperature_2m")
        rh_out = w["current"].get("relative_humidity_2m")
        rain = (w.get("daily", {}).get("precipitation_probability_max") or [None])[0]
        if t_out is None or rh_out is None:
            return None
        wx = {"t": t_out, "rh": rh_out, "rain": rain}
        state["_vent_wx_cache"] = {"ts": now_dt.isoformat(timespec="seconds"), "wx": wx}
        return wx
    except Exception:
        return None


def update_kwh(state, now_ts, load_power):
    """梯形积分更新 estimated_kWh。
    每次 tick 调用一次，用当前 load_power 与上一 tick 的功率做梯形积分。
    v8.16 严格 gap：相邻有效采样间隔 >KWH_MAX_GAP_MIN 视为空窗，不外推
    （空窗另一端可能是关机期待机 77W，用 stale 压缩机功率外推会虚记账），只重置锚点。"""
    prev_power = state.get("_prev_power")
    prev_ts = state.get("_prev_kwh_ts")
    kwh = state.get("estimated_kwh", 0.0)
    if prev_power is not None and prev_ts is not None and load_power is not None:
        try:
            dt_hours = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(prev_ts)).total_seconds() / 3600
            if dt_hours > 0 and dt_hours <= KWH_MAX_GAP_MIN / 60.0:
                avg_power = (prev_power + load_power) / 2.0
                kwh += avg_power * dt_hours / 1000.0
        except Exception:
            pass
    state["estimated_kwh"] = round(kwh, 4)
    if load_power is not None:
        state["_prev_power"] = load_power
        state["_prev_kwh_ts"] = now_ts


def stale_stop_ts(old_ts, run_start_ts):
    """旧 compressor_stop 时间戳是否早于本次 run_start（跨周期遗留视为无效）。"""
    if not old_ts:
        return True
    try:
        return bool(run_start_ts) and datetime.fromisoformat(old_ts) < datetime.fromisoformat(run_start_ts)
    except Exception:
        return False


def open_cycle(state, now_ts, ah, rh, temp=None, outdoor_temp=None):
    """开机动作(apply+verify 通过)时记录周期开始快照。纯数据层，不影响决策。

    v8.21 补 temp/outdoor_temp：原先只记 AH/RH，而策略同时按温度和湿度决策，
    导致温度侧完全无法复盘——诊断 08-18 短循环时只能靠 RH 反推"是温度分支触发的"。
    有了温度才能回答"这轮到底热不热、压下去了没、舒适度达成了吗"。"""
    state["cycle_start"] = {
        "ts": now_ts,
        "ah": ah,
        "rh": rh,
        "temp": temp,
        "outdoor_temp": outdoor_temp,
        "kwh": state.get("estimated_kwh", 0.0) or 0.0,
    }


def close_cycle(state, now_ts, ah, rh, target_temp, comp_min, path=None, abort_reason=None,
                temp=None, outdoor_temp=None):
    """关机动作(apply+verify 通过)时追加一条完整周期记录到 cycle_log.jsonl。
    无 cycle_start（异常/失败路径）→ 不写假周期。append-only，幂等。"""
    cs = state.get("cycle_start")
    if not cs:
        return False
    end_kwh = state.get("estimated_kwh", 0.0) or 0.0
    try:
        dur_min = round((datetime.fromisoformat(now_ts) - datetime.fromisoformat(cs["ts"])).total_seconds() / 60.0, 1)
    except Exception:
        dur_min = None
    # v8.17 周期污染标记：6min 窗口内 RH 逆势跳升 ≥3pp（开门/晾衣/烧水类事件，
    # COP 寻优时应剔除该周期——LongCat 待办#5 的数据基础）
    spike = False
    hist = state.get("rh_history") or []
    for i in range(2, len(hist)):
        if hist[i][1] - hist[i - 2][1] >= 3:
            spike = True
            break
    # v8.21 占空比自校验：压缩机分钟数不该超过周期时长（实录 3/51 条占空>1，
    # 最高 2.00——计量 bug 残留）。写盘时标记而非静默，让分析端能剔除脏行。
    duty = None
    if dur_min and comp_min is not None and dur_min > 0:
        duty = round(comp_min / dur_min, 3)
    rec = {
        "start_ts": cs["ts"],
        "end_ts": now_ts,
        "start_AH": cs.get("ah"),
        "end_AH": ah,
        "start_RH": cs.get("rh"),
        "end_RH": rh,
        "start_temp": cs.get("temp"),
        "end_temp": temp,
        "start_outdoor_temp": cs.get("outdoor_temp"),
        "end_outdoor_temp": outdoor_temp,
        "target_temp": target_temp,
        "compressor_runtime_min": round(comp_min, 1) if comp_min is not None else None,
        "kwh_used": round(max(0.0, end_kwh - cs.get("kwh", 0.0)), 4),
        "duration_min": dur_min,
        "duty": duty,
        "duty_invalid": bool(duty is not None and duty > 1.01),
        "abort_reason": abort_reason,
        "rh_spike": spike,
    }
    path = path or os.path.join(os.path.dirname(os.path.realpath(__file__)), "cycle_log.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state.pop("cycle_start", None)
    return True


def handle_cycle_after_action(state, new_mode, mode_before, now_ts, ah, hum, running_target,
                              comp_min, path=None, abort_reason=None,
                              temp=None, outdoor_temp=None):
    """apply+verify 通过后按动作类型维护周期记录（main 的唯一入口，纯数据层）。
    开机 → open_cycle；关机 → close_cycle；其余动作 → 不写。
    v8.21：temp/outdoor_temp 透传，补齐温度侧复盘数据。"""
    if new_mode == "cooling" and mode_before != "cooling":
        open_cycle(state, now_ts, ah, hum, temp=temp, outdoor_temp=outdoor_temp)
        return True
    if new_mode == "off" and mode_before in ("cooling", "dehumid", "dehumid_alert"):
        return close_cycle(state, now_ts, ah, hum, running_target, comp_min, path=path,
                           abort_reason=abort_reason, temp=temp, outdoor_temp=outdoor_temp)
    return False


def decide(temp, hum, running, since_on, since_off, is_night,
           compressor=None, compressor_stop_duration_min=None, cooldown_until_dt=None,
           current_target=26, delta_rh_20min=None, delta_rh_60min=None,
           minutes_since_last_adjust=None, ah=None, compressor_run_min=None,
           night_comp_starts=None, fake_run_count=None, evening=False,
           outdoor_temp=None, outdoor_rain=None, sustained=None,
           is_steady_state=False):
    """纯决策函数。返回 (new_mode, target_temp, reason) 或 (None, None, None)。

    v8.6 核心改进：所有"已运行多久"判断改用 compressor_run_min（压缩机实际累计运行分钟），
    不用 since_on（壁钟时间）——定频机到温停压缩机，since_on 包含大量风扇空吹时间。
    失败/低效判断只基于真实除湿工作时间，避免"看起来跑了很久实际没干活"的误判。
    v8.7：增加 reason 文本——供 TTS 播报"为什么开关"，让用户听到决策逻辑。
    v8.10：增加 fake_run_count 参数，连续 3 次假运行后停机告警。
    """
    if running is None:
        # 传感器不可达：放弃本次决策，不动作
        return (None, None, None)
    if running:
        if compressor == "fan_only":
            # ── 假运行（v8.3） ──
            stop_duration = None
            if compressor_stop_duration_min is not None and since_on is not None:
                stop_duration = compressor_stop_duration_min
                if stop_duration < 0:
                    stop_duration = None
            in_cooldown = False
            if cooldown_until_dt is not None:
                in_cooldown = datetime.now() < cooldown_until_dt

            # v8.10 假运行计数器：连续 FAKE_RUN_MAX_CYCLES 次后停机告警
            if fake_run_count is not None and fake_run_count >= FAKE_RUN_MAX_CYCLES:
                return ("off", None,
                        f"假运行已连续{int(fake_run_count)}次，空调可能硬件故障，停止自动重试")

            if (hum > DEHUMID_LOW_EFF_RH
                    and stop_duration is not None
                    and stop_duration >= COMPRESSOR_FALSE_RUN_MIN
                    and not in_cooldown):
                new_target = max(DEHUMID_MIN_TARGET, current_target - COMPRESSOR_RESTART_DROP)
                return ("cooling", new_target,
                        f"压缩机只吹风不制冷已{int(stop_duration)}分钟，湿度{hum:.0f}%仍偏高，降2度重启压缩机")
            if hum <= DEHUMID_EXIT_RH:
                return ("off", None, f"湿度已降到{hum:.0f}%，压缩机已停，关机省电")
            # v8.9 降温补偿（Laguna 交叉审查）：虚拟变频升温后湿度回升 → 降回重启
            # 填补 58%~66% 之间的"无人区"（假运行检测只覆盖 >66%）
            if (hum >= VIRTUAL_INV_RECOVER_RH
                    and current_target > A.TEMP_ABSOLUTE_FLOOR + 1
                    and not in_cooldown):
                return ("cooling", 24,
                        f"升温缓除后湿度回升到{hum:.0f}%，降回24度继续除湿")
            # v8.13 假运行盲区兜底（glm-5.2 交叉审查）：压缩机停 + 湿度 56~66%
            # 既不满足重启(>66)也不满足达标(<=55)也不满足降回24(>=62 且 target>25) →
            # 之前一直返回 None 让风扇空吹。压缩机已停=室温已降、湿度接近目标，吹风纯浪费；
            # 加 stop_duration>=10min 守卫，避免到温停机短暂停顿(<10min)被误关。
            # 关机后 MIN_OFF 天然兜底防短循环。
            if (hum <= DEHUMID_LOW_EFF_RH
                    and stop_duration is not None
                    and stop_duration >= COMPRESSOR_FALSE_RUN_MIN):
                return ("off", None,
                        f"压缩机已停、湿度{hum:.0f}%接近目标，不再吹风空耗，关机省电")
            return (None, None, None)

        # ── 压缩机运行中 ──
        # 用压缩机实际累计运行时间（不是壁钟时间）
        comp_min = compressor_run_min if compressor_run_min is not None else since_on

        # 硬上限：压缩机累计运行超时，无论湿度如何都停（保护压缩机）
        # v8.18：晚间巡航豁免——定频机由自身温控器占空比调制，累计时长≠连续运行
        if comp_min is not None and comp_min >= WATCH_MAX_RUN and not evening:
            return ("off", None, f"压缩机已连续运行{int(comp_min)}分钟，为保护压缩机强行关机")

        # 最短运行时间保护：跑不够 A.MIN_RUN 不关非安全类关机（防短循环）
        # 安全类关机（硬上限/过冷/夜间）在前置处理，不受此守卫影响

        # 夜间停止条件（v8.11 修复：从 T≤26 改为湿度达标优先，避免60%就停）
        # 注意：安全类关机（过冷/AH达标）先于守卫，防止低温不停机
        #
        # v8.22 补温度达标前置（2026-08-19 用户实测："咋给我关了？"）：
        # 原先只看 AH —— 用户 27°C 正热，但 AH=13.4 ≤ 14.0 就被判"湿度已达标"关机，
        # 且该分支位于 MIN_RUN 守卫之前（当安全类处理），NIGHT_MIN_COMP_ON=20 也拦不住，
        # 于是开机 20 秒即被关。这与 v8.21 修的白天病同源：**用湿度判据关一台因为热
        # 而开的空调**。屋里干≠屋里不热。
        # 故要求室温已降到 target + DAY_TEMP_REACHED_SLACK 才允许按湿度收手；
        # 拿不到 current_target 时退化旧行为（宁可早停也不要整夜空转）。
        if is_night:
            if ah is not None and ah <= NIGHT_STOP_AH:
                temp_reached = (current_target is None
                                or temp <= current_target + DAY_TEMP_REACHED_SLACK)
                if temp_reached:
                    return ("off", None, f"夜间室内湿度已达标（AH={ah:.1f}），关机省电")
                # 温度未达标 → 不因湿度收手，继续制冷把室温压到目标
            # v8.11: 夜间停止不再以 T≤26°C 为条件（太激进，导致 60%RH 就停），改为湿度驱动
            # 只有湿度真正降到 55% 以下才停，或者温度降到 24°C 逃生门
            # v8.22: 与上面 AH 分支同样补温度前置——同一个病（拿湿度判据关一台因为热而
            # 开的空调）。这条本身有 NIGHT_MIN_COMP_ON=20min 地板兜着，不像 AH 那条会
            # 开机即关，但判据错配一样要修，否则 25.1°C 仍会被 RH=48% 关掉。
            if (hum <= DEHUMID_EXIT_RH and comp_min is not None
                    and comp_min >= NIGHT_MIN_COMP_ON):
                if current_target is None or temp <= current_target + DAY_TEMP_REACHED_SLACK:
                    return ("off", None, f"夜间湿度已降到{hum:.0f}%，压缩机工作完成关机")
            if temp <= A.TEMP_ABSOLUTE_FLOOR:
                return ("off", None, f"夜间室温{temp:.0f}度低于绝对下限{A.TEMP_ABSOLUTE_FLOOR}度，逃生门关机")
            # 舒适类守卫：跑不够 20 分钟不停，启动次数超限不新开
            if comp_min is not None and comp_min < NIGHT_MIN_COMP_ON:
                return (None, None, None)
            if night_comp_starts and len(night_comp_starts) >= NIGHT_MAX_STARTS_PER_H:
                return (None, None, None)

        # 过冷保护（v8.6 白天）：温度已低于舒适下限且不闷 → 停，避免吹到 22°C 以下
        if not is_night and temp <= DAY_COOL_STOP_T:
            if ah is not None and ah <= DAY_COOL_STOP_AH:
                return ("off", None, f"室温已降到{temp:.0f}度不闷，过冷保护关机")

        # 温度绝对下限逃生门（SKILL.md：湿度<60 或 温度<24°C 无条件关，防除湿越吹越冷）
        # 先于 MIN_RUN 守卫：安全类关机不等最短运行时间（补白天过冷 AH>15 的空区）
        if temp < A.TEMP_ABSOLUTE_FLOOR:
            return ("off", None, f"室温{temp:.0f}度低于绝对下限{A.TEMP_ABSOLUTE_FLOOR}度，逃生门无条件关机")

        # v8.16 白天双轴停止（AH+RH）：低热负荷时段 RH 单轴会拖到过冷逃生门才停
        # （2026-08-16 16:30 周期实录：AH 6min 即到 14.0，T 却在 12min 冲到 23°C 触发逃生门）。
        # AH=真实含水量，达标即收手，室温可停在 25°C 而不是吹到 23°C。
        # v8.18：晚间巡航时豁免舒适类停止（双轴/RH达标/无效判定）——巡航目标是恒温，
        # 只保留安全类（过冷逃生门/白天过冷保护/传感器失效），除湿收手逻辑留给日/夜模式
        #
        # v8.21 补温度达标前置（修短循环根因）：AH 达标只说明"不潮"，不说明"不热"。
        # 若本周期是温度驱动开的（室温还在目标之上），AH 一达标就停会让室温原地不动，
        # 十几分钟后温度再次触发 → 抖振。故要求室温已降到 target+SLACK 以内才允许
        # 用湿度判据收手；否则交给下方 MIN_RUN 守卫继续跑，真正把温度压下去。
        # 拿不到 current_target 时退化为旧行为（宁可早停也不要因缺参数一直吹）。
        if (not is_night and not evening and ah is not None and hum <= DAY_EXIT_RH_MAX
                and ah <= DAY_STOP_AH
                and comp_min is not None and comp_min >= DUAL_STOP_MIN_COMP):
            temp_reached = (current_target is None
                            or temp <= current_target + DAY_TEMP_REACHED_SLACK)
            if temp_reached:
                return ("off", None, f"含水量已达标（AH={ah:.1f}，RH={hum:.0f}%），关机防过冷")
            # 温度未达标：不收手，落到 MIN_RUN 守卫继续制冷（避免"除湿达标但还热"的抖振）

        # 最短运行时间保护：跑不够 A.MIN_RUN 不关非安全类关机（防短循环）
        if comp_min is not None and comp_min < A.MIN_RUN:
            return (None, None, None)

        # v8.24 稳态运行：温度已稳定在 target+DAY_TEMP_REACHED_SLACK 内持续
        # >STEADY_STATE_MIN_MIN 分钟，且压缩机在转 → 不再主动停机，让空调
        # 自身温控器占空比调制。定频机热负荷=制冷量时，室温永远降不到 target
        # 但空调在维持温度不上升 = 有效工作。只挡湿度判据关机，不挡温度。
        if (not is_night and not evening and is_steady_state
                and comp_min is not None and comp_min >= A.MIN_RUN):
            return (None, None, None)

        # 湿度达标
        if not evening and hum <= DEHUMID_EXIT_RH:
            return ("off", None, f"湿度已达标降到{hum:.0f}%，压缩机工作完成关机")

        # 虚拟变频（v8.8）：湿度接近达标（≤58）且已跑够 MIN_RUN → 升温降负载缓除
        # 定频机满功率狂除容易过冷，升温 1°C 让压缩机慢除，曲线平滑、防过冷
        if (hum <= VIRTUAL_INV_APPROACH_RH
                and comp_min is not None and comp_min >= A.MIN_RUN
                and current_target is not None and current_target < VIRTUAL_INV_MAX_TARGET
                and minutes_since_last_adjust is not None
                and minutes_since_last_adjust >= DEHUMID_ADJUST_COOLDOWN):
            new_target = min(VIRTUAL_INV_MAX_TARGET, current_target + 1)
            return ("cooling", new_target,
                    f"湿度近达标（{hum:.0f}%），目标升1度到{new_target}度缓除防过冷")

        # 无效（Tier 4）：压缩机实际跑了 60min 以上 RH 降幅不足 → 立即关掉（不空耗）
        # 方向性判断：湿度上涨=环境在加湿，不该停；只有下降不足才算无效
        if (comp_min is not None and comp_min >= DEHUMID_STALL_MIN and not evening
                and delta_rh_60min is not None
                and delta_rh_60min > -DEHUMID_STALL_RH_BAND):
            return ("off", None, f"压缩机跑了{int(comp_min)}分钟湿度降幅不足，判定无效空耗关机")

        # 正常除湿（Tier 1）
        if delta_rh_20min is not None and delta_rh_20min <= DEHUMID_DELTA_RH_MIN:
            return (None, None, None)

        # 低效（Tier 2）
        if hum > DEHUMID_LOW_EFF_RH and delta_rh_20min is not None and delta_rh_20min > DEHUMID_DELTA_RH_MIN:
            if minutes_since_last_adjust is not None and minutes_since_last_adjust < DEHUMID_ADJUST_COOLDOWN:
                return (None, None, None)
            if comp_min is not None and comp_min >= DEHUMID_FORCE_MIN and hum > DEHUMID_FORCE_RH:
                new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
                return ("cooling", new_target, f"除湿太慢湿度{hum:.0f}%还降不下来，再降1度加强除湿")
            new_target = max(DEHUMID_MIN_TARGET, current_target - DEHUMID_STEP_C)
            return ("cooling", new_target, f"除湿偏慢，目标温度降1度到{new_target}度")

        return (None, None, None)

    # ── 未运行 ──
    # 白天/夜间用不同 MIN_OFF：白天温度回升快，15min 即可；夜间保持 30min 防短循环。
    # 注意：白天 DAY_MIN_OFF=15 + DUAL_STOP_MIN_COMP=10 → 理论最快 25min 一轮 =
    # 2.4 次/小时（旧注释误称"配合 MIN_OFF=30 → ~2 次/小时"，算错了基数）。
    # 真正的抖振闸门是 v8.21 的 DAY_MAX_STARTS_PER_H，不要指望 MIN_OFF 兜住。
    effective_min_off = A.MIN_OFF if is_night else A.DAY_MIN_OFF
    if since_off is not None and since_off < effective_min_off:
        return (None, None, None)
    if is_night:
        # 分支 A 夜间对齐：目标永远低于室温 2C（保证定频压缩机启动），clamp 24~26
        night_target = max(NIGHT_MIN_TARGET, min(NIGHT_TARGET, round(temp - 2)))
        if temp >= NIGHT_START_T:
            # v8.23 持续判据：碰一下 27 不算热，要持续 SUSTAIN_MIN 分钟。
            # sustained=None 表示历史数据不足 → 放行（fail-open，不因缺数据不制冷）。
            # 室温 >= SUSTAIN_URGENT_T 属明确过热，不等持续时长。
            if temp < SUSTAIN_URGENT_T and sustained is False:
                return (None, None, None)
            return ("cooling", night_target, f"夜间室温{temp:.0f}度偏热，自动开制冷{night_target}度")
        # v8.13 夜间 AH 启动守卫（glm-5.2 交叉审查）：室温已低于目标（如 24°C/24°C）时
        # 定频机压缩机不会启动 → 只吹风陷入假运行。要求目标严格低于室温 1°C 以上才开。
        if (ah is not None and ah >= NIGHT_START_AH + NIGHT_START_AH_HYST
                and temp - night_target >= 1):
            return ("cooling", night_target, f"夜间感觉闷（湿度高），自动开制冷{night_target}度压一压")
        return (None, None, None)

    # ── v8.21 白天启停次数上限：抖振时直接不开，等热负荷真正累积起来 ──
    # 白天上限 2 次/小时（夜间 4）。定频机每次启动都有浪涌且头几分钟无有效制冷，
    # 08-18 实录 15 次/天（下午 10 次 12min 周期）纯属做无用功。
    # 安全阀：室温 >= DAY_STARTS_OVERRIDE_T 时无条件放行——抖振的特征是 26-27°C
    # 反复触发，29°C 是真实热负荷，压次数不能压到把人热着。
    if (night_comp_starts and len(night_comp_starts) >= DAY_MAX_STARTS_PER_H
            and temp < DAY_STARTS_OVERRIDE_T):
        return (None, None, None)
    # v8.22 天气感知改为「压低目标」而非「提前启动」：见 HOT_DAY_TARGET_FLOOR 注释，
    # 1°C 分辨率下提前一档会把死区压到 1°C，重新引发抖振。
    hot_day = outdoor_temp is not None and outdoor_temp >= OUTDOOR_HOT_T
    if temp >= A.TEMP_COOLING:
        # v8.23 持续判据（同夜间）：短暂触碰启动线不算热。高湿闷热走下方除湿分支，
        # 那条不受持续时长约束——闷是即时体感，不需要等。
        if (temp < SUSTAIN_URGENT_T and sustained is False
                and hum < A.HUM_DEHUMID_ON):
            return (None, None, None)
        # v8.11: 高湿时优先除湿，降目标温度到24°C，避免26°C早停→湿度反弹→逃生门关机的死循环
        if hum >= A.HUM_DEHUMID_ON:
            return ("cooling", DEHUMID_START_TARGET, f"室内{temp:.0f}度湿度{hum:.0f}%闷热，制冷{DEHUMID_START_TARGET}度先除湿")
        # v8.22 目标下限 26→25：原下限是按启动线 28 设计的（28-2=26，死区 2°C）。
        # 启动线降到 27 后 27-2=25 会被 max(26,..) 抬回 26，死区只剩 1°C，回温 20min
        # 就再次触发 → v8.21 的"周期变长"效果被削掉大半（闭环实测均周期 19.2→12.0min）。
        # 下限改 25 恢复 2°C 死区（实测启停 10→7、均周期 12.0→17.6min）。
        # 25 也与手动开机路径一致（那里本就是 max(24, min(26, temp-2))），
        # 且定频机目标须显著低于室温才能持续制冷，26 对 27°C 室温太近容易到温停机。
        # 炎热日多压 1°C：注意不能靠"改下限"实现——max(24, temp-2) 在 27°C 时仍得 25，
        # 下限只是地板抬不下去。要真多降必须动 temp-N 这个偏移量。
        drop = HOT_DAY_TEMP_DROP if hot_day else 2
        t = round(max(HOT_DAY_TARGET_FLOOR if hot_day else 25, min(28, temp - drop)))
        wx_note = f"（室外{outdoor_temp:.0f}度炎热，多压1度少启停）" if hot_day else ""
        return ("cooling", t, f"室内{temp:.0f}度偏热，自动开制冷{t}度{wx_note}")
    # v8.14 E 方案（谷电积极版）：22-6 谷电半价 → 除湿启动阈值 65→62 更早压湿；
    # 峰电维持原阈值不推迟（保舒适，不牺牲体验）
    #
    # v8.21 补 AH 迟滞带（与夜间对称）：白天原先停看 AH<=14.5、启看 RH，跨量对比等于
    # 没有迟滞——实测 AH 平均只回升 1.70 就重新触发。夜间是停 AH<=14.0 / 启 AH>=16.0
    # 的 2.0 闭环同量迟滞。这里要求 AH 真正回升过迟滞线才允许湿度分支重开。
    # 只挡湿度分支：温度分支（上方 temp>=eff_cool）属"热了就该开"，不能被湿度迟滞拦住。
    if temp >= A.TEMP_DEHUMID_LOW and hum >= (VALLEY_START_RH if A.current_price() < A.ELECTRIC_PEAK else A.HUM_DEHUMID_ON):
        if ah is not None and ah < DAY_STOP_AH + DAY_STOP_AH_HYST:
            return (None, None, None)
        return ("cooling", DEHUMID_START_TARGET, f"室内{temp:.0f}度湿度{hum:.0f}%闷热，制冷{DEHUMID_START_TARGET}度强力除湿")
    # v8.18 晚间恒温巡航：温度优先（闷热除湿分支在上方先判，RH≥65/62 仍走深除）
    if evening and temp >= EVENING_START_T:
        return ("cooling", EVENING_TARGET, f"晚间恒温巡航{EVENING_TARGET}度（温度优先，电耗与锯齿打平）")
    return (None, None, None)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    now_ts = datetime.now().isoformat(timespec="seconds")
    now_dt = datetime.now()
    dry = "--dry" in sys.argv

    A.read_ac_power()
    A.ac_control_init()
    socket = A.AC_SOCKET
    load_power = A.AC_MEASURED_W
    temp, hum = A.read_indoor()

    # v8.10 传感器越界值校验
    if temp is not None and hum is not None:
        if not (SENSOR_PLAUSIBLE_T_MIN <= temp <= SENSOR_PLAUSIBLE_T_MAX
                and SENSOR_PLAUSIBLE_RH_MIN <= hum <= SENSOR_PLAUSIBLE_RH_MAX):
            log(f"传感器读数越界：T={temp} RH={hum}%，视为不可达")
            temp = hum = None

    state = A.load_state()
    # v8.24 P0: 写入当前温湿度供 evaluate_and_learn 回评（None时保留旧值）
    state["last_temp"] = temp if temp is not None else state.get("last_temp")
    state["last_hum"] = hum if hum is not None else state.get("last_hum")
    # v8.15 fail-safe：状态文件损坏 → 本次 tick 不执行任何开/关
    if state.get("_state_load_failed"):
        log("[ERROR] 状态文件损坏，本次 tick fail-safe 跳过（不执行开/关）")
        print("ac_watch: 状态文件损坏，fail-safe 跳过（不执行开/关）")
        return
    A.reconcile_state(state, now_ts)

    # v8.20 传感器离线回退：传感器坏时用天气预报的室外温湿度兜底继续决策
    # 必须在 load_state() 之后——cached_outdoor 需要 state 做 30min TTL 缓存。
    # （室内外湿度差异大，但比完全不决策强；仅用于避免"传感器一挂就彻底失控"）
    wx_fallback_used = False
    if temp is None or hum is None:
        try:
            _wx_fallback = cached_outdoor(state, now_dt)
        except Exception as e:
            _wx_fallback = None
            log(f"[WARN] 天气兜底获取失败：{type(e).__name__}: {e}")
        if _wx_fallback and _wx_fallback.get("t") is not None:
            temp = _wx_fallback["t"]
            # 室外湿度不能代表室内，回退时 hum=None 让决策只走温度分支
            hum = None
            wx_fallback_used = True
            # v8.24 F4：标记回退数据来源
            state["_temp_src"] = "outdoor_fallback"
            state["_hum_src"] = "outdoor_fallback"
            log(f"传感器离线，回退到天气预报 T={temp}°C RH={hum}%")

    # v8.20 传感器断连超时升级（仅当连天气兜底都拿不到时才跳过）
    if temp is None or hum is None:
        sensor_off_since = state.get("_sensor_off_since")
        if sensor_off_since is None:
            state["_sensor_off_since"] = now_ts
        else:
            sensor_off_min = A.minutes_since(sensor_off_since)
            if sensor_off_min is not None and sensor_off_min >= SENSOR_TIMEOUT_ESCALATE:
                if state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
                    log(f"传感器断连{sensor_off_min:.0f}分钟，空调运行中，保守关机")
                    print(f"ac_watch: 传感器断连{sensor_off_min:.0f}分钟，空调运行中，执行保守关机")
                    A.apply_and_commit("off", None, state, now_ts)
                    state.pop("_sensor_off_since", None)
                    A.save_state(state)
                    return
        log(f"传感器不可达且无天气兜底，跳过 socket={socket}, sensor_off_since={state.get('_sensor_off_since', 'now')}")
        print("ac_watch: 室内传感器不可达且无天气兜底，本次跳过")
        A.save_state(state)
        return

    if wx_fallback_used:
        # 仍算断连：保留 _sensor_off_since 以便真传感器长期不回来时触发保守关机
        log("传感器不可达但有天气预报兜底，继续控制（保留断连计时）")
        if state.get("_sensor_off_since") is None:
            state["_sensor_off_since"] = now_ts
    else:
        # 传感器恢复正常 → 清除断连标记
        state.pop("_sensor_off_since", None)

    # ── 手动关后 2 小时内不自动启动，但不超过 12h TTL ──
    manual_off = state.get("manual_off_at")
    if manual_off and state.get("mode") in (None, "off"):
        try:
            off_dt = datetime.fromisoformat(manual_off) if isinstance(manual_off, str) else manual_off
            mins = (now_dt - off_dt).total_seconds() / 60
            # v8.10 TTL：不超过 MANUAL_ANCHOR_TTL min（12h），过期后恢复自动逻辑
            if 0 <= mins < 30 and mins < MANUAL_ANCHOR_TTL:
                # v8.21 温度回升覆盖：冷却期内温度回升 ≥1°C → 解除冷却，按实时温湿度决策
                temp_at_off = None
                off_dt_str = state.get("manual_off_at")
                if off_dt_str:
                    try:
                        off_dt = datetime.fromisoformat(off_dt_str) if isinstance(off_dt_str, str) else off_dt_str
                        th = state.get("temp_history", [])
                        closest = None
                        for ts_str, t in th:
                            try:
                                ts = datetime.fromisoformat(ts_str)
                                if closest is None or abs((ts - off_dt).total_seconds()) < abs((closest[0] - off_dt).total_seconds()):
                                    closest = (ts, t)
                            except Exception:
                                pass
                        if closest:
                            temp_at_off = closest[1]
                    except Exception:
                        pass
                temp_rise = 0
                if temp_at_off is not None and temp is not None:
                    temp_rise = temp - temp_at_off
                if temp_rise >= 1.0:
                    log(f"手动关后{int(mins)}分钟，温度回升{temp_rise:.1f}°C，解除冷却期恢复自动")
                    # 不 return，继续走后续决策逻辑
                else:
                    log(f"手动关后{int(mins)}分钟，跳过自动启动（尊重用户意图）")
                    print(f"ac_watch: 手动关后{int(mins)}分钟，跳过自动启动")
                    A.save_state(state)
                    return
            if mins >= MANUAL_ANCHOR_TTL:
                log(f"手动关锚点已过期（{int(mins)}分钟 > {MANUAL_ANCHOR_TTL}），清除后恢复自动逻辑")
                state.pop("manual_off_at", None)
        except Exception:
            pass

    # ── 手动开保护：手动开后 30 分钟内不自动关，但不超过 12h TTL ──
    manual_on = state.get("manual_on_at")
    if manual_on and state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
        try:
            on_dt = datetime.fromisoformat(manual_on) if isinstance(manual_on, str) else manual_on
            mins = (now_dt - on_dt).total_seconds() / 60
            # v8.10 TTL：不超过 MANUAL_ANCHOR_TTL min（12h）
            if 0 <= mins < 30 and mins < MANUAL_ANCHOR_TTL:
                log(f"手动开后{int(mins)}分钟，暂不自动关（保护用户意图）")
                print(f"ac_watch: 手动开后{int(mins)}分钟，暂不自动关")
                A.save_state(state)
                return
            if mins >= MANUAL_ANCHOR_TTL:
                log(f"手动开锚点已过期（{int(mins)}分钟 > {MANUAL_ANCHOR_TTL}），清除后恢复自动逻辑")
                state.pop("manual_on_at", None)
        except Exception:
            pass

    # socket=None 时回退到 state.mode（室内传感器正常则继续控制）
    # 仅在室内传感器也失败时才放弃（running=None → decide 返回 None）
    if socket is None:
        running = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    else:
        running = socket == "on" or state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    since_on = A.minutes_since(state.get("run_start"))
    since_off = A.minutes_since(state.get("last_off_at"))

    # ── 压缩机状态识别（v8.3） ──
    comp = compressor_state(load_power)
    last_comp_stop = state.get("last_compressor_stop_at")
    cooldown_until_str = state.get("compressor_restart_cooldown_until")
    cooldown = None
    if cooldown_until_str:
        try:
            cooldown = datetime.fromisoformat(cooldown_until_str)
        except Exception:
            pass
    state_comp_before = state.get("compressor_state")

    # v8.10 假运行计数器
    fake_run_count = state.get("_fake_run_count", 0) or 0

    if comp == "compressor" and state_comp_before != "compressor":
        state["compressor_restart_cooldown_until"] = (
            now_dt + timedelta(minutes=COMPRESSOR_RESTART_COOLDOWN)
        ).isoformat(timespec="seconds")
        state["last_compressor_start_at"] = now_ts
        # 压缩机启动 → 清空假运行计数器
        state["_fake_run_count"] = 0
        # 压缩机启动次数跟踪（1h 滑窗）。v8.21：原先只在夜间记，导致白天无从判断
        # 抖振；改为全天记录，夜间用 NIGHT_MAX_STARTS_PER_H、白天用 DAY_MAX_STARTS_PER_H。
        starts = state.get("_night_comp_starts", [])
        starts.append(now_ts)
        cutoff = (now_dt - timedelta(hours=1)).isoformat(timespec="seconds")
        state["_night_comp_starts"] = [s for s in starts if s >= cutoff]
    elif comp == "fan_only" and state_comp_before == "compressor":
        state["last_compressor_stop_at"] = now_ts
        last_comp_stop = now_ts
        # 压缩机停止 → 增加假运行计数（如果预期是运行但停了）
        fake_run_count += 1
        state["_fake_run_count"] = fake_run_count
    elif comp == "fan_only" and state_comp_before != "compressor" and stale_stop_ts(
            state.get("last_compressor_stop_at"), state.get("run_start")):
        state["last_compressor_stop_at"] = now_ts
        last_comp_stop = now_ts
    state["compressor_state"] = comp

    # ── 压缩机运行时间（跨启停累加，仅 AC 关机时清零）──
    comp_on_min = 0
    cycle_comp_total = state.get("cycle_comp_total", 0) or 0
    if comp == "compressor":
        comp_since = state.get("compressor_on_since")
        if comp_since:
            try:
                elapsed = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(comp_since)).total_seconds() / 60
                comp_on_min = round(max(0, elapsed), 1)
            except:
                comp_on_min += 10  # 异常时保守估计 10 分钟
        else:
            state["compressor_on_since"] = now_ts
            comp_on_min = 0
        state["compressor_on_min"] = comp_on_min  # 保存当前连续运行时长供下一 tick 读取
    else:
        # 压缩机停时，把上一 tick 的连续运行时间加入周期累计
        prev_comp_on_min = state.get("compressor_on_min", 0) or 0
        if prev_comp_on_min > 0:
            cycle_comp_total = round(cycle_comp_total + prev_comp_on_min, 1)
        comp_on_min = 0
        state["compressor_on_min"] = 0
        state.pop("compressor_on_since", None)
    state["cycle_comp_total"] = cycle_comp_total

    # ── v8.5 kWh 梯形积分 ──
    update_kwh(state, now_ts, load_power)

    # ── v8.4 RH 历史 ──
    update_rh_history(state, now_ts, hum)
    update_temp_history(state, now_ts, temp)
    delta_rh_20, _ = compute_delta_rh(state.get("rh_history"), now_ts, 20)
    delta_rh_60, _ = compute_delta_rh(state.get("rh_history"), now_ts, 60)

    current_target = state.get("target_temp", 26) or 26
    dp = dew_point(temp, hum)
    ah = absolute_humidity(temp, hum)

    # ── v8.5 夜间模式判断 ──
    is_night = night_hours()
    evening = EVENING[0] <= now_dt.hour < EVENING[1]

    # 调温冷却锁
    last_adjust = state.get("last_dehumid_adjust_at")
    minutes_since_adjust = A.minutes_since(last_adjust) if last_adjust else None

    # 假运行停运时长：将 last_comp_stop 时间戳转为距 run_start 的分钟数
    stop_duration = None
    if last_comp_stop is not None and since_on is not None:
        try:
            stop_dt = datetime.fromisoformat(last_comp_stop) if isinstance(last_comp_stop, str) else last_comp_stop
            run_start = state.get("run_start")
            if run_start:
                run_dt = datetime.fromisoformat(run_start) if isinstance(run_start, str) else datetime.fromisoformat(run_start)
                stop_duration = since_on - (stop_dt - run_dt).total_seconds() / 60
                if stop_duration < 0:
                    stop_duration = None
        except Exception:
            pass

    # v8.19 天气感知：取室外温/雨（30min 缓存，失败 None 不阻塞决策）
    try:
        _wx = cached_outdoor(state, now_dt)
    except Exception:
        _wx = None
    _outdoor_t = _wx["t"] if (_wx and "t" in _wx) else None
    _outdoor_rain = _wx.get("rain") if _wx else None

    new_mode, target, reason = decide(temp, hum, running, since_on, since_off, is_night,
                              compressor=comp,
                              compressor_stop_duration_min=stop_duration,
                              cooldown_until_dt=cooldown,
                              current_target=current_target,
                              delta_rh_20min=delta_rh_20,
                              delta_rh_60min=delta_rh_60,
                              minutes_since_last_adjust=minutes_since_adjust,
                              ah=ah,
                              compressor_run_min=(state.get("cycle_comp_total") or 0) + (state.get("compressor_on_min") or 0),
                              night_comp_starts=state.get("_night_comp_starts"),
                              fake_run_count=fake_run_count,
                              evening=evening,
                              outdoor_temp=_outdoor_t,
                              outdoor_rain=_outdoor_rain,
                              sustained=sustained_above(
                                  state,
                                  NIGHT_START_T if is_night else A.TEMP_COOLING,
                                  SUSTAIN_MIN, now_ts))

    COMP_LABEL = {"compressor": "压缩机运行", "fan_only": "仅风扇",
                  "off": "已关机", "unknown": "未知"}
    cl = COMP_LABEL.get(comp, comp)
    kwh_str = f"kWh={state.get('estimated_kwh', 0):.3f}"
    delta_str = f"dRH20={delta_rh_20}% dRH60={delta_rh_60}%"
    dp_str = f"dp={dp:.1f}C" if dp is not None else "dp=?"
    ah_str = f"AH={ah:.1f}" if ah is not None else "AH=?"
    night_str = "夜间" if is_night else "白天"
    meta = f"{night_str} T={temp} RH={hum}% {dp_str} {ah_str} {delta_str} {kwh_str} target={current_target}C"

    if new_mode is None:
        log(f"无动作 {cl} {meta} 已开={since_on} 已关={since_off} mode={state.get('mode')}")
        print(f"ac_watch: 无需动作 · {cl} · {meta}")
        A.save_state(state)
        # v8.24 自学习回评
        evaluate(state, now_ts)
        return

    # v8.24 记录决策供回评
    log_decision(state, new_mode, temp, hum, now_ts)

    if dry:
        print(f"ac_watch [dry]: 将执行 {new_mode} target={target} · {meta}")
        log(f"[dry] 将执行 {new_mode} target={target} · {meta}")
        evaluate(state, now_ts)
        return

    # v8.24 自学习回评
    evaluate(state, now_ts)

    # ── v8.17 室外免费干燥门控：干爽天让开窗干活，不花电除湿 ──
    # 只拦"从关到开"的启动决策；RH 爬过 70 或天气失效自动放行（fail-open，自限）。
    mode_before = state.get("mode")
    if new_mode == "cooling" and mode_before not in ("cooling", "dehumid", "dehumid_alert"):
        wx = cached_outdoor(state, now_dt)
        dp_out = dew_point(wx["t"], wx["rh"]) if wx else None
        if vent_gate_decision(now_dt.hour, hum, temp, wx and wx.get("rain"), dp_out, dp):
            log(f"vent_gate 拦截开机（室外干爽可免费除湿）· {meta}")
            state["_vent_skip_at"] = now_ts
            A.save_state(state)
            last_tts = state.get("_vent_tts_at")
            tts_ok = last_tts is None
            if not tts_ok:
                try:
                    tts_ok = (now_dt - datetime.fromisoformat(last_tts)).total_seconds() >= VENT_TTS_COOLDOWN * 60
                except Exception:
                    tts_ok = True
            if tts_ok and not is_night:
                state["_vent_tts_at"] = now_ts
                A.save_state(state)
                try:
                    import xiaomi_tts
                    xiaomi_tts.speak("室外空气干爽，开窗通风就能除湿，空调先不开，省电。")
                except Exception:
                    pass
            print("ac_watch: 室外干爽，建议开窗免费除湿，本次不开机")
            return

    # meta 传递
    extra_meta = None
    # v8.13 升温也写冷却锁（nemotron 交叉审查）：虚拟变频升温后若不同步 last_dehumid_adjust_at，
    # Tier2 低效判断立即把目标降回去，与升温逻辑来回震荡
    if new_mode == "cooling" and target is not None and target != current_target:
        extra_meta = {"last_dehumid_adjust_at": now_ts}

    mode_before = state.get("mode")
    running_target = current_target
    comp_min_at_apply = (state.get("cycle_comp_total") or 0) + (state.get("compressor_on_min") or 0)
    ctrl = A.apply_and_commit(new_mode, target, state, now_ts, meta=extra_meta)
    log(f"执行 {new_mode} target={target} → {ctrl['status']} {ctrl.get('action','')} {ctrl.get('reason','')} · {cl} {meta}")
    if ctrl["status"] == "action":
        if new_mode == "off":
            state["cycle_comp_total"] = 0
            state["compressor_on_min"] = 0
            state.pop("compressor_on_since", None)
        handle_cycle_after_action(state, new_mode, mode_before, now_ts, ah, hum, running_target,
                                  comp_min_at_apply, abort_reason=reason,
                                  temp=temp, outdoor_temp=_outdoor_t)
        state.pop("manual_off_at", None)
        A.save_state(state)
        # 换气提醒
        if new_mode == "off" and mode_before == "cooling":
            run_start = state.get("run_start")
            if run_start:
                try:
                    run_dt = datetime.fromisoformat(run_start) if isinstance(run_start, str) else run_start
                    run_hours = (now_dt - run_dt).total_seconds() / 3600
                    if run_hours >= 2 and temp >= 24 and hum <= 70:
                        log(f"建议开窗换气：已连续制冷{run_hours:.1f}小时，室外温度可能更低")
                except Exception:
                    pass
        print(f"ac_watch: 已自动{ctrl['action']} · {meta}")
        _last_action_tts = state.get("_last_action_tts_at")
        _can_tts = _last_action_tts is None or (now_dt - datetime.fromisoformat(_last_action_tts)).total_seconds() >= ACTION_TTS_COOLDOWN * 60
        if not is_night and _can_tts:
            try:
                import xiaomi_tts
                tts_msg = f"空调已自动{ctrl['action']}，{reason}。" if reason else f"空调已自动{ctrl['action']}。"
                xiaomi_tts.speak(tts_msg)
                state["_last_action_tts_at"] = now_ts
            except Exception as e:
                log(f"TTS 播报失败（不影响控制）: {e}")
    elif ctrl["status"] == "no_action":
        print(f"ac_watch: 已处目标状态无需动作 · {meta}")
    else:
        print(f"ac_watch: 自动控制失败（{ctrl['reason']}）——建议人工确认空调状态")


def _selftest():
    # ── compressor_state ──
    comp_cases = [
        (None, "unknown"), (0, "off"), (3, "off"),
        (10, "fan_only"), (25, "fan_only"), (50, "fan_only"),
        (100, "unknown"), (250, "unknown"),
        (301, "compressor"), (500, "compressor"), (1100, "compressor"),
    ]
    for pw, exp in comp_cases:
        assert compressor_state(pw) == exp, f"compressor_state({pw}) = {compressor_state(pw)}"

    # ── 露点/AH ──
    dp = dew_point(23, 70)
    ah = absolute_humidity(23, 70)
    assert dp is not None and 16 < dp < 18, f"dew_point={dp}"
    assert ah is not None and 12 < ah < 16, f"AH={ah}"

    # ── ΔRH ──
    from datetime import timedelta
    base = datetime.now()
    hist = [
        [(base - timedelta(minutes=25)).isoformat(), 72.0],
        [(base - timedelta(minutes=20)).isoformat(), 71.0],
        [(base - timedelta(minutes=10)).isoformat(), 70.0],
        [(base - timedelta(minutes=5)).isoformat(), 69.0],
    ]
    d20, n20 = compute_delta_rh(hist, base.isoformat(), 20)
    assert n20 == 4, f"rh_count={n20}"
    assert d20 is not None and abs(d20 - (-2.0)) < 0.1, f"delta_rh_20={d20}"

    d, n = compute_delta_rh([(base.isoformat(), 70)], base.isoformat(), 20)
    assert d is None and n == 1

    # ── kWh 积分 ──
    st = {}
    update_kwh(st, "2026-08-14T22:00:00", 1100)
    assert st.get("estimated_kwh") == 0.0, f"first tick kwh={st['estimated_kwh']}"
    assert st["_prev_power"] == 1100
    update_kwh(st, "2026-08-14T22:10:00", 1100)
    assert abs(st["estimated_kwh"] - 0.1833) < 0.01, f"kwh_10min={st['estimated_kwh']}"
    update_kwh(st, "2026-08-14T22:20:00", 25)
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01, f"kwh_20min={st['estimated_kwh']}"
    update_kwh(st, "2026-08-14T22:30:00", None)
    assert st["_prev_power"] == 25, f"unknown_prev={st['_prev_power']}"
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01, f"unknown_kwh={st['estimated_kwh']}"
    # v8.16 严格 gap：恢复采样距上一有效采样 20min > 10min 上限 → 不外推（原会虚记 0.0125）
    update_kwh(st, "2026-08-14T22:40:00", 50)
    assert abs(st["estimated_kwh"] - 0.2771) < 0.01, f"recover_gap_kwh={st['estimated_kwh']}"
    assert st["_prev_power"] == 50
    update_kwh(st, "2026-08-14T22:50:00", 45)
    assert abs(st["estimated_kwh"] - 0.2850) < 0.01, f"after_recover_kwh={st['estimated_kwh']}"
    # v8.16 实锤场景：关机期 None 空窗后开机读 1100W，stale 高功率不得跨空窗外推
    update_kwh(st, "2026-08-14T23:30:00", None)   # 关机期待机（读不到）
    update_kwh(st, "2026-08-14T23:35:00", 1100)   # 开机首读，距 22:50 空窗 45min
    assert abs(st["estimated_kwh"] - 0.2850) < 0.01, f"stale_gap_kwh={st['estimated_kwh']}"
    update_kwh(st, "2026-08-14T23:37:00", 1100)   # 正常相邻采样恢复积分
    assert abs(st["estimated_kwh"] - (0.2850 + 1100 * 2 / 60 / 1000)) < 0.01, f"resume_kwh={st['estimated_kwh']}"

    # ── 夜间模式 decide ──
    _future = datetime.now() + timedelta(hours=1)
    assert decide(22, 65, True, 30, 90, False, "compressor", None, None, 26, -1.0, None, None, 13.5, 30)[:2] == ("off", None)
    assert decide(25, 65, True, 30, 90, False, "compressor", None, None, 26, -1.0, None, None, 13.5, 30)[:2] != ("off", None)
    assert decide(29, 60, False, None, None, True, "off", None, None, 26, None, None, False, None, None)[:2] == ("cooling", 26)
    assert decide(26, 75, False, None, None, True, "off", None, None, 26, None, None, False, 18.0, None)[:2] == ("cooling", 24)
    assert decide(26, 65, False, None, None, True, "off", None, None, 26, None, None, False, 15.0, None)[:2] == (None, None)
    assert decide(24, 60, True, 30, 90, True, "compressor", None, None, 27, 0, 0, None, 13.5, None)[:2] == ("off", None)
    assert decide(27, 65, True, 50, 90, False, "compressor", None, None, 26, -2.0, None, None, None, None)[:2] == (None, None)

    # v8.10 假运行计数器测试
    for i in range(FAKE_RUN_MAX_CYCLES):
        r = decide(27, 75, True, 30, 90, False, "fan_only", 15, None, 26, 0, 0, None, None, None, None, fake_run_count=i)
        assert r[0] == "cooling", f"fake_run #{i} should restart, got {r}"
    r = decide(27, 75, True, 30, 90, False, "fan_only", 15, None, 26, 0, 0, None, None, None, None, fake_run_count=FAKE_RUN_MAX_CYCLES)
    assert r[0] == "off", f"fake_run #{FAKE_RUN_MAX_CYCLES} should stop, got {r}"

    # v8.16 白天双轴停止（AH+RH）：位置参数 ah=14th, compressor_run_min=15th
    # ① AH 达标 + RH 过门 + 压缩机跑够 10min 地板 → 早于 MIN_RUN(40) 停（16:30 周期实录场景）
    assert decide(25, 61, True, 12, 90, False, "compressor", None, None, 25, -2.0, None, None, 14.0, 10)[:2] == ("off", None)
    # ② AH 未达标（16.0）→ 继续跑
    assert decide(25, 61, True, 12, 90, False, "compressor", None, None, 25, -2.0, None, None, 16.0, 10)[:2] == (None, None)
    # ③ RH 63 > 62 门 → 不停（防"高温不潮"误停）
    assert decide(27, 63, True, 50, 90, False, "compressor", None, None, 25, -1.0, None, None, 14.0, 45)[:2] == (None, None)
    # ④ 压缩机仅 5min < 10min 地板 → 不停（压缩机保护）
    assert decide(25, 61, True, 6, 90, False, "compressor", None, None, 25, -2.0, None, None, 14.0, 5)[:2] == (None, None)
    # ⑤ 夜间不吃白天双轴规则（夜间有自己的 AH 线 14.0 与 20min 地板）
    assert decide(25, 61, True, 30, 90, True, "compressor", None, None, 25, 0, 0, None, 14.2, 25)[:2] == (None, None)

    # v8.17 门控纯决策 + 周期污染标记
    assert vent_gate_decision(15, 66, 26, 10, 15.0, 19.0) is True    # 🟢 干爽白天 → 拦
    assert vent_gate_decision(15, 66, 26, 10, 18.5, 19.0) is False   # 露点差不足 → 放行
    assert vent_gate_decision(15, 71, 26, 10, 15.0, 19.0) is False   # 紧急(RH≥70) → 放行
    assert vent_gate_decision(23, 66, 26, 10, 15.0, 19.0) is False   # 夜间 → 放行
    assert vent_gate_decision(15, 66, 26, 60, 15.0, 19.0) is False   # 有雨 → 放行
    assert vent_gate_decision(15, 66, 26, 10, None, 19.0) is False   # 数据缺失 fail-open
    st_spike = {"estimated_kwh": 1.0, "cycle_start": {"ts": "2026-08-16T10:00:00", "ah": 16.0, "rh": 70, "kwh": 0.5},
                "rh_history": [["2026-08-16T10:00:00", 70], ["2026-08-16T10:02:00", 70],
                                ["2026-08-16T10:04:00", 74], ["2026-08-16T10:06:00", 74]]}
    assert close_cycle(st_spike, "2026-08-16T10:06:00", 14.0, 74, 25, 6.0,
                       path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_cycle.tmp.jsonl"))
    import json as _json
    _rec = _json.loads(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_cycle.tmp.jsonl"),
                            encoding="utf-8").read().splitlines()[-1])
    assert _rec["rh_spike"] is True
    os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_cycle.tmp.jsonl"))

    # v8.18 晚间恒温巡航
    assert decide(26.5, 60, False, None, 90, False, "off", None, None, 26, None, None, None, None, None, evening=True)[:2] == ("cooling", 26)
    assert decide(26.5, 66, False, None, 90, False, "off", None, None, 26, None, None, None, None, None, evening=True)[:2] == ("cooling", 25)  # 闷热除湿优先
    assert decide(25, 61, True, 12, 90, False, "compressor", None, None, 26, -2.0, None, None, 14.0, 10, evening=True)[:2] == (None, None)  # 巡航不吃双轴停止
    assert decide(26, 54, True, 50, 90, False, "compressor", None, None, 26, -2.0, None, None, 12.0, 50, evening=True)[:2] == (None, None)  # 巡航不吃RH达标停止
    assert decide(23.5, 70, True, 50, 90, False, "compressor", None, None, 26, 0, 0, None, 15.0, 50, evening=True)[0] == "off"  # 安全逃生门保留

    print("ac_watch selftest: ALL PASS (v8.18)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # selftest 是纯函数验证，不碰硬件也不写状态 → 不抢锁，
        # 否则紧接 cron 一轮运行时会"跳过"并 exit 0，造成假通过。
        _selftest()
    else:
        if not acquire_lock():
            print("ac_watch: 上一轮还在运行，跳过")
            sys.exit(0)
        main()