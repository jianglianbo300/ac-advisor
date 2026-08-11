import json, urllib.request
KEY = "tf_8f6bd15fe8564bf28f63fbc0c9cd845f"
prompt = "定频空调多久开关一次最省电？设定温度多少合适？"
data = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500}
req = urllib.request.Request("https://freetokenfaucet.com/v1/chat/completions", data=json.dumps(data).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
with urllib.request.urlopen(req, timeout=120) as r:
    d = json.loads(r.read().decode())
m = d["choices"][0]["message"]
print("ANS:", m.get("content","")[:1000])
print("REA:", m.get("reasoning_content","")[-500:])
