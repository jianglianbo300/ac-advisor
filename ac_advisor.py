#!/usr/bin/env python3
"""
定频空调省电顾问 v4.2 — 上海闵行
基于 cline(deepseek0731) 审查修正版。
核心修正:
A. 状态持久化: 引入 state.json 记录 mode/last_on/last_off，让 MIN_RUN/MIN_OFF/MAX_RUN 真生效
B. 除湿关标准加温度下限逃生门: 湿度<60 或 温度<24 → 关
C. 回退路径禁用室外湿度判除湿
D. 单一决策转移表 + 滞回区间
E. 除湿下限提到 26°C（对齐"不算热"）+ 湿度阈值 65%
F. 阈值统一头部常量
G. v4.1: 低温高湿分支（24≤T<26 且湿度>65 → 制冷 23°C 强制除湿一轮）+ 除湿占空比随室温修正（低温到温停机占空比↓）
H. v4.2: 分支B(26≤T<28湿度>65) 由「除湿模式」改为「制冷24°C集中一轮」——与降湿优先制冷的结论统一；省电提示按各分支实际设定温度输出；输出注明空调模式为建议值非实测
I. v4.3: 开窗判断加湿度趋势+时段（晴天清晨露水潮气且湿度趋势明显下降→不劝关窗，提示稍后再开）；除湿占空比模型绑定湿度（湿度越高压缩机越卖力，60%→0.7倍/85%→1.1倍）
J. v4.4: 电价按时段动态计算——上海一户一表分时电价 峰0.617(6:00-22:00)/谷0.307(22:00-6:00 半价)，cost_est 按当前时段取价，省电提示标注峰电/谷电
K. v4.5: 审计修复（分支A设定=室内-2防到温空转、删死代码、ac_off_alert落地、last_off_at锚点修复）+ 夜间三方案对比块（睡眠+26/24 与除湿，睡前时段展示）
"""
import json
import os
import sys
import urllib.request
from datetime import datetime

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

# ── 空调功率（松川 KFRd-35GW 定频 1.5 匹） ──
AC_INPUT_W = 1076     # 输入功率 W（铭牌）
AC_COP = 3.25          # 能效比
ELECTRIC_PEAK = 0.617   # 上海居民峰电（6:00-22:00）元/度（一户一表第一档）
ELECTRIC_VALLEY = 0.307 # 上海居民谷电（22:00-6:00）元/度（约半价）
ELECTRIC_PRICE = ELECTRIC_PEAK  # 兼容旧引用（默认峰电）
DEHUMID_DUTY = 0.60    # 定频除湿模式占空比（压缩机跑跑停停, 估算）
COOL_DUTY = 0.70       # 定频制冷模式占空比（估算）
DEHUMID_MIN = 60       # 定频除湿达标通常更慢（风速低, 估算60分钟）
COOL_BURST_MIN = 40    # 制冷集中一轮达标（风速高, 约40分钟）
FILTER_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter_state.json")
FILTER_CLEAN_INTERVAL = 30  # 滤网建议清洗间隔（天）, 脏滤网=风量降=除湿慢=费电
# 铭牌实测: 制冷1076W/5.1A, 热泵1110W, PTC1000W, 最大2400W/11.3A, 2018-09产(老机除湿稍慢属正常)

def dehumid_duty(temp, hum=None):
    """定频除湿占空比：温度基准(室温越低越频繁到温停机，24°C≈25%/26°C+≈60%) × 湿度修正(越潮压缩机越卖力：60%→0.7倍，85%→1.1倍，线性插值)"""
    if temp is None:
        base = DEHUMID_DUTY
    elif temp >= 26:
        base = DEHUMID_DUTY
    elif temp <= TEMP_ABSOLUTE_FLOOR:
        base = 0.25
    else:
        base = 0.25 + (DEHUMID_DUTY - 0.25) * (temp - TEMP_ABSOLUTE_FLOOR) / (26 - TEMP_ABSOLUTE_FLOOR)
    if hum is not None:
        if hum >= 85:
            factor = 1.10
        elif hum <= 60:
            factor = 0.70
        else:
            factor = 0.70 + 0.40 * (hum - 60) / 25.0
        base *= factor
    return base


def kwh_est(active_min, duty=1.0):
    """耗电估算(度): 输入功率 × 占空比 × 时长"""
    return AC_INPUT_W / 1000.0 * duty * (active_min / 60.0)


def current_price():
    """按当前时段取电价：22:00-6:00 谷电半价，其余峰电"""
    h = datetime.now().hour
    return ELECTRIC_VALLEY if h >= 22 or h < 6 else ELECTRIC_PEAK


def cost_est(kwh):
    """电费估算(元)：按当前时段电价（峰/谷）"""
    return kwh * current_price()

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
    "&hourly=relative_humidity_2m,precipitation_probability"
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


