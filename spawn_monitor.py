"""Catch who spawns visible console processes (cmd/powershell/schtasks/conhost).
Poll every 2s, log NEW pids with parent chain. Runs 10 minutes then exits."""
import subprocess, time, datetime

LOG = r"D:\work\ac-advisor\spawn_monitor.log"
TARGETS = {"cmd.exe", "powershell.exe", "schtasks.exe", "conhost.exe", "wscript.exe", "cscript.exe"}

def snap():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress -Depth 2"],
        capture_output=True, text=True, timeout=40,
        creationflags=0x08000000).stdout
    import json
    try:
        data = json.loads(out)
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {p["ProcessId"]: (p["Name"], p.get("CommandLine",""), p["ParentProcessId"]) for p in data}

def pname(pid, cache):
    return cache.get(pid, ("?",))[0]

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

log("=== spawn monitor start (10min) ===")
seen = snap()
end = time.time() + 600
while time.time() < end:
    time.sleep(2)
    now = snap()
    for pid, (name, cmdline, parent) in now.items():
        if pid in seen or name.lower() not in TARGETS:
            continue
        pp = seen.get(parent) or now.get(parent)
        log(f"NEW {name} pid={pid} parent={pp[0] if pp else parent}(pid={parent})")
        log(f"    cmd: {cmdline[:200]}")
    seen = now
log("=== spawn monitor end ===")
