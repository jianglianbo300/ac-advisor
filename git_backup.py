#!/usr/bin/env python3
"""Obsidian vault 每日自动备份到 Git
检测变更 → commit → push 到 Gitee
由 Hermes cron 每日 03:00 调用
"""
import subprocess
import sys
from datetime import datetime

REPO = r"D:\Knowledge"
LOG = r"D:\Knowledge\.git_backup.log"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd, cwd=REPO):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def main():
    log("Git backup starting")

    # 检查是否有变更
    out, err, code = run(["git", "status", "--porcelain"])
    if code != 0:
        log(f"git status failed: {err}")
        return 1

    if not out.strip():
        log("No changes, skip")
        return 0

    changed = out.strip().split("\n")
    log(f"Found {len(changed)} changed files")

    # Stage all
    _, err, code = run(["git", "add", "-A"])
    if code != 0:
        log(f"git add failed: {err}")
        return 1

    # Commit
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"auto: vault backup {date_str} ({len(changed)} files)"
    _, err, code = run(["git", "commit", "-m", msg])
    if code != 0:
        log(f"git commit failed: {err}")
        return 1
    log(f"Committed: {msg}")

    # Push to all remotes
    out, _, _ = run(["git", "remote"])
    remotes = out.split("\n") if out else []
    for remote in remotes:
        remote = remote.strip()
        if not remote:
            continue
        _, err, code = run(["git", "push", remote, "main"])
        if code != 0:
            # 尝试 master
            _, err2, code2 = run(["git", "push", remote, "master"])
            if code2 != 0:
                log(f"push to {remote} failed: {err}")
                continue
        log(f"Pushed to {remote}")

    log("Git backup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
