#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v8.62 回归自检：TTS 三级降级链 小米音箱 → edge-tts → SAPI。

背景（用户 2026-09-27 反馈「空调语音走小爱音箱啊，笔记本外放声音小」）：
音箱通道 `xiaomi_tts.py` 2026-08-13 就建好了，但一直**未被 ac_watch 调用** ——
共享封装 `ac_tts.py` 从未存在，文档里「接入位置」写的都是规划。v8.62 才真正接上。

要锁定的性质：
  1. 音箱是**第一顺位**（用户诉求就是声音大）
  2. 音箱失败必须**继续往下走**，不能整条哑掉（实测 token 过期 401/70016
     时仍成功落到笔记本并产出 15408 字节 MPEG）
  3. 三级顺序固定，不得把兜底提到前面
  4. 音箱异常必须被吞掉（绝不影响 command → verify → commit 主链路）
  5. 静音时段收口仍在 tts_speak() 单点（v8.61 的性质不得回退）

跑法： python test_v862_tts_chain.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

SRC = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ac_watch.py")
CHAIN = ["_tts_speaker", "_tts_edge", "_tts_sapi"]


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    return bool(cond)


def main():
    ok = True
    with open(SRC, encoding="utf-8") as f:
        src = f.read()

    print("== 1. 音箱为第一顺位 ==")
    m = re.search(r"def _speak\(\):(.*?)\n\n", src, re.S)
    body = m.group(1) if m else ""
    ok &= check(bool(body), "找到 tts_speak 内的 _speak() 链")
    for name in CHAIN:
        ok &= check(name in body, f"链中包含 {name}")
    idx = [body.find(n) for n in CHAIN]
    ok &= check(all(i >= 0 for i in idx) and idx == sorted(idx),
                f"三级顺序固定 音箱→edge→SAPI（位置 {idx}）")
    ok &= check(body.strip().startswith("if _tts_speaker"),
                "第一顺位是音箱（而非兜底）")

    print("\n== 2. 音箱失败不阻断后续 ==")
    ok &= check(re.search(r"if _tts_speaker\(text\):\s*\n\s*return\s*\n\s*if _tts_edge",
                          src) is not None,
                "音箱 return 之后紧接 edge-tts（非直接结束链）")
    ok &= check("if _tts_edge(text):" in body and "_tts_sapi(text)" in body,
                "edge 之后仍有 SAPI 兜底")

    print("\n== 3. 音箱异常被吞（不得影响控制主链路）==")
    fn = re.search(r"def _tts_speaker\(.*?(?=\ndef |\nclass )", src, re.S)
    fbody = fn.group(0) if fn else ""
    ok &= check("except Exception:" in fbody, "_tts_speaker 有裸 except 兜底")
    ok &= check("return False" in fbody, "失败返回 False 而非抛出")
    ok &= check("subprocess.run" in fbody and "timeout=" in fbody,
                "subprocess 带 timeout（token 失效时不会挂死主循环）")
    ok &= check("os.path.exists(script)" in fbody, "脚本缺失时短路返回（不抛）")

    print("\n== 4. 复用既有脚本，不重写 ==")
    ok &= check("xiaomi_tts.py" in fbody, "调用既有 xiaomi_tts.py（未另写一份实现）")
    # 剥离 docstring 与注释后再查「是否内联了实现」——
    # 否则「不重写 miotspec/action 逻辑」这句解释性说明自己就会命中关键字。
    code_only = re.sub(r'"""(?:.|\n)*?"""', "", fbody)
    code_only = re.sub(r"#.*", "", code_only)
    ok &= check("miot" not in code_only.lower(),
                "代码里未内联 miotspec/DID 逻辑（避免第二份会腐烂的副本）")
    ok &= check("SOUND_DID" not in code_only and "miocapi" not in code_only.lower(),
                "代码里未复制 DID/凭据常量")

    print("\n== 5. v8.61 静音收口未回退 ==")
    ok &= check("TTS_QUIET_HOURS = (22, 8)" in src, "静音窗口仍是 22-08")
    ok &= check(re.search(r"def tts_speak\(text\):.*?if _tts_quiet\(\):\s*\n\s*return", src, re.S)
                is not None, "静音判定仍在 tts_speak() 单点收口")
    ok &= check("NIGHT = (23, 7)" in src, "控制逻辑 NIGHT 窗口未被 TTS 污染")

    print("\n" + ("ALL PASS" if ok else "HAS FAILURE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
