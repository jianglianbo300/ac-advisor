#!/usr/bin/env python3
"""
定频空调省电顾问 v4 — 上海闵行
基于 cline(deepseek0731) 审查修正版。
核心修正:
A. 状态持久化: 引入 state.json 记录 mode/last_on/last_off，让 MIN_RUN/MIN_OFF/MAX_RUN 真生效
B. 除湿关标准加温度下限逃生门: 湿度<60 或 温度<24 → 关
C. 回退路径禁用室外湿度判除湿
D. 单一决策转移表 + 滞回区间
E. 除湿下限提到 26°C（对齐"不算热"）+ 湿度阈值 65%
F. 阈值统一头部常量
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# 确保能找到 miio（cron 可能用 python3.11，miio 装在 3.12）
_MIIO_PATHS = [
    "C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages",
]
for p in _MIIO_PATHS:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

# ── 阈值常量（统一头部，可配置） ─────────────
TEMP_COOLING = 28       # 体感≥28 → 制冷
TEMP_DEHUMID_LOW = 26   # 除湿温度下限（对齐"不算热"）
TEMP_DEHUMID_HIGH = 28  # 除湿温度上限
HUM_DEHUMID_ON = 65     # 除湿开启湿度阈值
HUM_DEHUMID_OFF = 60    # 除湿关闭湿度阈值（滞回 5%）
TEMP_ABSOLUTE_FLOOR = 24# 除湿温度绝对下限（OR 逃生门，低于此无条件关）
MIN_RUN = 40            # 开一次至少 40 分钟
MIN_OFF = 30            # 关后至少 30 分钟再开
MAX_RUN = 180           # 连续运行超 180 分钟建议切换/关（防死锁）

# ── 持久化状态文件 ────────────────────────
# 用 realpath：cron 侧 ~/.hermes/scripts/ac_advisor.py 是指向本文件的符号链接，
# abspath 会解析到链接所在目录，realpath 才指向真实目录（D:\work\ac-advisor）
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "ac_state.json")

# ── 天气 API ──────────────────────────────
LAT, LON = 31.11, 121.38
WX_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=Asia%2FShanghai"
)

# ── 室内传感器（小米空气净化器 4 Lite） ────
CONFIG_FILE = os.path.join(SCRIPT_DIR, "miio_config.json")

WEATHER_MAP = {
    0: "☀️ 晴", 1:"🌤 少云", 2:"⛅ 多云", 3:"☁️ 阴",
    45:"🌫 雾",
    51:"🌦 毛毛雨",61:"🌧 小雨",63:"🌧 中雨",65:"🌧 大雨",
    71:"🌨 小雪",73:"🌨 中雪",75:"🌨 大雪",
    80:"🌦 阵雨",81:"🌦 小阵雨",82:"🌦 大阵雨",95:"⛈ 雷暴",
}
def weather_cn(code): return WEATHER_MAP.get(code, f"☁️ {code}")


def load_state() -> dict:
    """读取持久化状态，不存在时返回默认"""
    default = {"mode": None, "last_on_at": None, "last_off_at": None, "run_start": None}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r") as f:
            return {**default, **json.load(f)}
    except Exception:
        return default


def save_state(state: dict):
    """写入持久化状态"""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def minutes_since(ts_str: str | None) -> float | None:
    """计算自某个 ISO 时间戳以来的分钟数"""
    if not ts_str:
        return None
    try:
        then = datetime.fromisoformat(ts_str)
        now = datetime.now(tz=then.tzinfo if then.tzinfo else None)
        return (now - then).total_seconds() / 60.0
    except Exception:
        return None


def fetch_weather() -> dict:
    """获取天气数据，带异常处理"""
    try:
        req = urllib.request.Request(WX_URL, headers={"User-Agent": "ac-advisor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def read_indoor(timeout=3.0):
    """读取小米空气净化器 4 Lite 的室内温湿度。
    返回 (温度, 湿度) 或 (None, None)。
    """
    if not os.path.exists(CONFIG_FILE):
        return None, None
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        return None, None

    ip = cfg.get("ip")
    token = cfg.get("token")
    if not ip or not token:
        return None, None

    try:
        from miio import Device
        d = Device(ip, token, timeout=timeout)
        # 4 Lite 是 MIoT 设备，用 get_properties
        # 温度 siid=3 piid=7，湿度 siid=3 piid=1（已验证与屏幕一致）
        r = d.send("get_properties", [
            {"siid": 3, "piid": 7},   # 温度
            {"siid": 3, "piid": 1},   # 湿度（不是 siid=4 piid=1!）
        ])
        if isinstance(r, list) and len(r) >= 2:
            temp = r[0].get("value") if isinstance(r[0], dict) else None
            hum = r[1].get("value") if isinstance(r[1], dict) else None
            if temp is not None and hum is not None:
                return round(temp, 1), round(hum, 0)
        # 备用：尝试 raw send
        r2 = d.send("get_prop", ["temp_dec", "humidity"])
        if isinstance(r2, list) and len(r2) >= 2:
            return round(r2[0] / 10, 1), round(r2[1], 0)
    except Exception:
        pass
    return None, None


def main():
    # ── 1. 获取天气数据 ──
    wx = fetch_weather()
    if "error" in wx:
        print(f"⚠️ 天气API失败: {wx['error']}")
        print("🏠 上海闵行 · 定频空调省电顾问")
        print("  数据不可用，请稍后再查")
        return

    cur = wx["current"]
    dai = wx["daily"]
    temp = cur["temperature_2m"]
    feels = cur["apparent_temperature"]
    hum_out = cur["relative_humidity_2m"]
    wcode = cur["weather_code"]
    max_t = dai["temperature_2m_max"][0]
    rain = dai["precipitation_probability_max"][0]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 2. 读取室内温湿度 ──
    indoor_temp, indoor_hum = read_indoor()
    indoor_ok = indoor_temp is not None and indoor_hum is not None

    # 主信号：室内优先，室外回退（仅限体感温度，湿度不用室外）
    signal = feels
    hum_sig = indoor_hum  # 湿度信号只用室内
    src = "室外体感"
    if indoor_ok:
        signal = indoor_temp
        hum_sig = indoor_hum
        src = "室内(净化器)"
    else:
        hum_sig = None  # 室内不可用时，湿度信号为 None——禁掉湿度触发的除湿分支

    # ── 3. 读取持久化状态 ──
    state = load_state()
    now_ts = datetime.now().isoformat()
    since_on = minutes_since(state.get("run_start"))
    since_off = minutes_since(state.get("last_off_at"))

    # ── 4. 决策 ──
    # 决策树：单一转移表 + 滞回区间
    # 优先级：制冷 > 除湿 > 风扇 > 不开

    # 开窗/关窗建议（新增：室外湿度纳入，防回南天误开窗）
    # 注意：上海夏季清晨室外湿度常达 85-95%（晴天也如此），固定阈值会误判关窗；
    # 所以关窗需满足：有雨，或 室外很潮(≥85%) 且 明显比室内潮(高≥10个百分点)。
    rainy = rain >= 50                                  # 有雨
    humid_out = (hum_out is not None
                 and hum_out >= 85
                 and (indoor_hum is None or hum_out >= indoor_hum + 10))
    close_windows = rainy or humid_out                  # 需要关窗防潮

    decision = None
    reason = ""

    # 分支 A: 体感高 → 制冷
    if signal >= TEMP_COOLING:
        reco = max(26, min(28, temp - 7))
        decision = f"制冷模式 {reco}°C + 自动风速"
        reason = f"体感{signal:.1f}°C ≥ {TEMP_COOLING}°C"
        new_mode = "cooling"

    # 分支 B: 温度适中 + 湿度高 → 除湿（仅室内湿度可用时）
    elif (TEMP_DEHUMID_LOW <= signal < TEMP_DEHUMID_HIGH
          and hum_sig is not None
          and hum_sig > HUM_DEHUMID_ON):
        # 检查是否已超最大运行时间
        over_max = since_on is not None and since_on >= MAX_RUN
        # 除湿也要考虑温度下限逃生门：如果温度已经低于绝对下限，不开除湿
        if signal < TEMP_ABSOLUTE_FLOOR:
            decision = "风扇/通风"
            reason = f"温度{signal:.1f}°C已低于{TEMP_ABSOLUTE_FLOOR}°C，开除湿会过冷"
            new_mode = "fan"
        elif over_max:
            decision = "建议切换制冷或关（防死锁）"
            reason = f"除湿已连续运行≥{MAX_RUN}分钟"
            new_mode = "dehumid_alert"
        else:
            decision = "除湿模式（定频自动除湿）"
            reason = f"湿度{hum_sig:.0f}% > {HUM_DEHUMID_ON}%"
            new_mode = "dehumid"

    # 分支 C: 不冷不湿 → 风扇
    elif signal >= TEMP_DEHUMID_LOW:
        # ...（后续代码不变）...
        # 但如果是除湿模式运行中，且温度还在→检查关条件
        if state.get("mode") == "dehumid" and hum_sig is not None:
            if hum_sig < HUM_DEHUMID_OFF or signal < TEMP_ABSOLUTE_FLOOR:
                # 关除湿的条件：湿度已达标 或 温度已低于绝对下限
                decision = "风扇/通风（除湿已达标，可关）"
                reason = f"湿度{hum_sig:.0f}% < {HUM_DEHUMID_OFF}% 或 温度<{TEMP_ABSOLUTE_FLOOR}"
                new_mode = "fan"
            else:
                # 继续除湿
                over_max = since_on is not None and since_on >= MAX_RUN
                if over_max:
                    decision = "建议切换制冷或关（防死锁）"
                    reason = f"除湿已连续运行≥{MAX_RUN}分钟"
                    new_mode = "dehumid_alert"
                else:
                    decision = "除湿模式（继续）"
                    reason = "上一轮建议除湿，湿度和温度尚未达标"
                    new_mode = "dehumid"
        else:
            decision = "风扇够用，不用开空调"
            reason = f"体感{signal:.1f}°C，不算热"
            new_mode = "fan"

    # 分支 D: 凉快 → 不开，但室内湿度高本身即"闷"，无需室外体感背书
    else:
        # 室内温度接近舒适区但湿度 >80% → 本身即闷热，建议开一会儿制冷 (温和,非硬除湿)
        if signal >= 24 and hum_sig is not None and hum_sig > 80:
            decision = "开一会儿制冷 26°C 兼除湿"
            reason = f"室内{signal:.1f}°C/湿度{hum_sig:.0f}%偏高(室外体感{feels:.1f}°C)，体感闷"
            new_mode = "cooling"
        elif signal < TEMP_ABSOLUTE_FLOOR:
            decision = "关掉除湿！已经过冷"
            reason = f"温度{signal:.1f}°C < {TEMP_ABSOLUTE_FLOOR}°C，除湿还在吹会越吹越冷"
            new_mode = "off"
        else:
            if close_windows:
                decision = "不用开空调，关窗防潮+风扇循环"
                why = "室外有雨/潮湿，开窗会把潮气带进屋"
            else:
                decision = "不用开空调，开窗通风+风扇"
                why = "室外干爽，开窗通风更省电"
            reason = f"温度{signal:.1f}°C，凉快；{why}"
            new_mode = "off"

    # ── 5. 应用状态约束（最小运行/停机时间） ──
    if new_mode in ("cooling", "dehumid", "dehumid_alert"):
        # 开：检查关后最小停机时间
        if since_off is not None and since_off < MIN_OFF:
            decision = f"风扇（关后{MIN_OFF}分钟内不重开，还剩{MIN_OFF - int(since_off)}分钟）"
            reason += f"；关后仅{int(since_off)}分钟，<{MIN_OFF}分钟锁定"
            new_mode = "fan_locked"
            # 如果湿度高但锁定中，给个手动建议
            if hum_sig is not None and hum_sig > 80:
                decision += "；实在闷就开一会儿制冷26°C，不闷就关"
        else:
            # 记录运行开始时间
            if state.get("mode") != new_mode:
                state["run_start"] = now_ts
            state["last_on_at"] = now_ts
            state["mode"] = new_mode
    elif new_mode in ("fan", "fan_locked", "off"):
        # 关：检查最小运行时间
        if since_on is not None and since_on < MIN_RUN:
            decision = "继续开着（开够" + str(MIN_RUN) + "分钟再关，已经" + str(int(since_on)) + "分钟）"
            reason += f"；开仅{int(since_on)}分钟，<{MIN_RUN}分钟"
            new_mode = state.get("mode", "unknown")
        else:
            state["last_off_at"] = now_ts
            state["mode"] = new_mode

    save_state(state)

    # 构建运行时间信息
    run_info = ""
    if since_on is not None:
        run_info = f"  已运行: {int(since_on)} 分钟"
        if new_mode in ("cooling", "dehumid"):
            if since_on >= MAX_RUN:
                run_info += f" ⚠️ 超 {MAX_RUN} 分钟，建议切换"
    elif since_off is not None:
        run_info = f"  已关 {int(since_off)} 分钟"

    # ── 6. 输出 ──
    print(f"🏠 上海闵行 · 定频空调省电顾问 v4")
    print(f"📅 {now_str} · {weather_cn(wcode)}")
    print()
    print(f"  室外: {temp:.1f}°C  体感: {feels:.1f}°C  湿度: {hum_out:.0f}%")
    if indoor_ok:
        print(f"  室内: {indoor_temp:.1f}°C  湿度: {indoor_hum:.0f}%  (来源: {src})")
    else:
        print(f"  室内传感器不可用，室外体感仅供参考")
    print(f"  今日最高: {max_t:.1f}°C  降雨: {rain:.0f}%")
    if run_info:
        print(run_info)
    print()

    # 雨天/潮湿警示放结论前（防投递链路润色丢失）
    if close_windows:
        if rainy:
            print("  ⚠️ 今日有雨，请勿开窗（防潮）")
        else:
            print(f"  ⚠️ 室外潮湿({hum_out:.0f}%)，请勿开窗（防潮）")

    print(f"  💡 {decision}")
    print(f"     ({reason})")
    print()

    if close_windows:
        print("  🌧 关窗防潮，开风扇循环（别开窗）")
    if new_mode in ("cooling", "dehumid"):
        print(f"  ⏱ 开够 {MIN_RUN} 分钟再关，关后等 {MIN_OFF} 分钟再开")
        if new_mode == "dehumid":
            print(f"  ⏱ 温度<{TEMP_ABSOLUTE_FLOOR}°C 或 湿度<{HUM_DEHUMID_OFF}% 可关（含逃生门，防过冷）")
        else:
            print(f"  💡 湿度<60%且温度≤27 / 湿度60-70%且≤26 → 可关")
            # 定频制冷省电策略：集中开一轮拉到26°C就关，别一直挂着（定频频繁启停费电）
            print(f"  🔁 省电：集中开一轮 {TEMP_DEHUMID_LOW}°C（30-40分钟）→ 关 → 风扇循环；别一直开(定频启停费电)")
    print()
    print("─" * 40)
    print("数据: Open-Meteo + 小米净化器4Lite · 状态机v4")


if __name__ == "__main__":
    main()