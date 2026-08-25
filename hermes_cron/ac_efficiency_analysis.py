#!/usr/bin/env python3
# 唯一事实源: D:\work\ac-advisor (git repo)。本文件仅是 Hermes cron 入口，请勿在此改逻辑。
import os, subprocess, sys
REAL = r"D:\work\ac-advisor\ac_efficiency_analysis.py"
os.chdir(os.path.dirname(REAL))
PY = r"D:\work\ac-advisor\.venv\Scripts\python.exe"
if not os.path.exists(PY): PY = sys.executable
subprocess.run([PY, REAL] + sys.argv[1:], check=True)
