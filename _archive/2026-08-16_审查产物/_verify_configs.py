import yaml

d = yaml.safe_load(open(r'C:\Users\Administrator\.dsh\settings.yaml', encoding='utf-8'))
providers = d['llm-pi-ai']['providers']
default = d.get('agent-default-model', {})

print(f"默认模型: {default.get('provider')}/{default.get('model')}\n")
print(f"共 {len(providers)} 个 provider\n")

for name, cfg in providers.items():
    models = cfg.get('models', [])
    model_names = [m.get('name', m['id']) for m in models]
    print(f"  {name:16s} | {len(models):2d} 个模型 | {model_names}")

c = yaml.safe_load(open(r'C:\Users\Administrator\.dsh\.credentials.yaml', encoding='utf-8'))
print(f"\n凭据: {len(c)} 条")
for k in c:
    print(f"  {k:30s} | {str(c[k])[:24]}...")