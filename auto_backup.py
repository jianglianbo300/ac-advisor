#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动备份脚本 - 每次运行将未提交的改动自动 commit + push 到 GitHub。
由 Hermes cron 每天定时调用，或手动运行。

用法:
  python auto_backup.py              # 有改动才 commit + push
  python auto_backup.py --force     # 强制 commit（含空提交）
  python auto_backup.py --push-only  # 只 push，不 commit
"""

import subprocess
import sys
from datetime import datetime


def run(cmd, cwd):
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def main():
    repo_dir = r"D:\work\ac-advisor"
    force = "--force" in sys.argv
    push_only = "--push-only" in sys.argv

    if not push_only:
        # Check if there are changes
        out, _, _ = run("git status --porcelain", repo_dir)
        if not out and not force:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 无改动，跳过")
            return

        # Stage everything
        run("git add -A", repo_dir)

        # Commit
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if out:
            # Count changed files
            n = len(out.splitlines())
            msg = f"auto-backup: {ts} ({n} files changed)"
        else:
            msg = f"auto-backup: {ts}"
        run(f'git commit -m "{msg}"', repo_dir)

    # Push to all remotes
    out, err, rc = run("git push", repo_dir)
    if rc == 0:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 推送成功")
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 推送失败: {err}")


if __name__ == "__main__":
    main()
