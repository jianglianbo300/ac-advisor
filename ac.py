#!/usr/bin/env python3
"""空调统一命令入口 v1.1 —— 所有空调操作一个命令搞定。

用法:
  ac.py status              查看空调/插座/室内状态（功率、温度、湿度）
  ac.py on                  开机（保持当前模式/温度）
  ac.py off                 关机
  ac.py temp 26             开机制冷并设到 26°C
  ac.py mode cool|dry|heat  开机并切模式（cool 制冷 / dry 除湿 / heat 制热）
  ac.py advice              跑完整顾问（读功率+决策+自动控制+TTS，同 cron）

依赖: D:\\work\\ac-advisor\\ac_advisor.py（自动控制/实测功率都在那维护）

v1.1（2026-08-16）: on/off/temp 改为走 apply_and_commit 统一接口
（command→verify→按真实设备状态写 ac_state.json），修掉手动操作后
watcher 读到 stale state 的问题（AGENTS.md P2-b 待办）。
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import ac_advisor


def get_ctrl():
    ac_advisor.ac_control_init()
    if ac_advisor.AC_CTRL is None:
        print("❌ 空调插座不可达（检查 miio_config.json 的 ac_partner）")
        sys.exit(1)
    return ac_advisor.AC_CTRL


def cmd_status():
    ac_advisor.ac_control_init()
    ac_w = ac_advisor.read_ac_power()
    t, h = ac_advisor.read_indoor()
    if ac_w:
        print(f"🔌 空调: 运行中  实测功率 {ac_w}W")
    else:
        print("🔌 空调: 已关（插座实测）")
    if t is not None:
        print(f"🌡️  室内: {t}°C  湿度 {h}%")
    else:
        print("🌡️  室内传感器不可用")


def _commit(new_mode, target_temp=None):
    """走 apply_and_commit 统一接口：执行 → verify → 按真实结果写 state。"""
    state = ac_advisor.load_state()
    ctrl = ac_advisor.apply_and_commit(new_mode, target_temp, state)
    if ctrl["status"] == "failed":
        reason = ctrl.get("reason", "")
        print(f"⚠️  执行未完成（{reason}）—— 设备状态未变更/未落盘，勿重复操作")
        sys.exit(1)
    action = ctrl.get("action", "") or "无需动作（状态已一致）"
    print(f"✅ {action}")


def cmd_on():
    # 保持当前目标温度（state.target_temp），走制冷开
    state = ac_advisor.load_state()
    target = state.get("target_temp")
    _commit("cooling", target if isinstance(target, int) else None)


def cmd_off():
    _commit("off")


def cmd_temp(n):
    _commit("cooling", n)


def cmd_mode(m):
    if m not in ("cool", "dry", "heat", "fan", "auto"):
        print(f"❌ 模式 {m} 无效（cool/dry/heat/fan/auto）")
        sys.exit(1)
    # apply_and_commit 支持 cooling(cool)/dehumid(dry)/fan/off；
    # heat/auto 为手动裸模式（advisor 状态机不建模）→ send_command + 手动对账 state
    if m == "cool":
        _commit("cooling", None)
        return
    if m == "dry":
        _commit("dehumid")
        return
    d = get_ctrl()
    d.send_command("set_power", ["on"])
    d.send_command("set_mode", [m])
    # 手动对账：裸模式按"运行中"记账（模式未知按 cooling 计，同 reconcile_state 惯例）
    state = ac_advisor.load_state()
    now_ts = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    was_on = state.get("mode") in ("cooling", "dehumid", "dehumid_alert")
    if not was_on:
        state["run_start"] = now_ts
        state["last_on_at"] = now_ts
    state["mode"] = "cooling"
    state.pop("last_off_at", None)
    ac_advisor.save_state(state)
    print(f"✅ 已开机并切到 {m}（手动裸模式，状态按运行中记账）")


def cmd_advice():
    runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_advisor.py"), run_name="__main__")


USAGE = __doc__.split("依赖:")[0]


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        print(USAGE)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "status":
        cmd_status()
    elif cmd == "on":
        cmd_on()
    elif cmd == "off":
        cmd_off()
    elif cmd == "temp" and rest:
        cmd_temp(int(rest[0]))
    elif cmd == "mode" and rest:
        cmd_mode(rest[0])
    elif cmd == "advice":
        cmd_advice()
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
