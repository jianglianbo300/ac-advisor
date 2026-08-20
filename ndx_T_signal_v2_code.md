# ndx_T_signal_v2.py

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K3 波幅策略信号 (实盘组合指数, 2026-08-08 信号源迁移, 08-08 组件消融采纳仅K3)
基于 2010-09 至今实盘组合指数 (东财全球指数, 每日收盘合成)

信号源: 实盘组合 = 美股77.4% (SPX 44.23%+NDX 33.17%) + 欧8.04% (DAX+FTSE 各4.02%)
              + 日7.54% (N225) + 新兴4.93% (SENSEX 2.47%+VNINDEX 2.47%), 现金2.12%不参与
数据: 东财 push2his 100.{SPX,NDX,N225,GDAXI,FTSE,SENSEX,VNINDEX} (curl, requests TLS不稳)
      合成输出 portfolio_composite.csv, 每日追加当日合成收盘
策略 (2026-08-08 组件消融: T+G1 为负资产已移除, 回撤保护全由 K3 承担):
  K3波幅两档: 42日波幅 >16%→仓位50%, >23%→仓位30%, 驻留10日(升快降慢)
  无 T 状态机/动量/均线交叉择时 (消融证实其只砍收益、不贡献回撤保护)
输出: 仓位 ∈ {1.0, 0.5, 0.3}
用法:
  python ndx_T_signal_v2.py        → 默认(alert模式, cron用)
  python ndx_T_signal_v2.py --alert → 同默认
