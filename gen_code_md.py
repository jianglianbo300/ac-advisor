src = open(r'D:\Knowledge\03_Resources\Scripts\ndx_T_signal_v2.py', encoding='utf-8').read()
md = '# ndx_T_signal_v2.py\n\n```python\n' + src + '\n```\n'
out = r'D:\work\ac-advisor\ndx_T_signal_v2_code.md'
with open(out, 'w', encoding='utf-8') as f:
    f.write(md)
print('OK, wrote', len(md), 'bytes to', out)
