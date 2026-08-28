"""Ctrl+C hijack sentinel: probe RegisterHotKey every 15s; on capture, snapshot suspects.

Runs detached (pythonw). Log: D:\\work\\ac-advisor\\ctrlc_sentinel.log
"""
import subprocess, sys, time, datetime, os

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

LOG = r"D:\work\ac-advisor\ctrlc_sentinel.log"
SUSPECT_PATTERNS = "ima|copilot|weixin|wetype|doubao|qq|wps|tabbit|logi|snip|pick|shot|capture|pixpin"

CODE = r'''
import ctypes, ctypes.wintypes as wt
u = ctypes.windll.user32
ok = u.RegisterHotKey(None, 9001, 0x2, 0x43)  # MOD_CONTROL, VK_C
sys_exit = 0 if ok else 1
if ok:
    u.UnregisterHotKey(None, 9001)
sys.exit(sys_exit)
'''

def snapshot():
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-Process | Where-Object { $_.Path } | Select-Object ProcessName, Id, Path | "
        "Sort-Object ProcessName | Format-Table -AutoSize | Out-String -Width 220"],
        capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    return ps.stdout

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

log("sentinel started (pid=%s, interval=15s)" % os.getpid())
consec_fail = 0
while True:
    r = subprocess.run([sys.executable, "-c", CODE], capture_output=True, timeout=20,
                       creationflags=NO_WINDOW)
    if r.returncode == 1:
        consec_fail += 1
        # double-confirm after 2 consecutive failures (avoid transient race)
        if consec_fail >= 2:
            log(f"CTRL+C HELD (confirmed x{consec_fail}). Suspect process snapshot:")
            out = snapshot()
            for line in out.splitlines():
                low = line.lower()
                if any(p in low for p in SUSPECT_PATTERNS.split("|")):
                    log("  " + line.rstrip())
            log("--- end snapshot ---")
            consec_fail = 0  # reset; wait for next capture event
    else:
        consec_fail = 0
    time.sleep(15)
