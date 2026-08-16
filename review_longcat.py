#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调 Hermes 当前的 longcat 模型审查空调脚本逻辑（v8.9 交叉验证链成员）。

通道：custom:nous（https://inference-api.nousresearch.com/v1）
模型：meituan/longcat-2.0:free（Hermes config.yaml 全局默认模型）

与 review_glm.py / review_tokenfaucet.py 的区别：
- 实时读取 ac_watch.py + ac_advisor.py 完整源码构造 prompt（不手工粘贴，杜绝代码漂移）
- 审查要点与输出格式对齐 _review_longcat.json 惯例（级别/位置/问题/建议/总结）

用法：
  python review_longcat.py                # 审查当前生产代码（ac_watch.py + ac_advisor.py 全量）
  python review_longcat.py --code FILE    # 只审查指定文件
  python review_longcat.py --model XXX    # 覆盖模型名
"""
import json
import os
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCH_PY = os.path.join(SCRIPT_DIR, "ac_watch.py")
ADVISOR_PY = os.path.join(SCRIPT_DIR, "ac_advisor.py")

# 默认走 Hermes 现在的 longcat 通道（config.yaml: model.provider=custom:nous）
DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
DEFAULT_MODEL = "meituan/longcat-2.0:free"


def _load_env():
    """读取同目录 .env（git 已忽略），key 不硬编码在代码里"""
    envf = os.path.join(SCRIPT_DIR, ".env")
    try:
        for line in open(envf, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def _read_code(path, label):
    try:
        with open(path, encoding="utf-8") as f:
            return f"### {label}（{os.path.basename(path)}，{os.path.getsize(path)} 字节）\n```python\n{f.read()}\n```"
    except OSError as e:
        return f"### {label}\n[读取失败: {e}]"


def build_prompt(files):
    """构造审查 prompt：系统背景 + 当前参数 + 完整源码 + 审查要点（对齐 longcat 审查惯例）"""
    parts = ["""你是一位 HVAC 控制系统专家 + 资深 Python 代码审查员。对这套空调自动控制系统中给出的源码做全面审查。请**通读源码后逐条输出**，不要只给泛泛结论。

## 系统背景
上海闵行 70 平，松川 KFRd-35GW 定频 1.5 匹空调（输入功率 1076W，COP 3.25），米家空调伴侣 lumi.acpartner.mcn02 红外控制+功率感知（load_power>300W=压缩机转，5~50W=仅风扇，≤5W=关），小米空气净化器 4 Lite 读室内温湿度（T: siid3/piid7, RH: siid3/piid1）。Hermes cron 每 2 分钟跑一次 ac_watch.py 自动闭环（no_agent），状态持久化到 ac_state.json，白天 TTS 播报、23-7 静音。

## 架构分层
- ac_watch.py = 控制层：状态机 decide() → apply_and_commit()（command→verify→commit）→ 落盘；WATCH_MAX_RUN=90 硬上限防死锁
- ac_advisor.py = 模块：read_indoor/read_ac_power/ac_apply/verify_socket/reconcile_state/apply_and_commit + 顾问模式 main()
- 关键机制：室内传感器主信号；AH=绝对湿度（露点近似）用于夜间启停（停≤14 启≥17）；夜间停止先于 MIN_RUN 守卫（安全类关机优先）；虚拟变频（RH≤58 且跑够 MIN_RUN → target+1°C 封顶26，RH回升≥62 降回24重启）；MIN_RUN=40 / MIN_OFF=30；温度<24°C 逃生门无条件关；手动关后 2h 不自动启动、手动开后 30min 不自动关；kWh 梯形积分（sensor unknown 不覆盖锚点）

## 审查要点
1. 决策逻辑完整性：状态机分支有无遗漏路径/死路/逻辑矛盾
2. 阈值合理性：65/55/58/62/22/28/AH14-17/MIN_RUN 等与实际场景匹配度
3. 边界条件：传感器不可达、功率读不到、状态冲突降级路径安全
4. 安全隐患：误开/误关/死循环/失控/压缩机保护
5. 代码质量：异常处理、变量作用域、空值判断、死代码
6. 架构：reconcile_state 与 decide 交互、双控制器竞态、main() 流程编排

## 输出格式
逐条：级别（🔴 必改 / 🟡 建议 / 🔵 可选）、位置（文件+函数/行号）、问题、具体修复建议。
最后给整体结论 + 0-100 评分。"""]
    parts.extend(files)
    return "\n\n".join(parts)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _load_env()

    base_url = os.environ.get("NOUS_BASE_URL", DEFAULT_BASE_URL)
    model = DEFAULT_MODEL
    targets = [ADVISOR_PY, WATCH_PY]

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif args[i] == "--code" and i + 1 < len(args):
            targets = [args[i + 1]]
            i += 2
        elif args[i] == "--base-url" and i + 1 < len(args):
            base_url = args[i + 1]
            i += 2
        else:
            i += 1

    key = os.environ.get("NOUS_API_KEY", "")
    if not key:
        print("❌ 缺少 NOUS_API_KEY（.env 中未配置）。")
        print("   添加方式：将 Hermes config.yaml 的 providers.custom:nous.api_key 写入 D:\\work\\ac-advisor\\.env 的 NOUS_API_KEY= 行")
        sys.exit(1)

    # 默认按文件逐个审查（避免 65KB 全量 prompt 超 Cloudflare 120s 窗口）
    for p in targets:
        label = "顾问/控制模块" if p == ADVISOR_PY else "自动监控主程序" if p == WATCH_PY else os.path.basename(p)
        print(f"\n{'=' * 60}\n📄 审查: {label} ({os.path.basename(p)})\n{'=' * 60}")
        files = [_read_code(p, label)]
        prompt = build_prompt(files)

        data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 6000,
            "temperature": 0.3,
        }
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(data).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "application/json",
            },
        )
        print(f"🔍 longcat({model}) 审查中，prompt≈{len(prompt)//1000}KB ...")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read().decode())
            msg = resp["choices"][0]["message"]
            content = msg.get("content", "")
            if content:
                print("=== LONGCAIT ANSWER ===")
                print(content)
            else:
                reasoning = (msg.get("reasoning_content") or "").strip()
                if reasoning:
                    print("=== LONGCAIT ANSWER (reasoning) ===")
                    print(reasoning)
                else:
                    print("内容为空（模型未返回无 content 也无 reasoning）")
        except Exception as e:
            err = ""
            if hasattr(e, "read"):
                try:
                    err = e.read().decode()[:500]
                except Exception:
                    pass
            print(f"❌ 调用失败: {e} {err}")


if __name__ == "__main__":
    main()