"""
台股選股雷達 — 每週全市場篩選
Stage 1: TWSE open data → PE / PB / 殖利率 基本面初篩
Stage 2: Yahoo Finance → RSI / KD / MA / MACD 技術面評分
"""
import json, time, statistics, math, sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

TZ_TW = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ_TW).strftime("%Y-%m-%d")

def fetch(url, headers=None, retries=3):
    h = {"User-Agent": "Mozilla/5.0 (compatible; StockScreener/1.0)"}
    if headers:
        h.update(headers)
    for attempt in range(retries):
        try:
            req = Request(url, headers=h)
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt)

# ─── Stage 1: TWSE 基本面初篩 ───────────────────────────────────────────────

def get_twse_fundamentals():
    print("🔍 抓取 TWSE 基本面資料（PE / PB / 殖利率）...")
    data = fetch("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d")
    if not data:
        print("⚠ TWSE API 失敗，嘗試備用...")
        data = fetch("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU")
    return data or []

def fundamental_filter(stocks):
    """
    基本面篩選條件：
    - PE: 0 < PE ≤ 30（合理估值，排除虧損或泡沫）
    - PB: 0 < PB ≤ 5
    - 殖利率 ≥ 1.0%
    """
    passed = []
    for s in stocks:
        try:
            code = s.get("Code", s.get("股票代號", ""))
            name = s.get("Name", s.get("股票名稱", ""))
            pe_str   = s.get("PEratio",       s.get("本益比", "0"))
            pb_str   = s.get("PBratio",       s.get("股價淨值比", "0"))
            dy_str   = s.get("DividendYield", s.get("股利殖利率", "0"))

            if not code or len(code) != 4:
                continue
            # Skip ETFs and special codes
            if not code.isdigit():
                continue

            pe = float(pe_str)  if pe_str  and pe_str  not in ("-", "N/A", "") else 0
            pb = float(pb_str)  if pb_str  and pb_str  not in ("-", "N/A", "") else 0
            dy = float(dy_str)  if dy_str  and dy_str  not in ("-", "N/A", "") else 0

            if pe <= 0 or pe > 30:
                continue
            if pb <= 0 or pb > 5:
                continue
            if dy < 1.0:
                continue

            passed.append({
                "code": code,
                "name": name,
                "pe": round(pe, 2),
                "pb": round(pb, 2),
                "dividend_yield": round(dy, 2),
            })
        except (ValueError, TypeError):
            continue

    print(f"  基本面通過：{len(passed)} 支（共 {len(stocks)} 支上市股）")
    return passed

# ─── Stage 2: Yahoo Finance 技術指標 ────────────────────────────────────────

def get_yahoo_history(code, range_="3mo"):
    ticker = f"{code}.TW"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_}"
    data = fetch(url)
    if not data:
        return None
    try:
        result = data["chart"]["result"][0]
        quotes  = result["indicators"]["quote"][0]
        closes  = quotes.get("close",  [])
        highs   = quotes.get("high",   [])
        lows    = quotes.get("low",    [])
        volumes = quotes.get("volume", [])
        ts      = result.get("timestamp", [])
        # Remove None values (keep aligned)
        rows = [(t,c,h,l,v) for t,c,h,l,v in zip(ts,closes,highs,lows,volumes) if c and h and l]
        if len(rows) < 20:
            return None
        return {
            "timestamps": [r[0] for r in rows],
            "closes":     [r[1] for r in rows],
            "highs":      [r[2] for r in rows],
            "lows":       [r[3] for r in rows],
            "volumes":    [r[4] for r in rows],
        }
    except (KeyError, IndexError, TypeError):
        return None

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(diffs)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)

def calc_kd(highs, lows, closes, period=9, smooth=3):
    if len(closes) < period:
        return None, None
    k_vals = []
    for i in range(period - 1, len(closes)):
        h_max = max(highs[i - period + 1 : i + 1])
        l_min = min(lows[i  - period + 1 : i + 1])
        rsv = (closes[i] - l_min) / (h_max - l_min) * 100 if h_max != l_min else 50
        k_vals.append(rsv)
    # Smooth K
    k_smooth = []
    prev_k = 50
    for rsv in k_vals:
        prev_k = (prev_k * (smooth - 1) + rsv) / smooth
        k_smooth.append(prev_k)
    # Smooth D
    d_smooth = []
    prev_d = 50
    for k in k_smooth:
        prev_d = (prev_d * (smooth - 1) + k) / smooth
        d_smooth.append(prev_d)
    return round(k_smooth[-1], 1), round(d_smooth[-1], 1)

def calc_ma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    def ema(data, period):
        k = 2 / (period + 1)
        e = data[0]
        for v in data[1:]:
            e = v * k + e * (1 - k)
        return e
    def ema_series(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for v in data[1:]:
            result.append(v * k + result[-1] * (1 - k))
        return result
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast[slow-1:], ema_slow[slow-1:])]
    signal_line = ema_series(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line[-signal:], signal_line[-signal:])]
    return round(macd_line[-1], 3), round(signal_line[-1], 3), round(hist[-1], 3)

