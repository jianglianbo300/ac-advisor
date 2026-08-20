# AGENTS.md — 定频空调省电顾问（ac_advisor）

> 给 AI 代理的接续指南。**新会话开工前先读本文件**，10 秒恢复上下文。
> 最后更新：2026-08-19

## 项目是什么

上海闵行 70 平（关厕门 60 平），两台松川 1.5 匹定频空调（KFRd-35GW，制冷 3500W / 输入 1076W / COP 3.25），实际只用一台（女儿屋）+ 风扇循环。

## 当前架构

| 位置 | 角色 |
|---|---|
| `D:\work\ac-advisor\ac_advisor.py` | **唯一维护点**（代码 + ac_state.json + miio_config.json） |
| `D:\work\ac-advisor\ac_watch.py` | 每2分钟实控巡检（开/关/调目标） |
| `D:\work\ac-advisor\ac_watchdog.py` | 每30分钟心跳看门狗（异常报警） |
| `D:\work\ac-advisor\home_living.py` | 每5分钟换气闭环（到点提醒） |
| `C:\Users\Administrator\.hermes\scripts\ac_advisor.py` | **薄包装器**（exec 跳转到 D:\work） |
| cron `fc62a2bfd83d` | 每天 07:00 早报推微信 |
| cron `1d6c5460de5e` | 每2分钟空调巡检 |
| cron `cca8361f1c4c` | 每30分钟看门狗 |
| cron `c1be342fa05b` | 每5分钟换气到点提醒 |

## 关键注意点

- 脚本内 `SCRIPT_DIR` 用 `os.path.realpath(__file__)` 解析
- 状态文件**唯一**：只写 `D:\work\ac-advisor\ac_state.json`
- **凭据唯一事实源 = `miio_config.json` 顶层 `ip`/`token`**
- 运行日志自动存 `~/.hermes/cron/output/fc62a2bfd83d/*.md`
- **改代码只需改 D:\work 一份，无需手动同步**

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
python ac_watchdog.py --dry       # 看门狗 dry-run
hermes cron run fc62a2bfd83d      # 触发早报
hermes cron list | grep -A 5 fc62a2bfd83d   # 查状态
```

## 版本历史

> 完整版本历史 → [[02_Projects/Active/室内空气环境管理/00_联动决策模型]] §7