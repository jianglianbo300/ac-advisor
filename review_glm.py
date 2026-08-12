#!/usr/bin/env python3
"""调智谱 GLM-5.2 审查空调脚本逻辑"""
import json, os, urllib.request


def _load_env():
    """读取同目录 .env（git 已忽略），key 不硬编码在代码里"""
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_env()
KEY = os.environ.get("GLM_API_KEY", "")
prompt = """审查定频空调省电脚本逻辑，找出漏洞和改进建议：

定频1.5匹(松川KFRd-35GW)装上海闵行，两台(厅+2屋)实际只用一台(女儿屋)+风扇循环。

每天从天气API拿体感温度+湿度判断：
1. 体感>=28°C → 开空调，推荐温度=max(26,min(28,室外-7))
2. 湿度>70%且体感>=26°C → 开空调(制冷兼除湿，不用切除湿模式)
3. 体感<26°C → 不开空调，提醒关

关空调标准：湿度<60时温度<=27可关；60-70时<=26；>70时<=25.5
定频开一次至少40分钟，别频繁开关。

漏洞在哪？有什么改进建议？"""

data = {
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 4000
}
req = urllib.request.Request(
    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    data=json.dumps(data).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY}
)
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read().decode())
        print(resp["choices"][0]["message"]["content"])
except Exception as e:
    print(f"错误: {e}")
    if hasattr(e, 'read'):
        print(e.read().decode()[:1000])