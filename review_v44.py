#!/usr/bin/env python3
"""调强模型终审 ac_advisor.py v4.4 完整方案（多后端可切换）
用法:
  python review_v44.py glm    # 智谱 GLM-5.2 (默认)
  python review_v44.py terra  # TokenFaucet gpt-5.6-terra
  python review_v44.py tfdeep # TokenFaucet deepseek-v4-flash
  python review_v44.py ix     # InferX deepseek-v4-flash
"""
import json, os, sys, urllib.request

BACKENDS = {
    "glm":   dict(url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
                  key="64721d72617346ac8ec4870beef6bbd6.kC322yOykimt7l0I",
                  model="glm-5.2"),
    "terra": dict(url="https://freetokenfaucet.com/v1/chat/completions",
                  key="tf_8f6bd15fe8564bf28f63fbc0c9cd845f",
                  model="gpt-5.6-terra"),
    "tfdeep":dict(url="https://freetokenfaucet.com/v1/chat/completions",
                  key="tf_8f6bd15fe8564bf28f63fbc0c9cd845f",
                  model="deepseek-v4-flash"),
    "ix":    dict(url="https://model.inferx.net/endpoints/v1/chat/completions",
                  key="ix_b0747423cfcc0975741385ba09a1ecc65227859da28c653748b5fa44c71d517d",
                  model="deepseek-v4-flash"),
    "omni-t":dict(url="http://localhost:20128/v1/chat/completions",
                  key="x", model="aug/gpt5.6-terra"),
    "omni-r":dict(url="http://localhost:20128/v1/chat/completions",
                  key="x", model="auto/pro-reasoning"),
    "omni-gl":dict(url="http://localhost:20128/v1/chat/completions",
                  key="x", model="aug/glm-5.2"),
}

backend = sys.argv[1] if len(sys.argv) > 1 else "glm"
conf = BACKENDS.get(backend, BACKENDS["glm"])

DIR = os.path.dirname(os.path.realpath(__file__))
with open(os.path.join(DIR, "ac_advisor.py"), encoding="utf-8") as f:
    code = f.read()

prompt = f"""你是资深 HVAC 工程师 + 自控系统评审专家。下面是"定频空调省电顾问 v4.4"的完整 Python 脚本。

背景：上海闵行 70 平民居，两台松川 1.5 匹定频空调（KFRd-35GW，输入 1076W，COP 3.25），实际只用女儿屋一台 + 风扇循环。每天 07:00 定时跑一次，把省电建议推送到用户微信。室内温湿度来自小米净化器 4Lite（IP 局域网直读），室外来自 Open-Meteo。定频空调无智能接口，脚本输出的是"给用户的建议值"，不是实测模式，用户按遥控器手动操作。

核心策略：定频空调"集中开一轮(40~60分钟)即关 + 风扇循环"，避免一直挂着（室温≤设定=到温停机空转，白费电又除不了湿）；设定温度永远要低于当前室温才能触发压缩机连续运转强制除湿；最近还加入了用遥控器"定时关机 60 分钟"替代手动守候的兜底。电价分时：峰 0.617(6-22点)/谷 0.307(22-6点)。

==================== 完整代码 ====================
{code}
==================== 代码结束 ====================

请以冷峻严格的角度终审这个方案，重点找：
1. 【状态机漏洞】MIN_RUN=40 / MIN_OFF=30 / MAX_RUN=180 的时序约束有没有 bug？什么时候会锁死、死循环或状态错乱？run_start 重置、mode 值（fan/fan_locked/off/cooling/dehumid/dehumid_alert/unknown）流转是否完整？
2. 【除湿逃生门】温度<24°C 无条件关是否充分？会不会有"低温高湿困住不下来"的场景？
3. 【开窗/关窗逻辑】代码里 close_windows/morning_dry 的判定（降雨≥50%、室外湿度≥85%且比室内高≥10、清晨5-11点露水趋势例外）有没有自相矛盾或误报？
4. 【定频省电策略】"压一轮即关"在真实家庭里（人不在、睡觉）的可行性；有没有实际上更省电或更合理的替代？
5. 【分支D】24°C/湿度>80% 的"开一会儿制冷26°C兼除湿"会不会跟 26°C 到温停机冲突（设定26≥室温24时压缩机不转）？
6. 【数值/阈值】占空比模型（温度×湿度插值）、cost 估算、电价时段划分有没有明显错误？
7. 其他任何隐蔽 bug、边界情况、改进建议。

请按编号逐条给结论，最终给一句"方案总评：可用/需小修/有缺陷"。
"""

data = {"model": conf["model"], "messages": [{"role": "user", "content": prompt}], "max_tokens": 8000}
req = urllib.request.Request(
    conf["url"], data=json.dumps(data).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + conf["key"]},
)
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read().decode())
        print(resp["choices"][0]["message"]["content"])
except Exception as e:
    print(f"错误({backend}): {e}")
    if hasattr(e, "read"):
        print(e.read().decode()[:1500])