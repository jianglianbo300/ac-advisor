# AGENTS.md — 定频空调省电顾问（ac_advisor）

> 给 AI 代理的接续指南。**新会话开工前先读本文件**，10 秒恢复上下文。
> 最后更新：2026-09-04 (v8.46)

## 项目是什么

上海闵行 70 平（关厕门 60 平），两台松川 1.5 匹定频空调（KFRd-35GW，制冷 3500W / 输入 1076W / COP 3.25），实际只用一台（女儿屋）+ 风扇循环。

> ⚠️ **当前状态（2026-09-04 核对，覆盖 08-25 旧注记）**
> - **Hermes 继续实控空调**：cron `420cdfe1a188`（每2分钟 ac_watch_wrapper.py → ac_watch.py）保持 enabled。「08-23 转只读」决定已作废。
> - **v8.30 白天抖振根治**：`DAY_START_LINE_FLOOR=27` 钳位启动线（学习负偏移曾把启动线压进 [25,26] 开关死区 → 20 分钟短循环）；自学习 `temp_cooling` 下限改为 0。
> - **脚本唯一事实源 = 本 repo**。Hermes cron 入口 wrapper 在 `HERMES_HOME/scripts/`（受管副本见 `hermes_cron/`），**改逻辑只改本 repo，再同步两处 wrapper**；`scripts/` 下旧副本已归档至 `_archive_ac_20260825/`，勿执行。
> - 只读采集 `ac_collect.py`（cron `1a652e2e7da9`）与每日建议 `ac_advice.py`（cron `f5fe9cde6d38`）照常运行。
> - **验收纪律：改完必须看 cron 实际 output 文件（`~/.hermes/cron/output/<job_id>/`），本地测试通过 ≠ 已部署。**
> - 米家阈值必须整数（读数展示可小数，设置阈值禁 25.5/26.5 这类伪精度）。

## 当前架构

| 位置 | 角色 |
|---|---|
| `D:\work\ac-advisor\ac_advisor.py` | **唯一维护点**（代码 + ac_state.json + miio_config.json） |
| `D:\work\ac-advisor\ac_watch.py` | 每2分钟实控巡检（开/关/调目标），cron `420cdfe1a188`（*/2） |
| `D:\work\ac-advisor\ac_collect.py` | 每15分钟只读采集，cron `1a652e2e7da9`（*/15）→ `ac_data/readings.jsonl` |
| `D:\work\ac-advisor\ac_advice.py` | 每天 08:30 米家调参建议推微信，cron `f5fe9cde6d38` |
| `C:\Users\Administrator\.hermes\scripts\ac_watch_wrapper.py` | cron 执行位 wrapper（静默透传真实动作，空 stdout 不推送） |

> ⚠️ **已下线 cron（2026-08-25~08-27 用户裁决「只留美股+空调」精简，生活类提醒不再重建）**：
> `cca8361f1c4c` 看门狗（30min）、`c1be342fa05b` 换气提醒（5min）、`fc62a2bfd83d` 早报（07:00）、`1d6c5460de5e` 空调巡检（已被 420cdfe1a188 取代）。
> `ac_watchdog.py` / `home_living.py` 脚本仍在本 repo 但**无 cron 挂载**——空调侧告警由 ac_watch 内部机制（假运行熔断/状态文件损坏 fail-safe）承担。勿据本文件旧版重建这些 job。

## 关键注意点

- 脚本内 `SCRIPT_DIR` 用 `os.path.realpath(__file__)` 解析
- 状态文件**唯一**：只写 `D:\work\ac-advisor\ac_state.json`
- **凭据唯一事实源 = `miio_config.json` 顶层 `ip`/`token`**
- 主日志 = `D:\work\ac-advisor\ac_watch.log`（每 tick 追加）；cron 只有真实动作才产生推送（wrapper 静默透传），`~/.hermes/cron/output/420cdfe1a188/` 仅存告警类输出
- **改代码只需改 D:\work 一份，无需手动同步**
- **米家阈值必须整数**（2026-08-23 用户反馈"米家没有25.5 是整数"）：米家 App 温度只能设整数，给用户的建议/阈值必须给整数（如 **27开 / 26关**），**禁止再出现 25.5 / 26.5 这类小数阈值**。区分清楚：实测室温/湿度是**读数展示**，可以显示小数；但**设置阈值**（启动线/结束线/目标）必须整数。旧实控脚本（ac_watch.py / ac_advisor.py）里的 26.5 残留属暂停禁用的控制路径，**留作不动、别捞回，更别给它们加回任何控制**。

## 决策逻辑（v8.23b）

> 完整决策引擎 → [[02_Projects/Active/室内空气环境管理/00_联动决策模型]]

- 主信号 = 室内实测温湿度（小米净化器 4 Lite，IP 192.168.71.120）
- 启动线 27°C + 持续 10min（碰一下不算热）
- 白天：温度达标才收手 + AH 迟滞带 + 启停上限 2 次/h
- 夜间：AH 逻辑 + 谷电优先
- 免费干燥门控：露点差 ≥1.5°C → 拦下开机
- 晚间恒温巡航：20-23 点 T≥26.5 → 26°C
- 自学习：双向收敛 / 30min 才回评 / 120min 过期不学

## 数据源

1. 室内 = 小米空气净化器 4 Lite：温度 siid=3 piid=7，湿度 siid=3 piid=1
2. 室外 = 和风天气 CMA（逐小时）；PM2.5 走 Open-Meteo Air Quality

## 常用命令

```bash
cd /d/work/ac-advisor
python ac_advisor.py              # 手动跑一次
python -m py_compile ac_advisor.py  # 语法检查
python ac_watch.py --dry          # 巡检 dry-run
python ac_watch.py --selftest     # 内置 selftest（断言式，打出版本号即全过）
python _replay_v846.py            # v8.46 幻象门控分支重放（18 用例）
```

## 版本历史

> 完整版本历史 → [[02_Projects/Active/室内空气环境管理/00_联动决策模型]] §7