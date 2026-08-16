import json
import urllib.request
import yaml

with open(r'C:\Users\Administrator\.dsh\.credentials.yaml', encoding='utf-8') as f:
    creds = yaml.safe_load(f)
nous_key = creds['NOUS_API_KEY']

CODE = '''def decide(temp, hum, running, since_on, since_off, is_night,
           compressor=None, last_compressor_stop_at=None, cooldown_until_dt=None,
           current_target=26, delta_rh_20min=None, delta_rh_60min=None,
           minutes_since_last_adjust=None, ah=None, compressor_run_min=None,
           night_comp_starts=None):
    if running is None:
        return (None, None, None)
    if running:
        if compressor == "fan_only":
            stop_duration = None
            if last_compressor_stop_at is not None and since_on is not None:
                stop_duration = last_compressor_stop_at
                if stop_duration < 0:
                    stop_duration = None
            in_cooldown = False
            if cooldown_until_dt is not None:
                in_cooldown = datetime.now() < cooldown_until_dt
            if (hum > 66 and stop_duration is not None
                    and stop_duration >= 10 and not in_cooldown):
                new_target = max(16, current_target - 2)
                return ("cooling", new_target,
                        f"压缩机只吹风不转，降{new_target}度重启")
            return (None, None, None)
        if comp_min is not None and comp_min >= 90:
            return ("off", None, f"连续运行{int(comp_min)}分钟，硬上限关机")
    if since_off is not None and since_off < 30:
        return (None, None, None)
    if is_night:
        night_target = max(24, min(26, round(temp - 2)))
        if temp >= 28:
            return ("cooling", night_target, f"夜间室温{temp}度偏热")
        return (None, None, None)
    if temp >= 28:
        t = round(max(26, min(28, temp - 2)))
        return ("cooling", t, f"室内{temp}度偏热")
    return (None, None, None)'''

prompt = f"""审查这段空调状态机决策函数的漏洞和改进建议（上海定频1.5匹，每2分钟运行一次）：

{CODE}

输出格式：逐条 级别(🔴/🟡/🔵)、位置、问题、修复建议。最后给整体结论。简洁，不要水话。"""

def call_model(model, key, base_url):
    data = {"model": model, "messages": [{"role":"user","content":prompt}], "max_tokens": 3000, "temperature": 0.3}
    req = urllib.request.Request(f"{base_url}/chat/completions",
        data=json.dumps(data).encode(),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {key}","User-Agent":"Mozilla/5.0","Accept":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read().decode())
        msg = resp["choices"][0]["message"]
        content = msg.get("content","") or (msg.get("reasoning_content","") or "")
        return content
    except Exception as e:
        return f"ERROR: {e}"

# Longcat
lc = call_model("meituan/longcat-2.0:free", nous_key, "https://inference-api.nousresearch.com/v1")
with open(r"D:\work\ac-advisor\_compare_longcat.txt", "w", encoding="utf-8") as f:
    f.write(lc)
print(f"LONGCAT done, chars={len(lc)}")
print(lc[:300])
print("="*60)

# Deepseek via sensenova
with open(r"C:\Users\Administrator\.hermes\config.yaml", encoding="utf-8") as hcfg:
    h = yaml.safe_load(hcfg)
sens_key = h["providers"]["custom:sensenova-key1-ds"]["api_key"]
ds = call_model("deepseek-v4-flash", sens_key, "https://token.sensenova.cn/v1")
with open(r"D:\work\ac-advisor\_compare_deepseek.txt", "w", encoding="utf-8") as f:
    f.write(ds)
print(f"DEEPSEEK done, chars={len(ds)}")
print(ds[:300])
