#!/usr/bin/env python3
"""Hermes cron wrapper -- 真实代码在 D:\work\ac-advisor\ac_watch.py"""
import os, subprocess, sys
REAL = r"D:\work\ac-advisor\ac_watch.py"
os.chdir(os.path.dirname(REAL))
subprocess.run([sys.executable, REAL] + sys.argv[1:], check=True)
