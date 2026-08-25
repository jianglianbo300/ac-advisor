# hermes_cron/ — Hermes cron 入口 wrappers

唯一事实源 = 仓库根的业务代码（ac_watch.py 等）。
本目录文件是 `D:\Hermes_Data\.hermes\scripts\` 中同名文件的受管副本：
Hermes cron 只能执行 HERMES_HOME/scripts/ 内的脚本，故入口必须放在那里。
**改逻辑请改仓库根业务脚本，然后同步本目录与 scripts/ 两处 wrapper。**

| scripts/ 文件 | 指向 | 用途 |
|---|---|---|
| ac_watch_wrapper.py | ../ac_watch.py | 每2分钟实控（v8.29 静默版：只透传真实动作/故障行） |
| ac_collect_wrapper.py | ../ac_collect.py | 每15分钟只读采集（固定 Python312 解释器，micloud 依赖） |
| ac_advice_wrapper.py | ../ac_advice.py | 每天08:30 米家调参建议 |
| home_living_alert.py | ../home_living.py --alert | （停用）换气到点提醒，走 repo .venv |