"""

import csv, json, os, sys, math
from datetime import datetime

def _warn(msg):
    print(msg, file=sys.stderr)
    print(msg)

CSV_MAIN = "D:/Knowledge/03_Resources/QDII_Data/portfolio_composite.csv"
CSV_OLD = "D:/Knowledge/03_Resources/QDII_Data/ndx_full_2000_2026.csv"
STATE = "D:/Knowledge/03_Resources/QDII_Data/ndx_t_state_v2.json"
LIVE_POS = "D:/Knowledge/03_Resources/QDII_Data/live_position.json"
WEIGHTS = "D:/Knowledge/03_Resources/QDII_Data/portfolio_weights.json"

# 实盘组合指数成分 (东财全球指数代码 -> 权重)
INDICES = {"SPX": 0.4423, "NDX": 0.3317, "N225": 0.0754,
           "GDAXI": 0.0402, "FTSE": 0.0402, "SENSEX": 0.0247, "VNINDEX": 0.0247}

# ========== K3波幅两档 (2026-08-08 组件消融后唯一信号源, T/G1已移除) ==========
A_VOL_WIN = 42
A_VOL_TH = 0.16        # >16%→仓位50%
A_VOL_TH2 = 0.23       # >23%→仓位30%
A_CAP1 = 0.50
A_CAP2 = 0.30
A_HOLD = 10

# ========== v1.1 数据质量层 (weight-coverage 判据) ==========
# coverage = Σ(可得/非过旧指数权重) / Σ(INDICES 有效总权重)。
# 分母用实际权重求和(INVESTED), 不硬编码 0.9792, 防未来权重表变更后失真。
VALID_COV = 0.90      # coverage >= 0.90 -> VALID, 可正式入账 & 可改仓位
DEGRADED_COV = 0.79   # 0.79 <= cov < 0.90 -> DEGRADED, 只观察、不入账、不调仓
STALE_DAYS = 3        # 单指数数据过旧 / 整体信号过旧的容忍天数

def _atomic_write_json(path, data):
    """状态文件原子写: 先写临时文件再 os.replace, 避免半写入损坏状态 (v1.1 第三批)。"""
    import tempfile
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ========== 数据加载 ==========

def load_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((datetime.strptime(r["date"], "%Y-%m-%d"), float(r["close"])))
    rows.sort()
    return rows

def fetch_latest_indices(days=5):
    """东财全球指数最近 N 日收盘 (curl). 返回 {code: [(date, close), ...] 升序}"""
    import subprocess
    import tempfile
    out = {}
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    tmpdir = "D:/Knowledge/03_Resources/QDII_Data/opencode_tmp"
    os.makedirs(tmpdir, exist_ok=True)
    for code in INDICES:
        try:
            url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                   f"?secid=100.{code}&fields1=f1,f2,f3,f4,f5,f6"
                   "&fields2=f51,f52,f53,f54,f55,f56,f57"
                   "&klt=101&fqt=1&beg=0&end=20500101&lmt=1000")
            tmp = os.path.join(tmpdir, f"em_{code}.json")
            subprocess.run(["curl.exe", "-s", "-A", UA, url, "-o", tmp],
                           timeout=60, capture_output=True)
            with open(tmp, encoding="utf-8") as f:
                d = json.load(f).get("data")
            if d and d.get("klines"):
                seq = []
                for k in d["klines"][-days:]:
                    f2 = k.split(",")
                    seq.append((datetime.strptime(f2[0], "%Y-%m-%d"), float(f2[2])))
                out[code] = seq
        except Exception as e:
            _warn(f"  [WARN] fetch {code} 失败: {type(e).__name__} {e}")
    return out

# ========== 计算函数 ==========

def sma(p, i, w):
    if i < w - 1:
        return None
    return sum(p[i - w + 1:i + 1]) / w

def momentum(prices, i, period=63):
    if i < period:
        return None
    return (prices[i] / prices[i - period] - 1) * 100

def annualized_vol(prices, i, window=42):
    if i < window:
        return None
    logs = [math.log(prices[j] / prices[j - 1]) for j in range(i - window + 1, i + 1)]
    mean = sum(logs) / len(logs)
    var = sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)
    return math.sqrt(var * 252)

def calc_signal(rows, prev_state=None):
    """主函数: 仅 K3 波幅两档 (驻留期: 升快降慢) 决定仓位, 无 T 状态机"""
    dates = [d for d, _ in rows]
    prices = [p for _, p in rows]
    n = len(prices)

    last_i = n - 1
    last_date = dates[last_i]
    vol42 = annualized_vol(prices, last_i, A_VOL_WIN)

    if vol42 and vol42 > A_VOL_TH2:
        want_cap = A_CAP2
    elif vol42 and vol42 > A_VOL_TH:
        want_cap = A_CAP1
    else:
        want_cap = 1.0

    current_cap = want_cap
    last_cap_change = None
    if prev_state:
        current_cap = prev_state.get("current_cap", 1.0)
        lcc_str = prev_state.get("last_cap_change")
        if lcc_str:
            last_cap_change = datetime.strptime(lcc_str, "%Y-%m-%d")

    if want_cap < current_cap:
        # 波幅升档 -> 减仓(risk-off): 立即执行
        current_cap = want_cap
        last_cap_change = last_date
    elif want_cap > current_cap:
        # 波幅回落 -> 升仓(risk-on): 需距上次调仓 ≥10 自然日(驻留, 防反弹踏空反复)
        if last_cap_change is None or (last_date - last_cap_change).days >= A_HOLD:
            current_cap = want_cap
            last_cap_change = last_date

    final_pos = current_cap

    if final_pos >= 0.99:
        label = "full"
    elif final_pos >= 0.45:
        label = "cap1"
    else:
        label = "cap2"

    if label == "full":
        cn = "满仓"
    elif label == "cap1":
        cn = "K3仓位{:.0f}%（波幅{:.1f}%）".format(current_cap * 100, vol42 * 100)
    else:
        cn = "K3仓位{:.0f}%（波幅{:.1f}%）".format(current_cap * 100, vol42 * 100)

    return {
        "date": dates[-1].strftime("%Y-%m-%d"),
        "price": round(prices[-1], 2),
        "vol42": round(vol42, 4) if vol42 else None,
        "want_cap": want_cap,
        "current_cap": current_cap,
        "last_cap_change": last_cap_change.strftime("%Y-%m-%d") if last_cap_change else None,
        "final_pos": final_pos,
        "final_label": label,
        "final_cn": cn,
    }

# ========== 02_信号日志 自动落库 (2026-08-16, MOC 硬规则#3 自动化) ==========
VAULT_LOG = "D:/Knowledge/02_Projects/Active/量化策略/02_信号日志.md"

def append_signal_log(sig, changed, prev, signal_status):
    """每日信号行插入 02_信号日志.md 表格末行之后。
    幂等: 同一信号日期只记一行; 非交易日不入表; 表格行必须紧贴(不插空行)。
    失败只 WARN 不抛——落库是合规动作, 不允许它阻断信号推送。"""
    try:
        d = datetime.strptime(sig["date"], "%Y-%m-%d")
        if d.weekday() >= 5:
            return
        if not os.path.exists(VAULT_LOG):
            _warn("  [WARN] 信号日志不存在, 跳过落库")
            return
        with open(VAULT_LOG, encoding="utf-8") as f:
            lines = f.read().splitlines()
        logged = {l[2:12] for l in lines if l.startswith("| 2026-") or l.startswith("| 2025-")}
        if sig["date"] in logged:
            return
        vol = f"{sig['vol42']*100:.2f}%" if sig.get("vol42") else "N/A"
        want = sig.get("want_cap", 1.0)
        want_str = "100%" if want >= 0.99 else f"{want*100:.0f}%"
        pos = f"{sig['final_pos']*100:.0f}%"
        if signal_status != "VALID":
            chg = f"⚠️ 数据{signal_status}，保留原仓位（coverage {sig.get('coverage',0)*100:.0f}%）"
        elif changed and prev:
            chg = f"{prev.get('final_cn','?')} → {sig['final_cn']}"
        elif want > sig["current_cap"]:
            lcc = sig.get("last_cap_change") or "?"
            chg = f"want {want_str}，驻留锁（{lcc} 起 10 自然日）未满 → 沿用"
        else:
            chg = "—"
        row = (f"| {sig['date']} | {sig['price']:.2f} | {vol} | 已消融 | {want_str} "
               f"| {pos} | {chg} | hermes / cron 自动 |")
        last_tbl = max(i for i, l in enumerate(lines) if l.startswith("| 2"))
        lines.insert(last_tbl + 1, row)
        tmp = VAULT_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, VAULT_LOG)
    except Exception as e:
        _warn(f"  [WARN] 信号日志落库失败(不影响信号): {type(e).__name__} {e}")

# ========== 主入口 ==========

def main():
    alert_mode = len(sys.argv) == 1 or "--alert" in sys.argv

    rows_main = load_csv(CSV_MAIN)

    # 有效总权重 = 实际参与组合的权重求和 (不硬编码 0.9792)
    INVESTED = sum(INDICES.values())

    idx = fetch_latest_indices()

    # ---- v1.1 数据质量层: 以 weight-coverage 判据取代 n_idx 计数 ----
    # missing = 完全没抓到;  stale = 抓到但数据过旧(>STALE_DAYS)。
    # 二者都不贡献今日 coverage, 也都没有"正式入账"资格 => 避免污染 42 日波动率历史。
    def _data_quality(idx):
        if not idx:
            return 0.0, sorted(INDICES.keys()), []
        newest = max(v[-1][0] for v in idx.values())
        available = 0.0
        missing, stale = [], []
        for code in INDICES:
            seq = idx.get(code)
            if not seq:
                missing.append(code)
                continue
            last = seq[-1][0]
            if (newest - last).days > STALE_DAYS:
                stale.append(code)
            else:
                available += INDICES[code]
        return available / INVESTED, sorted(missing), sorted(stale)

    cov, missing, stale = _data_quality(idx)

    # 状态 = coverage 主判据 (0.79 / 0.90)
    if cov < DEGRADED_COV:
        signal_status = "NO_SIGNAL"
    elif cov < VALID_COV:
        signal_status = "DEGRADED"
    else:
        signal_status = "VALID"

    # 组合合成: 仅 VALID 才把(部分指数)合成结果正式写入 portfolio_composite.csv。
    # DEGRADED/NO_SIGNAL 即使算出观察值, 也不入正式历史, 只留作展示。
    newest = None
    comp = None
    if idx:
        last_main_d = rows_main[-1][0]
        newest = max(v[-1][0] for v in idx.values())
        if newest > last_main_d:
            # 计算 from CSV 最后日期 到 newest 的累计合成收益:
            # 逐日推进, 每只指数按自身序列对齐(缺失日沿用上一已知收盘, 权重按当日可得成分归一)
            c = rows_main[-1][1]
            day = last_main_d
            cursor = {code: 0 for code in idx}  # 指向<=day的最后位置
            while day < newest:
                next_days = [v[ci][0] for code, v in idx.items()
                             for ci in range(cursor[code], len(v))
                             if v[ci][0] > day]
                nd = min(next_days) if next_days else None
                if nd is None:
                    break
                # 推进各成分到 nd (取 <= nd 的最新收盘)
                tot = 0.0; wsum = 0.0
                for code, seq in idx.items():
                    while cursor[code] + 1 < len(seq) and seq[cursor[code] + 1][0] <= nd:
                        cursor[code] += 1
                    if cursor[code] > 0:
                        r = seq[cursor[code]][1] / seq[cursor[code] - 1][1] - 1
                        tot += INDICES[code] * r
                        wsum += INDICES[code]
                    elif cursor[code] == 0:
                        wsum += INDICES[code]
                if wsum > 0:
                    c *= (1 + tot / wsum)
                day = nd
            comp = c
            if signal_status == "VALID":
                # 仅 VALID 正式入账; 非 VALID 不写入, 防止坏数据污染正式历史
                try:
                    with open(CSV_MAIN, "a", encoding="utf-8", newline="") as f:
                        f.write(f"{newest.strftime('%Y-%m-%d')},{comp:.4f}\n")
                except Exception as e:
                    _warn(f"  [WARN] 组合CSV回写失败: {e}")
                rows_main.append((newest, comp))
                _warn(f"  [INFO] 组合指数更新 {newest.strftime('%Y-%m-%d')} -> {comp:.4f}")

    prev = None
    if os.path.exists(STATE):
        try:
            prev = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            prev = None

    sig_main = calc_signal(rows_main, prev)

    sig_date = datetime.strptime(sig_main["date"], "%Y-%m-%d")
    days_stale = (datetime.now() - sig_date).days

    # 整体数据过旧 -> 一律降级 (禁止新建议/调仓/入账)
    if days_stale > STALE_DAYS and signal_status != "NO_SIGNAL":
        signal_status = "DEGRADED"

    # ---- 组装数据质量说明 (coverage / 缺失 / 过旧 / 观察值) ----
    data_note = ""
    if signal_status == "NO_SIGNAL":
        data_note = f"coverage {cov*100:.0f}% (<{DEGRADED_COV*100:.0f}%)，缺失: {'、'.join(missing) or '—'}"
    elif signal_status == "DEGRADED":
        np_ = [f"coverage {cov*100:.0f}% (<{VALID_COV*100:.0f}%)"]
        if missing:
            np_.append(f"缺失: {'、'.join(missing)}")
        if stale:
            np_.append(f"过旧: {'、'.join(stale)}")
        if days_stale > STALE_DAYS:
            np_.append(f"数据基于 {sig_main['date']} 已过 {days_stale} 天")
        if newest and comp is not None:
            np_.append(f"观察值 {newest.strftime('%Y-%m-%d')}:{comp:.2f}(未入正式历史)")
        data_note = "；".join(np_)

    sig_main["signal_status"] = signal_status
    sig_main["data_note"] = data_note
    sig_main["coverage"] = round(cov, 4)
    sig_main["missing"] = missing

    # ---- 数据质量权限层: 只有 VALID 有权改变仓位 ----
    # DEGRADED / NO_SIGNAL 一律保留上一仓位 (坏数据无权改变资金仓位)
    # v1.2 (2026-08-16): last_cap_change 一并回滚——calc_signal 在冻结前可能已把
    # 驻留锚点推到今天(幻影锚点), 让下次升仓白等多 10 天, 属慢性保守泄漏
    if signal_status != "VALID":
        if prev:
            sig_main["final_pos"] = prev.get("final_pos", sig_main["final_pos"])
            sig_main["current_cap"] = prev.get("current_cap", sig_main["current_cap"])
            sig_main["last_cap_change"] = prev.get("last_cap_change", sig_main.get("last_cap_change"))
            sig_main["final_label"] = prev.get("final_label", sig_main["final_label"])
            sig_main["final_cn"] = ("数据异常，保留原仓位（无新建议）" if signal_status == "NO_SIGNAL"
                                    else "数据降级，保留原仓位（仅观察）")
        else:
            sig_main["final_cn"] = "数据异常，无法判定（保留默认满仓观察）"

    changed = prev is None or (
        prev.get("final_label") != sig_main["final_label"]
        or prev.get("current_cap") != sig_main.get("current_cap", 1.0)
    )

    _atomic_write_json(STATE, sig_main)

    # 每日信号行自动落库 (幂等: 同日期/非交易日跳过)
    append_signal_log(sig_main, changed, prev, signal_status)

    pct_main = sig_main["final_pos"] * 100
    vol_main = f"{sig_main['vol42']*100:.1f}%" if sig_main["vol42"] else "N/A"
    cap_str = f"波幅档{['一档','二档'][sig_main['final_label']=='cap2'] if sig_main['final_pos'] < 0.99 else '满仓档'}"

    live_pos = 1.0
    if os.path.exists(LIVE_POS):
        try:
            live_pos = json.load(open(LIVE_POS, encoding="utf-8")).get("pos", 1.0)
        except Exception:
            pass
    dev = (live_pos - sig_main["final_pos"]) * 100
    dev_tag = "偏离0% ✓" if abs(dev) < 0.5 else f"偏离{dev:+.0f}pp ({'超配' if dev > 0 else '低配'})"

    report_lines = [
        f"K3 实盘组合指数 [{sig_main['date']}]",
        f"  实盘组合指数: {sig_main['price']:.2f} | 42日波幅: {vol_main}",
        f"  状态: {sig_main['final_cn']} | 仓位: {pct_main:.0f}% ({cap_str})",
        f"  实盘: {live_pos*100:.0f}% vs 策略 {pct_main:.0f}% | {dev_tag}",
    ]
    if signal_status != "VALID":
        report_lines.append(f"  ⚠️ 数据: {signal_status} (coverage {cov*100:.0f}%) — {data_note}")
    report = "\n".join(report_lines)

    if alert_mode:
        if signal_status == "NO_SIGNAL":
            print(report)
            if prev:
                print("  数据异常，保留原仓位，不产生新建议（请检查数据源）")
            else:
                print("  数据异常，无法判定（保留默认满仓观察）")
        elif signal_status == "DEGRADED":
            print(report)
            print("  数据降级，仅观察，不改变仓位（请检查数据源）")
        elif changed and prev:
            print(report)
            print(f"  变化: {prev.get('final_cn','?')} -> {sig_main['final_cn']}")
        elif not prev:
            print(report)
            print("  (首次运行)")
        else:
            print(f"K3 [{sig_main['date']}] {sig_main['final_cn']} (无变化)")
    else:
        print(report)

if __name__ == "__main__":
    main()
```
