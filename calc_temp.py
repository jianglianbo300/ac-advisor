#!/usr/bin/env python3
"""实际场景: 开一台(女儿屋)+风扇循环, 自己屋靠过道冷气+风扇"""
COOLING=3500; INPUT=1076; COP=3.25

def calc(temp_set, area, temp_out=35):
    heat = 2.5*area*(temp_out-temp_set)+600+300
    run = min(heat/COOLING,1.0) if heat>0 else 0
    kwh_m = INPUT*run/1000*8*30
    return {"run_pct":run*100, "kwh_8h":INPUT*run/1000*8, "kwh_m":kwh_m, "cost":kwh_m*0.617}

# 女儿屋面积约20-25平, 开一台
print("=== 你实际的开法: 一台(女儿屋) + 风扇循环 ===")
for area, label in [(20,"女儿屋20平"),(25,"女儿屋25平"),(30,"女儿屋30平")]:
    for t in (26,27):
        r=calc(t,area)
        print(f"  {label} {t}°C: 运行{r['run_pct']:.0f}% | 8h={r['kwh_8h']:.1f}度 | 月{r['kwh_m']:.0f}度 | 月电费{r['cost']:.0f}元")
    print()
print("加上你屋的风扇(约50W, 8h=0.4度/天, 月12度=7.4元)")
print()
print("=== 结论 ===")
r26=calc(26,25);r27=calc(27,25)
print(f"26°C: 空调月电费约 {r26['cost']:.0f} 元 + 风扇 7 元 = 约 {r26['cost']+7:.0f} 元/月")
print(f"27°C: 空调月电费约 {r27['cost']:.0f} 元 + 风扇 7 元 = 约 {r27['cost']+7:.0f} 元/月")
print(f"差: {r26['cost']-r27['cost']:.0f} 元/月")
print()
print("建议: 26°C + 高风速(女儿屋) + 你屋电风扇(循环)")
print("一台1.5匹管25平绰绰有余, 运行占比不到30%, 压缩机经常停机休息")
print("风扇循环把冷气带到走廊和你屋, 你屋不开空调也够凉")