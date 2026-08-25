# -*- coding: utf-8 -*-
# 唯一事实源: D:\work\ac-advisor (git repo, v8.30+)。本文件仅是 Hermes cron 入口，请勿在此改逻辑。
"""Hermes cron wrapper -- 真实代码在 D:\\work\\ac-advisor\\home_living.py"""
import os, subprocess, sys

REAL = r"D:\work\ac-advisor\home_living.py"
VENV_PY = r"D:\work\ac-advisor\.venv\Scripts\python.exe"
os.chdir(os.path.dirname(REAL))
os.environ["PATH"] = os.path.dirname(REAL) + os.pathsep + os.environ.get("PATH", "")
subprocess.run([VENV_PY, REAL, "--alert"], check=True)