NIGHT_HOURS = 6          # 夜间整夜对比时长（小时）
DAYS_PER_MONTH = 30      # 月差价估算天数
NIGHT_DUTY_26 = (2 * 0.55 + 4 * 0.20) / NIGHT_HOURS   # 睡眠+制冷26°C：前2h压1°C轻载、后4h维持
NIGHT_DUTY_24 = (2 * 0.85 + 4 * 0.20) / NIGHT_HOURS   # 睡眠+制冷24°C：前2h硬压3°C、后4h维持
# 说明：睡眠模式设定每小时+1°C → 后半夜设定>室温压缩机基本停，故不用整夜平均 COOL_DUTY(0.70)

def night_cost_lines(indoor_temp, indoor_hum):
    """夜间方案对比（睡前 20:00~次日 6:00 谷电窗口展示）。
    0️⃣ 压一轮即关（用户现行打法，基准参照）vs 1️⃣~3️⃣ 整夜方案。
    定频耗电 = 输入功率 × 占空比 × 时长；谷电 0.307 元/度。
    """
    h = datetime.now().hour
    if not (h >= 20 or h < 6):
        return []
    p = ELECTRIC_VALLEY
    kb = kwh_est(COOL_BURST_MIN, COOL_DUTY)   # 压一轮 24°C 40~60min（对齐文档实测案例 0.5度≈0.15元）
    dd = dehumid_duty(indoor_temp if indoor_temp is not None else 26.5, indoor_hum)
    k26 = kwh_est(NIGHT_HOURS * 60, NIGHT_DUTY_26)
    k24 = kwh_est(NIGHT_HOURS * 60, NIGHT_DUTY_24)
    kd = kwh_est(NIGHT_HOURS * 60, dd)
    lines = [f"🌙 夜间方案对比（谷电 {p:.3f} 元/度）:"]
    lines.append(f"   0️⃣ 压一轮24°C×40~60min: {kb:.2f}度 ≈ {kb * p:.2f}元 ← 最省（能顶到天亮就收工）")
    lines.append(f"   1️⃣ 睡眠+制冷26°C整夜:  {k26:.2f}度 ≈ {k26 * p:.2f}元（怕热醒选它，后半夜基本停）")
    lines.append(f"   2️⃣ 睡眠+制冷24°C整夜:  {k24:.2f}度 ≈ {k24 * p:.2f}元（贵 {(k24 - k26) * p * DAYS_PER_MONTH:.1f}元/月，除湿最快）")
    lines.append(f"   3️⃣ 除湿模式整夜:        {kd:.2f}度 ≈ {kd * p:.2f}元（慢且最贵；睡眠升降温对除湿无效）")
    if indoor_hum is not None and indoor_hum > 70:
        lines.append("   💡 湿度偏高：先压一轮24°C到60%再睡；后半夜闷醒就切睡眠26°C兜底")
    else:
        lines.append("   💡 湿度不高：压一轮收工最省；怕热醒就睡眠26°C整夜")
    return lines


