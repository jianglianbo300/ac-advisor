# -*- coding: utf-8 -*-
"""从 run_audit.py 的 *.raw（JSONL 事件流）里回收**最终回答正文**。

动机（第二轮外审 A6）：run_audit.py 在报告 JSON 解析失败时只把 answer 的
头/尾 2000 字存进 `_raw_head`/`_raw_tail`，并在 run_all.log 里记 defects=0 →
多页的真实审计报告被当成"0 缺陷"静默丢弃。本脚本把 `content_start(`type`=text)`
事件按序拼接，还原完整正文，写到 results/<name>.recovered.md。

用法：python recover_raw.py            # 处理 results 下所有 .raw
      python recover_raw.py 文件名     # 只处理指定（可带/不带 .raw）
只读 .raw，只写 *.recovered.md，不动任何已有文件。
"""
import io
import json
import os
import sys

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def recover(path):
    parts, n_bad = [], 0
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                ev = json.loads(ln).get("event") or {}
            except Exception:
                n_bad += 1
                continue
            if ev.get("type") == "content_start" and ev.get("contentType") == "text":
                parts.append(ev.get("text") or "")
    return "".join(parts), n_bad


def main():
    args = sys.argv[1:]
    names = ([a if a.endswith(".raw") else a + ".raw" for a in args]
             if args else sorted(f for f in os.listdir(RES) if f.endswith(".raw")))
    for name in names:
        src = os.path.join(RES, name)
        if not os.path.exists(src):
            print("[SKIP] %s 不存在" % name)
            continue
        txt, n_bad = recover(src)
        dst = os.path.join(RES, name[:-4] + ".recovered.md")
        if not txt.strip():
            print("[EMPTY] %-32s 无正文（%d 行坏行，%.1f KB）"
                  % (name, n_bad, os.path.getsize(src) / 1024))
            continue
        with io.open(dst, "w", encoding="utf-8") as fh:
            fh.write(txt)
        p0 = txt.count("P0")
        print("[OK  ] %-32s 回收 %6d 字 → %s（P0 出现 %d 次）"
              % (name, len(txt), os.path.basename(dst), p0))


if __name__ == "__main__":
    main()
