#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用当前最强可用模型（glm-5.2 / nemotron-3-ultra-free）对 ac_watch.py decide() 做独立深度审查。
输出：_review_strong_glm52.txt / _review_strong_nemotron.txt / _review_strong_merged.txt
"""
import json
import re
import urllib.request
import urllib.error
import socket
import time
import datetime

import yaml

SETTINGS = r"C:\Users\Administrator\.dsh\settings.yaml"
CREDS = r"C:\Users\Administrator\.dsh\.credentials.yaml"
WATCH = r"D:\work\ac-advisor\ac_watch.py"

socket.setdefaulttimeout(60)


def load_decide_source():
    """从 ac_watch.py 提取 decide() 函数体 + 关键常量区。"""
    src = open(WATCH, encoding="utf-8").read()
    # decide() 函数体
    m = re.search(r"(def decide\(.*?\n(?:\s+.*\n)*?)(?=\n\ndef |\nif __name__)", src, re.S)
    func = m.group(1) if m else "decide() 提取失败"
    # 常量区（含版本头注释前 160 行）
    head = "\n".join(src.splitlines()[:130])
    return head, func


def build_prompt(head, func):
    return f"""你是资深家电自动化/嵌入式状态机审查专家。请深度审查下面这段**定频空调 2 分钟自动控制闭环**的纯决策函数（上海松川 1.5 匹定频机 + 智能插座实测功率 + 室内温湿度传感器）。该函数每 2 分钟运行一次，决定 (mode, target_temp, reason)。

背景事实：
- 定频机：压缩机只有开/停，无变频；"制冷24°C"只是让压缩机持续转（到温会停，风扇继续吹）
- 目标不是把湿度压到最低，而是在 60-70% 附近缓慢波动，省电 + 防过冷
- 已修复过的历史坑：26°C 纯制冷湿度反弹死循环、夜间 60%RH 就停的短循环、关机后 compressor_on_min 残留导致时长虚高、除湿起步 24°C 过冲触发逃生门
- 现有机制：假运行检测、虚拟变频（近达标升温缓除）、无效空耗判定（60min 湿度降幅不足）、逃生门（temp<24）、夜间湿度驱动停止、启动次数限制、压缩机硬上限

请从这些角度找**真实漏洞/改进点**（不是泛泛而谈）：
1. 逻辑漏洞：边界条件、变量未定义、分支不可达、守卫顺序错误（哪些 return 顺序会导致误判）
2. 定频机物理特性坑：到温停机 vs 假运行误判、目标温度与室温关系导致压缩机不转
3. 数据/时序问题：2 分钟 tick 下的 delta_rh 计算、传感器抖动、冷启动
4. 节能/舒适改进：可在现有状态机内低风险落地的（不要大改架构）
5. 可测性：哪些分支实际永远走不到

输出格式（严格）：
## 问题清单
每条：级别(🔴必须修/🟡建议修/🔵可选) | 位置(函数名:行号或分支) | 问题 | 修复建议
## 最重要 3 条
## 一句话结论

要具体，引用实际代码行。不要水话。

=== 常量区（节选）===
{head}

=== decide() 函数 ===
{func}
"""


def call_model(provider, model, prompt, max_tokens=6000):
    providers = yaml.safe_load(open(SETTINGS, encoding="utf-8"))["llm-pi-ai"]["providers"]
    creds = yaml.safe_load(open(CREDS, encoding="utf-8"))
    cfg = providers[provider]
    base = cfg["baseURL"].rstrip("/")
    key = creds[cfg["apiKeyEnv"]]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    msg = d["choices"][0]["message"]
    content = msg.get("content") or ""
    return content


def main():
    head, func = load_decide_source()
    prompt = build_prompt(head, func)
    open(r"D:\work\ac-advisor\_review_strong_prompt.txt", "w", encoding="utf-8").write(prompt)

    jobs = [
        ("sensenova", "glm-5.2", r"D:\work\ac-advisor\_review_strong_glm52.txt"),
        ("opencode-zen", "nemotron-3-ultra-free", r"D:\work\ac-advisor\_review_strong_nemotron.txt"),
    ]
    merged = []
    for provider, model, out in jobs:
        print(f"[{datetime.datetime.now():%H:%M:%S}] calling {provider}/{model} ...", flush=True)
        try:
            text = call_model(provider, model, prompt)
            text = text.replace("\r\n", "\n").strip()
            open(out, "w", encoding="utf-8").write(text)
            print(f"  OK, {len(text)} chars -> {out}", flush=True)
            merged.append(f"===== {provider}/{model} =====\n{text}\n")
        except Exception as e:
            err = f"{provider}/{model}: FAIL {type(e).__name__}: {str(e)[:120]}"
            print(" ", err, flush=True)
            merged.append(f"===== {provider}/{model} =====\n{err}\n")
        time.sleep(2)

    open(r"D:\work\ac-advisor\_review_strong_merged.txt", "w", encoding="utf-8").write("\n".join(merged))
    print("merged -> _review_strong_merged.txt")


if __name__ == "__main__":
    main()
