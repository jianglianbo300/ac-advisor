# -*- coding: utf-8 -*-
"""从生产 ac_advisor.py 重新生成 5 个审计单元切片。

背景：切片几经修复（v8.59 的 T1/T5/T6/D2），手工维护的 U*.py 会与生产代码
漂移——verify_findings.py 直接 exec 这些切片，于是"验证通过"可能只是在验证
一份旧快照。本脚本以 AST 取整函数（避免切断 try 块），并打印哈希供核对。

用法： python mk_slices.py          # 重新生成全部
       python mk_slices.py --check  # 只核对当前切片是否与生产一致
"""
import ast
import hashlib
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.join(os.path.dirname(BASE), "ac_advisor.py")

# 单元 → 函数名列表（顺序即输出顺序）
UNITS = {
    "U1_env_model": [
        "comfort_index", "dew_point", "muggy_level", "seasonal_adjustments",
        "compute_optimal_schedule", "find_pre_cool_window",
        "predict_dehumidify_need",
    ],
    "U2_learn_thermal": [
        "load_learned", "save_learned", "evaluate_and_learn", "log_decision",
        "load_thermal_data", "save_thermal_data", "_thermal_event_usable",
        "record_thermal_event", "fit_thermal_model", "predict_cooling_time",
    ],
    "U3_reconcile": [
        "_anchor_oscillating", "reconcile_state", "verify_socket",
        "verify_target_temp", "apply_state_from_verify",
    ],
    "U4_commit_learn": ["apply_and_commit"],
    "U5_control": [
        "ac_control_init", "ac_apply", "_alert_ctrl_failure", "read_indoor",
        "_read_indoor_once", "_notify_toast", "miio_discover", "heal_partner_ip",
    ],
}

# 每个单元需要一并带出的模块级常量（切片本身要能独立 exec）
CONSTS = [
    "AC_INPUT_W", "ELECTRIC_PEAK", "ELECTRIC_VALLEY", "DEHUMID_DUTY",
    "COOL_DUTY", "COOL_BURST_MIN", "DEHUMIDIFY_COOL_TARGET",
    "CTRL_FAIL_ALERT_THRESHOLD", "NIGHT_HOURS", "TEMP_COOLING",
    "TEMP_ABSOLUTE_FLOOR", "EVAL_DELAY_MIN", "EVAL_STALE_MIN",
]

# 模块级辅助函数：被上述单元调用，须一并带出（否则切片 exec 时报 NameError）
HELPERS = ["kwh_est"]


def load_prod():
    src = io.open(PROD, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    funcs, consts = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = (node.lineno, node.end_lineno)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = (node.lineno, node.end_lineno)
    return lines, funcs, consts


def build_unit(unit, names, lines, funcs, consts, ver):
    out = []
    # 模块级常量区
    out.append(f"# 审计单元 {unit} —— 由 mk_slices.py 从 ac_advisor.py ({ver}) 生成，请勿手改。")
    out.append("# 本单元为 AST 取整函数切片；依赖项由 verify_findings.py 注入。")
    out.append("")
    have_const = False
    for c in CONSTS:
        if c in consts:
            s, e = consts[c]
            if not have_const:
                out.append("# ==== 模块级常量 ====")
                have_const = True
            out.extend(lines[s - 1:e])
    if have_const:
        out.append("")
    for n in HELPERS:
        if n in funcs:
            s, e = funcs[n]
            out.append(f"# ==== {n}  (原文件行 {s}-{e}，模块级辅助) ====")
            out.extend(lines[s - 1:e])
            out.append("")
    for n in names:
        if n not in funcs:
            raise SystemExit(f"ERROR: {unit} 需要的函数 {n} 在 ac_advisor.py 中不存在")
        s, e = funcs[n]
        out.append(f"# ==== {n}  (原文件行 {s}-{e}) ====")
        out.extend(lines[s - 1:e])
        out.append("")
    return "\n".join(out) + "\n"


def main():
    check = "--check" in sys.argv
    lines, funcs, consts = load_prod()
    ver = ""
    for l in lines[:60]:
        if "VERSION" in l and "=" in l and "ac_advisor" not in l.lower():
            pass
    # ac_advisor.py 无版本号，用内容哈希标识
    h = hashlib.sha256(io.open(PROD, "rb").read()).hexdigest()[:12]
    print(f"生产 ac_advisor.py sha256[0:12] = {h}")
    print(f"行数 = {len(lines)}")
    bad = []
    for unit, names in UNITS.items():
        txt = build_unit(unit, names, lines, funcs, consts, f"sha:{h}")
        path = os.path.join(BASE, unit + ".py")
        if check:
            old = io.open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            same = old == txt
            print(f"  {unit:18s} {'一致' if same else '**已漂移**'}")
            if not same:
                bad.append(unit)
        else:
            io.open(path, "w", encoding="utf-8", newline="\n").write(txt)
            print(f"  {unit:18s} 已写入 ({len(txt.splitlines())} 行)")
    if check and bad:
        print("\n漂移单元:", ", ".join(bad), "→ 运行 python mk_slices.py 重新生成")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
