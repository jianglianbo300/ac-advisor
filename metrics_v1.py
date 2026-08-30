#!/usr/bin/env python3
"""
metrics_v1.py —— VI-down 判决日聚合工具（v8.42 封版）

三个输出：
1. 26 存活：state_target_temp=26 的最长连续 tick 段（2min/tick）
   - null 视为 gap
   - 单 gap 豁免并补记入段（宽侧）
   - 非 null 不同值是真离场、断段
   - >90 ticks (3h) 触发 VI-down

2. 回声偏差：companion_target ≠ state_target_temp 的持续片段
   - persist≥2 才是真信号
   - 1-tick 瞬态单独计数，期望值 ≈ 调温次数
   - 持续片段非零 = H1 家族或 app 抢写

3. 暴露量：temp≥27.5 的夜间数（须 ≥2 才有判决资格）

格式行（ac_watch.log tick 行）：
2026-08-30 08:14:17 执行 cooling target=25 → action 开机 · 未知 白天 T=27.0 RH=65% ... target=25C companion_target=25
"""

import re
from datetime import datetime
from typing import List, Tuple, Optional


# ── 解析行 ──
# 格式：2026-08-30 08:14:17 ... target=25C companion_target=25
# miio 读失败时 companion_target=null/None/缺字段
LINE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"  # ts
    r".*?target=(\d+)C"                          # state_target
    r"(?:\s+companion_target=(\d+|null|None))?"  # companion (可选)
)

def parse_line(line: str) -> Optional[Tuple[datetime, int, Optional[int]]]:
    """解析 tick 行，返回 (ts, state_target, companion_target)"""
    m = LINE_RE.search(line)
    if not m:
        return None
    ts_str, state_t, companion_t = m.groups()
    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    state_target = int(state_t)
    # companion: 数字 / null / None / 缺字段 → int 或 None
    if companion_t is None or companion_t.lower() in ("null", "none"):
        companion_target = None
    else:
        companion_target = int(companion_t)
    return ts, state_target, companion_target


def parse_log(lines: List[str]) -> List[Tuple[datetime, int, Optional[int]]]:
    """解析多行日志，过滤无效行"""
    result = []
    for line in lines:
        parsed = parse_line(line)
        if parsed:
            result.append(parsed)
    return result


# ── 26 存活 ──
def survival_26(ticks: List[Tuple[datetime, int, Optional[int]]]) -> dict:
    """
    返回 26 存活分析结果：
    - longest_survival: 最长连续 26 段（ticks 数，单 gap 豁免）
    - episodes: 段列表 [(start_ts, end_ts, length), ...]
    - total_26_ticks: 值为 26 的总 ticks
    """
    if not ticks:
        return {"longest_survival": 0, "episodes": [], "total_26_ticks": 0}

    episodes = []
    current_start = None
    current_len = 0
    gap_len = 0
    total_26 = 0

    for i, (ts, state_t, _) in enumerate(ticks):
        if state_t == 26:
            total_26 += 1
            if current_start is None:
                current_start = ts
                current_len = 1
                gap_len = 0
            else:
                # 如果有 gap，但 gap_len <= 1，豁免并续段
                if gap_len <= 1:
                    current_len += 1 + gap_len
                    gap_len = 0
                else:
                    # gap 太大，断段
                    episodes.append((current_start, ticks[i - 1][0], current_len))
                    current_start = ts
                    current_len = 1
                    gap_len = 0
        else:
            if current_start is not None:
                gap_len += 1
                if gap_len > 1:
                    # 连续 2+ 非 26，断段
                    episodes.append((current_start, ticks[i - gap_len][0], current_len))
                    current_start = None
                    current_len = 0
                    gap_len = 0

    # 收尾
    if current_start is not None:
        episodes.append((current_start, ticks[-1][0], current_len))

    longest = max((e[2] for e in episodes), default=0)
    return {
        "longest_survival": longest,
        "episodes": episodes,
        "total_26_ticks": total_26,
    }


