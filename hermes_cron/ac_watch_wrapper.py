#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes cron wrapper -- 真实代码在 D:\work\ac-advisor\ac_watch.py
v8.29 静默版：ac_watch 每轮都打印一行（无需动作/手动保护等），
直接当 stdout 会被 cron 原文推微信 → 每2分钟骚扰一次。
这里改为只透传【真实开关动作/控制失败/传感器故障】的行，其余吞掉。
空输出时 cron 不推送（no_agent 语义：empty stdout = silent）。
"""
import os, re, subprocess, sys

REAL = r"D:\work\ac-advisor\ac_watch.py"
os.chdir(os.path.dirname(REAL))
r = subprocess.run([sys.executable, REAL] + sys.argv[1:],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")

# 有异常退出 → 透传错误现场（cron 会推错误告警）
if r.returncode != 0:
    sys.stdout.write((r.stderr or r.stdout or "unknown error")[-1500:])
    sys.exit(r.returncode)

out = (r.stdout or "")
keep = []
for line in out.splitlines():
    # 只保留这些行：真实执行了开关、控制失败、状态文件损坏、保守关机、假运行熔断
    if (re.search(r"已自动(开|关|制冷|除湿)", line)
            or "自动控制失败" in line
            or "状态文件损坏" in line
            or "保守关机" in line
            or "硬件故障" in line):
        keep.append(line)

print("\n".join(keep))  # 无匹配时输出为空 → cron 静默不推送
