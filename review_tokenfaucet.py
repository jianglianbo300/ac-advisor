#!/usr/bin/env python3
"""调 TokenFaucet deepseek-v4-flash 审查空调脚本 (超时重试版)"""
import json, urllib.request

KEY = "tf_8f6bd15fe8564bf28f63fbc0c9cd845f"
prompt = """审查定频空调省电脚本逻辑，找出漏洞和改进建议：

定频1.5匹(松川KFRd-35GW)装上海闵行，两台(厅+2屋)实际只用一台(女儿屋)+风扇循环。

每天从天气API拿体感温度+湿度判断：
1. 体感>=28°C → 开空调，推荐温度=max(26,min(28,室外-7))
2. 湿度>70%且体感>=26°C → 开空调(制冷兼除湿，不用切除湿模式)
3. 体感<26°C → 不开空调，提醒关

关空调标准：湿度<60时温度<=27可关；60-70时<=26；>70时<=25.5
定频开一次至少40分钟，别频繁开关。

漏洞在哪？有什么改进建议？"""

data = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": prompt}], "max_tokens": 4000}
req = urllib.request.Request(
    "https://freetokenfaucet.com/v1/chat/completions",
    data=json.dumps(data).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY}
)
try:
    with urllib.request.urlopen(req, timeout=150) as r:
        resp = json.loads(r.read().decode())
    msg = resp["choices"][0]["message"]
    content = msg.get("content", "")
    if content:
        print("=== ANSWER ===")
        print(content)
    else:
        print("内容为空，reasoning尾部:")
        print(msg.get("reasoning_content","")[-1200:])
except Exception as e:
    err = ""
    if hasattr(e, 'read'):
        try: err = e.read().decode()[:300]
        except: pass
    print(f"错误: {e} {err}")