def filter_clean_reminder():
    """滤网清洗提醒(v2.5): 脏滤网=风量降=除湿慢=费电, 2018老机更敏感。"""
    try:
        with open(FILTER_STATE_FILE, encoding="utf-8") as f:
            last = json.load(f).get("last_clean")
        if not last:
            return "  💡 记得每 15~30 天洗一次滤网（脏滤网=除湿慢+费电）"
        days = (datetime.now() - datetime.fromisoformat(last)).days
        if days > FILTER_CLEAN_INTERVAL:
            return f"  ⚠️ 该洗滤网了（距上次清洗 {days} 天）——滤网脏直接拖累除湿速度"
        return None
    except Exception:
        return None


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
    sig_label = "室内" if indoor_ok else "室外体感"

    # ── 3. 读取持久化状态 ──
    state = load_state()
    now_ts = datetime.now().isoformat()
    since_on = minutes_since(state.get("run_start"))
    since_off = minutes_since(state.get("last_off_at"))

    # ── 4. 决策 ──
    # 决策树：单一转移表 + 滞回区间
    # 优先级：制冷 > 除湿 > 风扇 > 不开

    # 开窗/关窗建议（v4.3：室外湿度纳入 + 趋势判断，防回南天误开窗 + 防晴天清晨误关窗）
    # 关窗需满足：有雨，或 室外很潮(≥85%) 且 明显比室内潮(高≥10个百分点)；
    # 例外：清晨(5~11点)晴天无雨、室外湿度虽高但未来3小时趋势明显下降(≥10个百分点) → 露水潮气，太阳出来会散，不劝关窗。
    rainy = rain >= 45                                  # 有雨 (v2.3 对齐换气策略 45% 闸门)
    humid_out = (hum_out is not None
                 and hum_out >= 85
                 and (indoor_hum is None or hum_out >= indoor_hum + 10))
    # 未来3小时湿度趋势（Open-Meteo hourly）
    hum_trend = None
    precip_h3 = None
    hourly = wx.get("hourly") or {}
    h_time = hourly.get("time") or []
    h_hum = hourly.get("relative_humidity_2m") or []
    h_precip = hourly.get("precipitation_probability") or []
    if h_time and h_hum:
        now_h = datetime.now().hour
        idx = next((i for i, t in enumerate(h_time) if len(t) >= 13 and t[11:13] == f"{now_h:02d}"), None)
        if idx is not None:
            fut_hum = [v for v in h_hum[idx+1:idx+4] if v is not None]
            if fut_hum:
                hum_trend = fut_hum[-1] - h_hum[idx]    # 3小时后 - 现在（负=下降）
            fut_precip = [v for v in h_precip[idx+1:idx+4] if v is not None]
            if fut_precip:
                precip_h3 = max(fut_precip)
    # 露水潮气需近3小时也无雨（daily 全天最大降雨概率可能发生在傍晚，清晨其实无雨；precip_h3 缺失时回退 daily）
    near_dry = (precip_h3 if precip_h3 is not None else rain) < 50
    morning_dry = (near_dry
                   and hum_trend is not None and hum_trend <= -10
                   and 5 <= datetime.now().hour < 12)
    close_windows = (rainy or humid_out) and not morning_dry
    if humid_out and morning_dry:
        morning_hint = True
    else:
        morning_hint = False

    decision = None
    reason = ""
    burst_set = None  # 本轮建议的制冷设定温度（v4.2 省电提示用）

    # 分支 A: 体感高 → 制冷
    if signal >= TEMP_COOLING:
        reco = round(max(26, min(28, signal - 2)))  # 设定比室温低2°C，保证压缩机运转（防到温停机空转）
        burst_set = reco
        decision = f"制冷模式 {reco}°C + 自动风速"
        reason = f"{sig_label}{signal:.1f}°C ≥ {TEMP_COOLING}°C"
        new_mode = "cooling"

    # 分支 B0: 低温高湿 → 制冷强制除湿（24≤T<26 且湿度>65；除湿模式此时会到温停机空转）
    elif (TEMP_ABSOLUTE_FLOOR <= signal < TEMP_DEHUMID_LOW
          and hum_sig is not None
          and hum_sig > HUM_DEHUMID_ON):
        burst_set = 23  # 本轮建议的制冷设定温度（低于室温强制压缩机运转）
        decision = "制冷 23°C 强制除湿一轮（40~60分钟，湿度降到60%即关）"
        reason = f"低温高湿：{signal:.1f}°C / 湿度{hum_sig:.0f}%——设定必须低于室温(23<{signal:.0f})才能触发压缩机运转"
        new_mode = "cooling"
    # 分支 B: 温度适中 + 湿度高 → 除湿（仅室内湿度可用时）
    elif (TEMP_DEHUMID_LOW <= signal < TEMP_DEHUMID_HIGH
          and hum_sig is not None
          and hum_sig > HUM_DEHUMID_ON):
        # 检查是否已超最大运行时间（仅当空调当前在运行中；run_start 是上次开机时间，
        # 若已关机则 state.mode=off，不应再触发"连续运行超时"）
        running = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
        over_max = running and since_on is not None and since_on >= MAX_RUN
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
            burst_set = 24
            decision = "制冷 24°C 集中除湿一轮（40~60分钟，湿度降到60%即关）"
            reason = f"湿度{hum_sig:.0f}% > {HUM_DEHUMID_ON}%，26~28°C 区间制冷比除湿更快更省（降湿优先制冷）"
            new_mode = "cooling"

    # 分支 C: 不冷不湿 → 风扇
    elif signal >= TEMP_DEHUMID_LOW:
        # 26≤T<28 且湿度≤65：风扇够用（v4.2 起除湿分支已统一为制冷，mode 不再有 dehumid，旧分支已删）
        decision = "风扇够用，不用开空调"
        reason = f"{sig_label}{signal:.1f}°C，不算热"
        new_mode = "fan"

    # 分支 D: 凉快 → 不开，但室内湿度高本身即"闷"，无需室外体感背书
    else:
        # 湿度爆表提醒已改为下方 ac_off_alert 独立处理；此分支只承接"凉快→不开"
        if signal < TEMP_ABSOLUTE_FLOOR:
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
            # 记录运行开始时间（仅当从"未运行"态进入运行态才重置；
            # 运行中模式切换如 dehumid→cooling/dehumid_alert 保留 run_start，维持连续运行时长）
            if state.get("mode") not in ("cooling", "dehumid", "dehumid_alert"):
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
            # 只在"从运行态转关闭"时刷新 last_off_at；空调本就关着时不刷（保持"已关 X 分钟"锚点真实）
            if state.get("mode") in ("cooling", "dehumid", "dehumid_alert"):
                state["last_off_at"] = now_ts
            state["mode"] = new_mode

    # ── ac_off_alert（文档 v2.3 声明，本次落地）：空调未建议运行 + 湿度爆表 → 提醒开空调压湿度（每天最多1次防轰炸） ──
    ac_alert = ""
    if (new_mode in ("fan", "fan_locked", "off")
            and hum_sig is not None and hum_sig > 78
            and signal is not None and signal >= TEMP_ABSOLUTE_FLOOR
            and state.get("last_alert_day") != datetime.now().strftime("%Y-%m-%d")):
        state["last_alert_day"] = datetime.now().strftime("%Y-%m-%d")
        ac_alert = (f"  ⚠️ 湿度{hum_sig:.0f}%偏高：就算不热，也该开空调压一轮湿度"
                    f"（制冷集中 40~60 分钟，到 60% 关）")

    save_state(state)

    # 构建运行时间信息（基于更新后的状态重新计算，避免旧 run_start 误导时长）
    run_info = ""
    if new_mode in ("cooling", "dehumid", "dehumid_alert"):
        run_now = minutes_since(state.get("run_start"))
        if run_now is not None:
            if run_now < 1:
                run_info = "  本轮刚建议开启"
            else:
                run_info = f"  已运行: {int(run_now)} 分钟"
                if run_now >= MAX_RUN:
                    run_info += f" ⚠️ 超 {MAX_RUN} 分钟，建议切换"
    elif since_off is not None:
        run_info = f"  已关 {int(since_off)} 分钟"

    # ── 6. 输出 ──
    print(f"🏠 上海闵行 · 定频空调省电顾问 v4.5")
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
    elif morning_hint:
        print(f"  🌅 清晨潮气({hum_out:.0f}%)但未来几小时湿度在降，太阳出来会散——过1~2小时再开窗通风")

    print(f"  💡 {decision}")
    print(f"     ({reason})")
    if ac_alert:
        print(ac_alert)
    print(f"  ℹ️ 空调模式为顾问建议值，非遥控器实测（定频空调无智能接口）")
    print()

    if close_windows:
        print("  🌧 关窗防潮，开风扇循环（别开窗）")
    if new_mode in ("cooling", "dehumid"):
        print(f"  ⏱ 开够 {MIN_RUN} 分钟再关，关后等 {MIN_OFF} 分钟再开")
        if new_mode == "dehumid":
            print(f"  ⏱ 温度<{TEMP_ABSOLUTE_FLOOR}°C 或 湿度<{HUM_DEHUMID_OFF}% 可关（含逃生门，防过冷）")
        else:
            print(f"  💡 湿度<60%且温度≤27 / 湿度60-70%且≤26 → 可关")
            # 定频制冷省电策略：设定温度必须低于当前室温，集中开一轮即关（避免到温停机空转）
            print(f"  🔁 省电：制冷设 {burst_set or TEMP_DEHUMID_LOW}°C（须低于室温才有效）集中 40~60 分钟 → 湿度到60%即关 → 风扇循环；别一直开(室温≤设定=到温停机空转，白费电)")
        # 功率感知：公平对比（各自达标时长）
        de_kwh = kwh_est(DEHUMID_MIN, dehumid_duty(indoor_temp, indoor_hum))   # 除湿60分钟（占空比随室温+湿度修正）
        co_kwh = kwh_est(COOL_BURST_MIN, COOL_DUTY)   # 制冷40分钟
        save = abs(de_kwh - co_kwh)
        price_tag = "谷电" if current_price() < ELECTRIC_PEAK else "峰电"
        if co_kwh <= de_kwh:
            tip = (f"⚡ 制冷{burst_set or 26}°C集中{COOL_BURST_MIN}分钟≈{co_kwh:.2f}度({cost_est(co_kwh):.2f}元{price_tag})，"
                   f"比除湿{DEHUMID_MIN}分钟≈{de_kwh:.2f}度省{save:.2f}度/轮 → 降湿优先制冷")
        else:
            tip = (f"⚡ 除湿{DEHUMID_MIN}分钟≈{de_kwh:.2f}度({cost_est(de_kwh):.2f}元{price_tag})，"
                   f"比制冷{burst_set or 26}°C集中{COOL_BURST_MIN}分钟≈{co_kwh:.2f}度省{save:.2f}度/轮")
        print(f"  {tip}")
    reminder = filter_clean_reminder()
    if reminder:
        print(reminder)
    for nl in night_cost_lines(indoor_temp, indoor_hum):
        print(nl)
    print()
    print("─" * 40)
    print("数据: Open-Meteo + 小米净化器4Lite · 状态机v4.5")


if __name__ == "__main__":
    main()