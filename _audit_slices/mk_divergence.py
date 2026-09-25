# -*- coding: utf-8 -*-
"""生成"分歧对齐表"：Nemotron-550B vs DeepSeek V4.1 Flash vs 我的复核。

数据来源：
  results/ultra550b__U*.recovered.md  （recover_raw.py 从 .raw 事件流恢复）
  results/dsv41__U1_env_model.json    （唯一成功 JSON 化的一份）
  severity_recheck.md                 （我上一轮的独立复核）

背景：Nemotron 报 16 条（3 P0 / 4 P1 / 6 P2 / 3 P3），声称"高风险·上线前必须逐项修"。
我逐条核实其 3 条 P0。
"""
import io

rows = []


def add(claim, nem, ds, mine, verdict):
    rows.append((claim, nem, ds, mine, verdict))


add("DP 电价套利是否有效",
    "P0 完全失效，谷电预冷永不触发",
    "P2 舒适罚项比电费高 17.4×，电价信号被淹没",
    "**P2**：电价信号**近乎**失效但非完全失效；真危害是谷时段下探越过 24°C 地板",
    "Nemotron 说重了；DeepSeek 与我一致")

add("fan_locked 落入 else 被改 cooling",
    "P0 强制改模式，压缩机意外启动",
    "T7 但判 latent（decide() 从不返回 fan_locked）",
    "**latent（不可达）**：ac_watch.py 全文 0 处出现 fan_locked，无调用方可传入",
    "**Nemotron 错**：代码 L1404 已显式处理 fan_locked，仅 real==on 时才落 else，且不可达")

add("_run_start_kwh 陈旧 → 学习回路污染",
    "P0 学习回路核心污染源",
    "T6 P2，已修 20bdfc2",
    "**已修**：L1340 手动锚点 + L1400 验证回读路径均已刷新；kWh 门控另有 L1231 pop 清理",
    "**Nemotron 过期**：它审的是修复前快照（其报告自述基线 v8.59，但 U3 切片未含 T6 修复）")

add("湿度缺失判 success 污染学习",
    "P1",
    "T4 但判 latent（50/50 生产日志有 kwh_at_decision，被旁路）",
    "**latent（被旁路）**：生产路径未走到",
    "两者一致（deepseek 已判 latent）")

add("find_pre_cool_window 跨零点空窗",
    "未列",
    "T3 但判 latent（零生产调用点）",
    "**latent（死代码）**",
    "一致")

io.open("divergence_table.md", "w", encoding="utf-8").write(
    "# 分歧对齐表\n\n"
    "| # | 争议点 | Nemotron-550B | DeepSeek V4.1 | 我的复核裁定 | 结论 |\n"
    "|---|--------|---------------|---------------|--------------|------|\n"
    + "\n".join("| %d | %s | %s | %s | %s | %s |" % (i + 1, *r)
                for i, r in enumerate(rows))
    + "\n")

for i, r in enumerate(rows, 1):
    print("─" * 70)
    print("%d. %s" % (i, r[0]))
    print("   Nemotron : %s" % r[1])
    print("   DeepSeek : %s" % r[2])
    print("   我的裁定 : %s" % r[3])
    print("   → %s" % r[4])
print("─" * 70)
print("\n[已写入 divergence_table.md]")
