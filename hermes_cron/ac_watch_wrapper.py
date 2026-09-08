#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes cron wrapper -- 真实代码在 D:\work\ac-advisor\ac_watch.py
v8.29 静默版：ac_watch 每轮都打印一行（无需动作/手动保护等），
直接当 stdout 会被 cron 原文推微信 → 每2分钟骚扰一次。
这里改为只透传【真实开关动作/控制失败/传感器故障】的行，其余吞掉。
空输出时 cron 不推送（no_agent 语义：empty stdout = silent）。

v8.53 (2026-09-08)：闪窗根治二阶段。上一版（09-07）用 sys._base_executable
仍会在无控制台父进程（Hermes gateway）下创建 conhost 并一闪而过。
本次改用 pythonw.exe（GUI 子系统，根本不创建控制台）+ 临时文件重定向输出：
  - pythonw 无 stdout/stderr 管道，输出经临时文件写回，语义等价
  - 彻底消除本 wrapper → ac_watch.py 这一层的 conhost 创建
"""
import os, subprocess, re, sys, tempfile

REAL = r"D:\work\ac-advisor\ac_watch.py"
_VENV_SP = r"D:\Hermes_Data\.hermes\hermes-agent\venv\Lib\site-packages"
# pythonw.exe：GUI 子系统，无控制台，绝不创建 conhost 窗口。
# 注意：必须用【真解释器】pythonw（uv base，91KB），不能用 venv 的 pythonw
# （45KB uv launcher 壳，re-exec 真解释器时仍会闪 conhost——与 python.exe 同款坑）。
_base = getattr(sys, "_base_executable", sys.executable)
_PYW = os.path.join(os.path.dirname(_base), "pythonw.exe")
if not os.path.isfile(_PYW):
    # 兜底 1：venv Scripts\pythonw.exe
    _cand = os.path.join(os.path.dirname(os.path.dirname(_VENV_SP)), "Scripts", "pythonw.exe")
    if os.path.isfile(_cand):
        _PYW = _cand
    else:
        _PYW = _base  # 兜底 2：退回原解释器（宁可闪窗不可不跑）

_env = dict(os.environ)
if os.path.isdir(_VENV_SP):
    _env["PYTHONPATH"] = _VENV_SP + (os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")

os.chdir(os.path.dirname(REAL))

# 输出经临时文件重定向（pythonw 无控制台，stdout 管道不可用）
_tmp = os.path.join(tempfile.gettempdir(), "ac_watch_out.txt")
try:
    with open(_tmp, "w", encoding="utf-8") as _fout:
        r = subprocess.run([_PYW, REAL] + sys.argv[1:],
                           stdout=_fout, stderr=subprocess.STDOUT, text=True,
                           encoding="utf-8", errors="replace", env=_env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with open(_tmp, "r", encoding="utf-8", errors="replace") as _fin:
        _raw = _fin.read()
finally:
    try:
        os.remove(_tmp)
    except OSError:
        pass

# 有异常退出 → 透传错误现场（cron 会推错误告警）
if r.returncode != 0:
    sys.stdout.write((_raw or "unknown error")[-1500:])
    sys.exit(r.returncode)

out = _raw or ""
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