# ── 回声偏差 ──
def echo_divergence(ticks: List[Tuple[datetime, int, Optional[int]]]) -> dict:
    """
    返回回声偏差分析：
    - raw_mismatch: 1-tick 瞬态不匹配数（期望 ≈ 调温次数）
    - episodes_persist_2+: 持续 ≥2 tick 的偏差片段数（真信号）
    - episode_details: [(start_ts, end_ts, length), ...]
    """
    if not ticks:
        return {"raw_mismatch": 0, "episodes_persist_2+": 0, "episode_details": []}

    raw_mismatch = 0
    episodes = []
    current_start = None
    current_len = 0

    for i, (ts, state_t, companion_t) in enumerate(ticks):
        # 偏差定义：companion 非 null 且 ≠ state
        is_mismatch = companion_t is not None and companion_t != state_t
        if is_mismatch:
            raw_mismatch += 1
            if current_start is None:
                current_start = ts
                current_len = 1
            else:
                current_len += 1
        else:
            if current_start is not None:
                if current_len >= 2:
                    episodes.append((current_start, ticks[i - 1][0], current_len))
                current_start = None
                current_len = 0

    # 收尾
    if current_start is not None and current_len >= 2:
        episodes.append((current_start, ticks[-1][0], current_len))

    return {
        "raw_mismatch": raw_mismatch,
        "episodes_persist_2+": len(episodes),
        "episode_details": episodes,
    }


# ── 暴露量 ──
def heat_exposure_nights(log_lines: List[str], threshold: float = 27.5) -> int:
    """统计 temp≥threshold 的夜间数（20-06 点算夜间）"""
    night_temps = {}  # date -> max_temp
    for line in log_lines:
        # 提取 T=xx.x
        m = re.search(r"T=(\d+\.?\d*)\s", line)
        if not m:
            continue
        temp = float(m.group(1))
        # 提取时间
        ts_match = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2}):(\d{2})", line)
        if not ts_match:
            continue
        date = ts_match.group(1)
        hour = int(ts_match.group(2))
        if 20 <= hour or hour < 6:
            if date not in night_temps or temp > night_temps[date]:
                night_temps[date] = temp
    return sum(1 for t in night_temps.values() if t >= threshold)


# ── 自测 ──
def main():
    """读 stdin，输出 matched / skipped / null_ticks"""
    import sys
    lines = sys.stdin.readlines()
    matched = 0
    skipped = 0
    null_ticks = 0
    for line in lines:
        parsed = parse_line(line)
        if parsed:
            matched += 1
            if parsed[2] is None:
                null_ticks += 1
        else:
            skipped += 1
    print(f"matched={matched} skipped={skipped} null_ticks={null_ticks}")


