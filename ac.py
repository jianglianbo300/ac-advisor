#!/usr/bin/env python3
"""空调统一命令入口 v1.0 —— 所有空调操作一个命令搞定。

用法:
  ac.py status              查看空调/插座/室内状态（功率、温度、湿度）
  ac.py on                  开机（保持当前模式/温度）
  ac.py off                 关机
  ac.py temp 26             开机制冷并设到 26°C
  ac.py mode cool|dry|heat  开机并切模式（cool 制冷 / dry 除湿 / heat 制热）
  ac.py advice              跑完整顾问（读功率+决策+自动控制+TTS，同 cron）

依赖: D:\\work\\ac-advisor\\ac_advisor.py（自动控制/实测功率都在那维护）
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


def cmd_on():
    get_ctrl().send_command("set_power", ["on"])
    print("✅ 已开机（保持当前模式/温度）")


def cmd_off():
    get_ctrl().send_command("set_power", ["off"])
    print("✅ 已关机")


def cmd_temp(n):
    d = get_ctrl()
    st = d.status()
    if not st.is_on:
        d.send_command("set_power", ["on"])
        print("已开机", end="  ")
    if st.mode is None or st.mode.value != "cool":
        d.send_command("set_mode", ["cool"])
        print("已切制冷", end="  ")
    d.send_command("set_tar_temp", [n])
    print(f"设定 {n}°C")


def cmd_mode(m):
    if m not in ("cool", "dry", "heat", "fan", "auto"):
        print(f"❌ 模式 {m} 无效（cool/dry/heat/fan/auto）")
        sys.exit(1)
    d = get_ctrl()
    d.send_command("set_power", ["on"])
    d.send_command("set_mode", [m])
    print(f"✅ 已开机并切到 {m}")


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