def calc_avg_volume(volumes, period=20):
    if len(volumes) < period:
        return None
    valid = [v for v in volumes[-period:] if v]
    return int(statistics.mean(valid)) if valid else None

def technical_score(hist):
    """
    技術面評分（0-100）：
    - RSI 40-60：理想，+25分；30-40 或 60-70：尚可 +10分；其餘 0分
    - KD 黃金交叉（K>D 且 K<80）：+20分；K>D 且 K≥80 超買 +5分
    - 價格站上 MA20：+20分；站上 MA5 但未過 MA20：+10分
    - MACD Histogram > 0（多頭動能）：+20分；MACD > Signal：+10分
    - 量能（最新量 > 20日均量 0.8x）：+15分
    """
    closes  = hist["closes"]
    highs   = hist["highs"]
    lows    = hist["lows"]
    volumes = hist["volumes"]
    price   = closes[-1]

    score   = 0
    signals = []

    # RSI
    rsi = calc_rsi(closes)
    if rsi is not None:
        if 40 <= rsi <= 60:
            score += 25; signals.append(f"RSI健康({rsi})")
        elif 30 <= rsi < 40 or 60 < rsi <= 70:
            score += 10; signals.append(f"RSI尚可({rsi})")
        elif rsi < 30:
            signals.append(f"RSI超賣({rsi})")
        else:
            signals.append(f"RSI超買({rsi})")

    # KD
    k, d = calc_kd(highs, lows, closes)
    if k is not None and d is not None:
        if k > d and k < 80:
            score += 20; signals.append(f"KD多頭(K{k}/D{d})")
        elif k > d and k >= 80:
            score += 5; signals.append(f"KD超買(K{k})")
        else:
            signals.append(f"KD空頭(K{k}/D{d})")

    # MA
    ma20 = calc_ma(closes, 20)
    ma5  = calc_ma(closes, 5)
    if ma20 and price > ma20:
        score += 20; signals.append(f"站上MA20({ma20:.1f})")
    elif ma5 and price > ma5:
        score += 10; signals.append("站上MA5")

    # MACD
    macd, sig, hist_val = calc_macd(closes)
    if macd is not None:
        if hist_val and hist_val > 0:
            score += 20; signals.append("MACD多頭動能")
        elif macd > sig:
            score += 10; signals.append("MACD翻多")

    # Volume
    avg_vol = calc_avg_volume(volumes)
    cur_vol = volumes[-1] if volumes[-1] else 0
    if avg_vol and cur_vol > avg_vol * 0.8:
        score += 15; signals.append(f"量能正常")
    elif avg_vol and cur_vol > avg_vol * 0.5:
        score += 5

    return score, signals, {
        "price": round(price, 2),
        "ma20":  ma20,
        "ma5":   ma5,
        "rsi":   rsi,
        "k":     k,
        "d":     d,
        "macd":  macd,
        "macd_signal": sig,
        "macd_hist":   hist_val,
        "avg_volume":  avg_vol,
        "cur_volume":  cur_vol,
    }

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"台股選股雷達 — {TODAY}")
    print(f"{'='*50}\n")

    # Stage 1
    raw = get_twse_fundamentals()
    if not raw:
        print("❌ 無法取得 TWSE 資料，結束。")
        sys.exit(1)

    candidates = fundamental_filter(raw)
    if not candidates:
        print("❌ 基本面初篩無通過，結束。")
        sys.exit(1)

    # Limit to max 80 stocks for speed (sort by lower PE first)
    candidates.sort(key=lambda x: x["pe"])
    candidates = candidates[:80]

    # Stage 2: technical scoring
    print(f"\n📈 計算技術指標（共 {len(candidates)} 支）...")
    results = []
    for i, s in enumerate(candidates):
        code = s["code"]
        print(f"  [{i+1:02d}/{len(candidates)}] {code} {s['name']}", end=" ", flush=True)
        hist = get_yahoo_history(code)
        if not hist:
            print("— 無歷史資料，略過")
            continue
        score, signals, tech = technical_score(hist)
        total_score = score  # pure technical score (0–100)
        # Fundamental bonus
        if s["pe"] < 15: total_score += 5
        if s["dividend_yield"] > 4: total_score += 5
        if s["pb"] < 1.5: total_score += 3

        results.append({
            **s,
            **tech,
            "score": min(total_score, 100),
            "signals": signals,
        })
        print(f"分數 {total_score}")
        time.sleep(0.3)  # avoid rate limit

    # Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)
    top30 = results[:30]

    # Write watchlist.json
    output = {
        "last_screened": TODAY,
        "criteria": {
            "fundamental": "0 < PE ≤ 30，0 < PB ≤ 5，殖利率 ≥ 1%",
            "technical": "RSI、KD、MA20、MACD 綜合評分",
            "universe": "TWSE 全上市股（排除 ETF）",
        },
        "total_screened": len(candidates),
        "candidates": top30,
    }

    with open("watchlist.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！前 10 名候選股：")
    for r in top30[:10]:
        print(f"  {r['code']} {r['name']:10s}  分數:{r['score']}  PE:{r['pe']}  殖利率:{r['dividend_yield']}%  信號:{', '.join(r['signals'][:2])}")

if __name__ == "__main__":
    main()
