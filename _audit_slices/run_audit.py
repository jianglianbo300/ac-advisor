# -*- coding: utf-8 -*-
"""双模型交叉审计驱动器：DeepSeek V4.1 Flash vs Nemotron-3-Ultra-550B。
对每个审计单元，两个模型审同一份代码，输出结构化 JSON 便于对齐分歧。"""
import io, json, os, subprocess, sys, time

SLICES = ['U1_env_model', 'U2_learn_thermal', 'U3_reconcile', 'U4_commit_learn', 'U5_control']
BASE = r'D:\work\ac-advisor\_audit_slices'

# Windows: `cline` 是 npm 的 .cmd shim，subprocess 不走 PATHEXT 解析，
# 必须给完整路径 + shell=True（否则 WinError 2）。
CLINE = os.path.join(os.environ.get('APPDATA', ''), 'npm', 'cline.cmd')
MODELS = {
    'dsv41':     'cline-free/deepseek-v4.1-flash',
    'ultra550b': 'nvidia/nemotron-3-ultra-550b-a55b:free',
}
TIMEOUT = 1800

PROMPT_TMPL = """你是空调控制策略与 Python 生产代码的外审专家。这是真实的住宅空调控制器项目（v8.58）的一个模块切片。

严格按下面要求审阅，**只输出 JSON，不要任何前言、解释或 markdown 代码块围栏**。

JSON 结构：
{{
  "defects": [
    {{
      "id": "D1",
      "severity": "P0|P1|P2",
      "title": "一句话标题",
      "location": "函数名 或 行号",
      "why": "为什么是错的（因果）",
      "impact": "实际后果（具体工况/数值）",
      "fix": "具体修法"
    }}
  ],
  "undef_behavior": ["未定义行为或崩溃风险，每条一句"],
  "uncertain": ["你不确定但从代码看不合理的地方"]
}}

严重度定义：P0=会导致设备损坏/控制死锁/进程崩溃；P1=功能明显错误或安全网缺失；P2=健壮性/可维护性。
要求：宁缺毋滥，**没把握的放 uncertain，不要编造**。同一问题只报一次。

代码（原文件 {fname}，{nlines} 行）：

```
{code}
```
"""


def extract_text(path, want_raw=False):
    """从 cline --json 输出提取正文（含 reasoning 通道）。
    cline 是**流式**输出：每个 chunk 只带 1~8 个字符，必须全部拼接，
    不能去重（早期版本用 prev_t 去重，会误删合法的连续重复片段）。
    """
    text, reason = [], []
    for line in io.open(path, encoding='utf-8', errors='replace'):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ev = d.get('event')
        if not isinstance(ev, dict) or ev.get('type') != 'content_start':
            continue
        ct = ev.get('contentType')
        if ct == 'reasoning':
            r = ev.get('reasoning')
            if isinstance(r, str):
                reason.append(r)
        else:
            t = ev.get('text')
            if isinstance(t, str):
                text.append(t)
    body, think = ''.join(text), ''.join(reason)
    # 模型有时把答案全放进 reasoning 通道（MiMo/V4.1 都出现过），
    # 正文为空时用 reasoning 兜底，否则会误判成"模型没输出"。
    payload = body if body.strip() else think
    tail = ''
    if not body.strip() and think.strip():
        tail = think[-4000:]
    if want_raw:
        return payload, think
    return payload


def parse_json(txt):
    """尽量从模型输出里抠出 JSON；截断时尝试补齐引号/括号。"""
    s = txt.strip()
    # 去掉可能的围栏
    if s.startswith('```'):
        s = s.split('\n', 1)[-1]
        if s.rstrip().endswith('```'):
            s = s.rstrip()[:-3]
    # 取第一个 { 到最后一个 }
    i, j = s.find('{'), s.rfind('}')
    if i >= 0 and j > i:
        s = s[i:j+1]
    try:
        return json.loads(s)
    except Exception as e:
        err = str(e)
    # 截断修复：被 timeout 砍掉的输出常常少了收尾的 ] }，
    # 试着手工补全，把已解析出的 defect 救回来。
    if s.startswith('{'):
        for suffix in ('"]}}', ']}', '}]}', '}]}}', '"}]}', '}}'):
            try:
                return json.loads(s + suffix)
            except Exception:
                continue
        # 退一步：只截到最后一个完整的 }，把 defects 数组尾巴切掉
        k = s.rfind('},')
        if k > 0:
            for suffix in ('}]}', ']}', '}]}}'):
                try:
                    return json.loads(s[:k+1] + suffix)
                except Exception:
                    continue
    return {'_parse_error': err, '_raw_head': txt[:800], '_raw_tail': txt[-800:]}


def run_one(tag, model, unit, force=False):
    outdir = os.path.join(BASE, 'results')
    os.makedirs(outdir, exist_ok=True)
    raw = os.path.join(outdir, f'{tag}__{unit}.raw')
    res = os.path.join(outdir, f'{tag}__{unit}.json')
    if os.path.exists(res) and not force:
        return 'cached'
    code = io.open(os.path.join(BASE, unit + '.py'), encoding='utf-8').read()
    prompt = PROMPT_TMPL.format(fname=unit + '.py', nlines=len(code.splitlines()), code=code)
    t0 = time.time()
    cmd = [CLINE, '-m', model, '--json', prompt, '-c', BASE]
    with io.open(raw, 'w', encoding='utf-8') as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                           timeout=TIMEOUT, shell=False)
        f.flush()
    el = time.time() - t0
    txt = extract_text(raw)
    parsed = parse_json(txt)
    parsed['_meta'] = {'model': model, 'unit': unit, 'seconds': round(el, 1),
                       'raw_chars': len(txt), 'exit': p.returncode}
    with io.open(res, 'w', encoding='utf-8') as f:
        f.write(json.dumps(parsed, ensure_ascii=False, indent=2))
    nd = len(parsed.get('defects', []) or [])
    return f'{el:6.0f}s  defects={nd}'


if __name__ == '__main__':
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for tag, model in MODELS.items():
        for unit in SLICES:
            if only and only not in (tag, unit):
                continue
            try:
                r = run_one(tag, model, unit)
            except Exception as e:
                r = f'ERROR {type(e).__name__}: {e}'
            print(f'  {tag:10s} {unit:16s} {r}', flush=True)