def _selftest():
    """自测用例（全绿才封版）"""
    # 用例 1: 基础 26 存活（无 gap）
    ticks1 = [
        (datetime(2026, 8, 29, 20, 0), 26, 26),
        (datetime(2026, 8, 29, 20, 2), 26, 26),
        (datetime(2026, 8, 29, 20, 4), 26, 26),
        (datetime(2026, 8, 29, 20, 6), 25, 25),  # 离场
    ]
    r1 = survival_26(ticks1)
    assert r1["longest_survival"] == 3, f"case1: got {r1['longest_survival']}"
    assert r1["total_26_ticks"] == 3

    # 用例 2: 单 gap 豁免
    ticks2 = [
        (datetime(2026, 8, 29, 20, 0), 26, 26),
        (datetime(2026, 8, 29, 20, 2), 25, 25),  # gap
        (datetime(2026, 8, 29, 20, 4), 26, 26),
    ]
    r2 = survival_26(ticks2)
    assert r2["longest_survival"] == 3, f"case2: got {r2['longest_survival']}"

    # 用例 3: 双 gap 断段
    ticks3 = [
        (datetime(2026, 8, 29, 20, 0), 26, 26),
        (datetime(2026, 8, 29, 20, 2), 25, 25),
        (datetime(2026, 8, 29, 20, 4), 25, 25),
        (datetime(2026, 8, 29, 20, 6), 26, 26),
    ]
    r3 = survival_26(ticks3)
    assert r3["longest_survival"] == 1, f"case3: got {r3['longest_survival']}"
    assert len(r3["episodes"]) == 2

    # 用例 4: 回声偏差（1-tick 瞬态 vs 持续片段）
    ticks4 = [
        (datetime(2026, 8, 29, 20, 0), 25, 25),
        (datetime(2026, 8, 29, 20, 2), 26, 25),  # 1-tick 瞬态
        (datetime(2026, 8, 29, 20, 4), 26, 26),
        (datetime(2026, 8, 29, 20, 6), 26, 25),  # 持续开始
        (datetime(2026, 8, 29, 20, 8), 26, 25),  # 持续
        (datetime(2026, 8, 29, 20, 10), 26, 26),  # 结束
    ]
    r4 = echo_divergence(ticks4)
    assert r4["raw_mismatch"] == 3, f"case4 raw: got {r4['raw_mismatch']}"
    assert r4["episodes_persist_2+"] == 1, f"case4 episodes: got {r4['episodes_persist_2+']}"

    # 用例 5: companion=null（读失败）
    ticks5 = [
        (datetime(2026, 8, 29, 20, 0), 26, None),
        (datetime(2026, 8, 29, 20, 2), 26, None),
    ]
    r5 = echo_divergence(ticks5)
    assert r5["raw_mismatch"] == 0, f"case5: null should not count"

    # 用例 6: 空输入
    assert survival_26([])["longest_survival"] == 0
    assert echo_divergence([])["raw_mismatch"] == 0

    # 用例 7: 模拟夜（3 次调温 → raw=3, episodes=0）
    ticks7 = [
        (datetime(2026, 8, 29, 20, 0), 25, 25),
        (datetime(2026, 8, 29, 20, 2), 26, 25),  # 调温 25→26，1-tick skew
        (datetime(2026, 8, 29, 20, 4), 26, 26),
        (datetime(2026, 8, 29, 20, 6), 27, 26),  # 调温 26→27
        (datetime(2026, 8, 29, 20, 8), 27, 27),
        (datetime(2026, 8, 29, 20, 10), 25, 27),  # 调温 27→25
        (datetime(2026, 8, 29, 20, 12), 25, 25),
    ]
    r7 = echo_divergence(ticks7)
    assert r7["raw_mismatch"] == 3, f"case7 raw: got {r7['raw_mismatch']}"
    assert r7["episodes_persist_2+"] == 0, f"case7 episodes: got {r7['episodes_persist_2+']}"

    # 用例 8: 暴露量
    log8 = [
        "2026-08-29 20:00:00 ... T=28.0 ... target=25C",
        "2026-08-29 21:00:00 ... T=27.0 ... target=25C",  # 夜间但 <27.5
        "2026-08-30 22:00:00 ... T=28.5 ... target=25C",  # 另一夜 ≥27.5
    ]
    assert heat_exposure_nights(log8) == 2, f"case8: got {heat_exposure_nights(log8)}"

    # 用例 9: 解析行（正常）
    line9 = "2026-08-30 08:14:17 执行 cooling target=25 → action 开机 · 未知 白天 T=27.0 RH=65% dp=19.8C AH=16.7 dRH20=8% dRH60=18% kWh今=3.74/累=75.0 target=25C companion_target=25"
    p9 = parse_line(line9)
    assert p9 is not None
    assert p9[1] == 25 and p9[2] == 25

    # 用例 10: 解析行（companion=null）
    line10 = "2026-08-30 08:14:17 ... target=25C companion_target=null"
    p10 = parse_line(line10)
    assert p10[2] is None

    # 用例 11: 解析行（缺 companion 字段）
    line11 = "2026-08-30 08:14:17 ... target=25C"
    p11 = parse_line(line11)
    assert p11[2] is None

    print("metrics_v1 selftest: ALL PASS (11 cases)")


if __name__ == "__main__":
    _selftest()
