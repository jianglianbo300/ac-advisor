#!/usr/bin/env python3
# 唯一事实源: D:\work\ac-advisor (git repo, v8.30+)。本文件仅是 Hermes cron 入口，请勿在此改逻辑。
# Hermes cron wrapper -- real code in D:work/ac-advisor/ac_advice.py (daily Mijia advice)
# Use fixed interpreter Python312 for path consistency.
import os, subprocess
REAL = r"D:\work\ac-advisor\ac_advice.py"
PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
os.chdir(os.path.dirname(REAL))
subprocess.run([PY, REAL] + os.sys.argv[1:], check=True)
