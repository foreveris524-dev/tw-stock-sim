"""
台股選股雷達 — 每週自動更新 watchlist.json
掃描候選股票池，計算 RSI / KD / MACD，打分後寫入 watchlist.json
由 cron 在每週五 14:40 收盤後觸發
"""
import json, os, time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

TZ_TW   = timezone(timedelta(hours=8))
TODAY   = datetime.now(TZ_TW).strftime("%Y-%m-%d")
BASE    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 候選股票池（專注三核心標的） ─────────────────────────────────────────────
UNIVERSE = [
    ("0050",   "0050.TW",   "元大台灣50"),
    ("2330",   "2330.TW",   "台積電"),
    ("00981A", "00981A.TW", "統一主動台股增長"),
]

# 三支全部釘選，永遠保留
PINNED = {"0050", "2330", "00981A"}

# ── 工具函式 ─────────────────────────────────────────────────────────────────

def fetch_ohlc(ticker, range_="3mo"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&range={range_}")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ScanBot/1.0)"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            result = data["chart"]["result"][0]
            q  = result["indicators"]["quote"][0]
            ts = result["timestamp"]
            rows = [
                (ts[i], q["open"][i], q["high"][i], q["low"][i], q["close"][i])
                for i in range(len(ts))
                if None not in (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
            ]
            if not rows:
                return None
            return {k: [r[j] for r in rows]
                    for j, k in enumerate(("ts", "opens", "highs", "lows", "closes"))}
        except Exception as e:
            if attempt == 2:
                return None
            time.sleep(2 ** attempt)

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    diffs  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(diffs)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return round(100 - 100 / (1 + ag / al), 1) if al else 100.0

def calc_kd(highs, lows, closes, period=9, smooth=3):
    if len(closes) < period:
        return None, None
    k_vals, prev_k = [], 50.0
    for i in range(period - 1, len(closes)):
        hmax = max(highs[i - period + 1:i + 1])
        lmin = min(lows[i - period + 1:i + 1])
        rsv  = (closes[i] - lmin) / (hmax - lmin) * 100 if hmax != lmin else 50
        prev_k = (prev_k * (smooth - 1) + rsv) / smooth
        k_vals.append(prev_k)
    d_vals, prev_d = [], 50.0
    for kv in k_vals:
        prev_d = (prev_d * (smooth - 1) + kv) / smooth
        d_vals.append(prev_d)
    return round(k_vals[-1], 1), round(d_vals[-1], 1)

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    def ema(data, p):
        k, e = 2 / (p + 1), [data[0]]
        for v in data[1:]:
            e.append(v * k + e[-1] * (1 - k))
        return e
    ef, es = ema(closes, fast), ema(closes, slow)
    ml  = [ef[slow - 1 + i] - es[slow - 1 + i] for i in range(len(closes) - slow + 1)]
    sl_ = ema(ml, signal)
    return round(ml[-1], 3), round(sl_[-1], 3), round(ml[-1] - sl_[-1], 3)

def kd_recently_crossed(k_series, d_series, lookback=3):
    """最近 N 根 K 棒內是否發生 KD 金叉（K 從下穿過 D）"""
    if len(k_series) < lookback + 1 or len(d_series) < lookback + 1:
        return False
    for i in range(-lookback, 0):
        if k_series[i - 1] <= d_series[i - 1] and k_series[i] > d_series[i]:
            return True
    return False

# ── 打分邏輯 ─────────────────────────────────────────────────────────────────

def score_stock(code, ticker, name):
    hist = fetch_ohlc(ticker)
    if not hist or len(hist["closes"]) < 30:
        return None

    closes = hist["closes"]
    highs  = hist["highs"]
    lows   = hist["lows"]

    rsi        = calc_rsi(closes)
    k, d       = calc_kd(highs, lows, closes)
    _, _, macd_hist = calc_macd(closes)
    price      = closes[-1]

    if any(v is None for v in [rsi, k, d, macd_hist]):
        return None

    # 計算 K/D 序列供金叉判斷
    period, smooth = 9, 3
    k_series, d_series = [], []
    prev_k, prev_d = 50.0, 50.0
    for i in range(period - 1, len(closes)):
        hmax = max(highs[i - period + 1:i + 1])
        lmin = min(lows[i - period + 1:i + 1])
        rsv  = (closes[i] - lmin) / (hmax - lmin) * 100 if hmax != lmin else 50
        prev_k = (prev_k * (smooth - 1) + rsv) / smooth
        prev_d = (prev_d * (smooth - 1) + prev_k) / smooth
        k_series.append(prev_k)
        d_series.append(prev_d)

    pts = 0
    reasons = []

    # KD 金叉（近3根）
    if kd_recently_crossed(k_series, d_series, lookback=3):
        pts += 2
        reasons.append(f"KD金叉(K{k}/D{d})")
    elif k > d:
        pts += 1
        reasons.append(f"K>D({k}/{d})")

    # RSI 健康區間
    if 40 <= rsi <= 65:
        pts += 2
        reasons.append(f"RSI健康={rsi}")
    elif 35 <= rsi < 40 or 65 < rsi <= 70:
        pts += 1
        reasons.append(f"RSI邊緣={rsi}")

    # MACD 柱狀圖正值
    if macd_hist > 0:
        pts += 2
        reasons.append("MACD多頭")
    elif macd_hist > -0.5:
        pts += 1
        reasons.append("MACD轉折中")

    # 未超買加分
    if k < 80:
        pts += 1
    if rsi < 70:
        pts += 1

    return {
        "code":    code,
        "name":    name,
        "ticker":  ticker,
        "price":   round(price, 2),
        "rsi":     rsi,
        "k":       k,
        "d":       d,
        "macd_hist": macd_hist,
        "score":   pts,
        "reasons": "；".join(reasons),
    }

# ── 主程式 ───────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"選股雷達掃描  {TODAY}  共 {len(UNIVERSE)} 支候選")
    print(f"{'='*55}")

    results = []
    for code, ticker, name in UNIVERSE:
        print(f"  掃描 {code} {name}...", end=" ", flush=True)
        r = score_stock(code, ticker, name)
        if r:
            tag = "✅" if r["score"] >= 5 else "—"
            print(f"{tag} 分={r['score']}  {r['reasons']}")
            results.append(r)
        else:
            print("略過（資料不足）")
        time.sleep(0.4)

    # 依分數排序
    results.sort(key=lambda x: x["score"], reverse=True)

    # 釘選 + 高分候選
    pinned    = [r for r in results if r["code"] in PINNED]
    qualified = [r for r in results if r["code"] not in PINNED and r["score"] >= 5]

    watchlist = {
        "updated":    TODAY,
        "pinned":     [r["code"] for r in pinned],
        "candidates": pinned + qualified,
    }

    out_path = os.path.join(BASE, "watchlist.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*55}")
    print(f"✅ 釘選：{[r['code'] for r in pinned]}")
    print(f"✅ 新增候選：{len(qualified)} 支")
    for r in qualified:
        print(f"   {r['code']} {r['name']:10s}  分={r['score']}  {r['reasons']}")
    print(f"已寫入 watchlist.json（共 {len(watchlist['candidates'])} 支）")

if __name__ == "__main__":
    main()
