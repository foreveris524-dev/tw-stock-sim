"""
台股日盤模擬當沖
每 15 分鐘由 GitHub Actions 觸發（台北 09:00-13:30）
  - 無持倉：RSI / KD / MACD 符合條件，以現價買入
  - 有持倉：達到 TP / SL 或反向技術信號則平倉（支援當沖）
價格來源：Yahoo Finance（日線計算指標 + 5 分線現價）
"""
import json, time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

TZ_TW = timezone(timedelta(hours=8))
NOW_TW = datetime.now(TZ_TW)
TODAY  = NOW_TW.strftime("%Y-%m-%d")
NOW_STR = NOW_TW.strftime("%H:%M")

MAX_POSITIONS = 5     # 最多同時持有幾支
TP_PCT = 0.05         # 停利 5%
SL_PCT = 0.03         # 停損 3%

DEFAULT_WATCHLIST = [
    ("0050",   "0050.TW",   "元大台灣50"),
    ("2330",   "2330.TW",   "台積電"),
    ("00981A", "00981A.TW", "統一主動台股增長"),
]

def load_watchlist():
    """從 watchlist.json 讀取選股雷達結果，取前 MAX_POSITIONS 名；檔案不存在則用預設清單"""
    try:
        with open("watchlist.json", encoding="utf-8") as f:
            wl = json.load(f)
        candidates = wl.get("candidates", [])
        if not candidates:
            raise ValueError("空清單")
        top = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:MAX_POSITIONS]
        result = []
        for s in top:
            code = s["code"]
            ticker = code + ".TW"
            result.append((code, ticker, s["name"]))
        print(f"選股雷達：使用前 {len(result)} 支（共 {len(candidates)} 支候選）")
        for r in result:
            print(f"  {r[0]} {r[2]}")
        return result
    except Exception:
        print("watchlist.json 不可用，使用預設標的")
        return DEFAULT_WATCHLIST

# ── 工具 ─────────────────────────────────────────────────────────────────────

def fetch(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DayBot/1.0)"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == 2:
                print(f"  fetch error: {e}")
                return None
            time.sleep(2 ** attempt)

def get_ohlc(ticker, interval="1d", range_="3mo"):
    data = fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                 f"?interval={interval}&range={range_}")
    if not data:
        return None
    try:
        result = data["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        rows = [(result["timestamp"][i], q["open"][i], q["high"][i],
                 q["low"][i], q["close"][i])
                for i in range(len(result["timestamp"]))
                if None not in (q["open"][i], q["high"][i],
                                q["low"][i], q["close"][i])]
        return {k: [r[j] for r in rows]
                for j, k in enumerate(("ts","opens","highs","lows","closes"))}
    except Exception:
        return None

def get_current_price(ticker):
    for interval, range_ in (("5m", "1d"), ("1d", "5d")):
        data = get_ohlc(ticker, interval=interval, range_=range_)
        if data and data["closes"]:
            return data["closes"][-1]
    return None

# ── 技術指標 ─────────────────────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    diffs  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(diffs)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    return round(100 - 100 / (1 + ag/al), 1) if al else 100.0

def calc_kd(highs, lows, closes, period=9, smooth=3):
    if len(closes) < period:
        return None, None
    k_vals, prev_k = [], 50.0
    for i in range(period-1, len(closes)):
        hmax = max(highs[i-period+1:i+1])
        lmin = min(lows[i-period+1:i+1])
        rsv  = (closes[i]-lmin)/(hmax-lmin)*100 if hmax != lmin else 50
        prev_k = (prev_k*(smooth-1)+rsv)/smooth
        k_vals.append(prev_k)
    d_vals, prev_d = [], 50.0
    for k in k_vals:
        prev_d = (prev_d*(smooth-1)+k)/smooth
        d_vals.append(prev_d)
    return round(k_vals[-1], 1), round(d_vals[-1], 1)

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    def ema_s(data, p):
        k, e = 2/(p+1), [data[0]]
        for v in data[1:]:
            e.append(v*k + e[-1]*(1-k))
        return e
    ef, es = ema_s(closes, fast), ema_s(closes, slow)
    ml  = [ef[slow-1+i]-es[slow-1+i] for i in range(len(closes)-slow+1)]
    sl_ = ema_s(ml, signal)
    return round(ml[-1], 3), round(sl_[-1], 3), round(ml[-1]-sl_[-1], 3)

# ── 開平倉條件 ───────────────────────────────────────────────────────────────

def should_buy(rsi, k, d, hist):
    if any(v is None for v in [rsi, k, d, hist]):
        return False, ""
    if k > d and k < 80 and 40 <= rsi <= 65 and hist > 0:
        return True, f"KD金叉(K{k}/D{d})，RSI={rsi}健康，MACD多頭"
    return False, ""

def should_sell(holding, price, rsi, k, d):
    entry = holding["entry_price"]
    tp    = holding["take_profit"]
    sl    = holding["stop_loss"]
    pct   = (price - entry) / entry * 100

    if price >= tp:
        return True, f"達停利 TP={tp:.2f}（+{pct:.1f}%）"
    if price <= sl:
        return True, f"達停損 SL={sl:.2f}（{pct:.1f}%）"
    if k and d and k < d and rsi and rsi > 75:
        return True, f"KD死叉且RSI超買({rsi})，平多"
    return False, ""

# ── 主邏輯 ───────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"台股日盤模擬  {TODAY}  {NOW_STR}")
    print(f"{'='*50}")

    with open("portfolio.json", encoding="utf-8") as f:
        pf = json.load(f)

    cash     = pf.get("cash", 0)
    holdings = {h["code"]: h for h in pf.get("holdings", [])}
    prices   = {}
    changed  = False

    watchlist = load_watchlist()
    print()

    for code, ticker, name in watchlist:
        print(f"\n── {code} {name} ──")

        hist = get_ohlc(ticker, interval="1d", range_="3mo")
        if not hist:
            print("  無法取得日線資料，略過")
            continue
        rsi = calc_rsi(hist["closes"])
        k, d = calc_kd(hist["highs"], hist["lows"], hist["closes"])
        _, _, hist_val = calc_macd(hist["closes"])

        price = get_current_price(ticker)
        if price is None:
            print("  無法取得現價，略過")
            continue
        prices[code] = price

        print(f"  現價={price:.2f}  RSI={rsi}  K={k}/D={d}  MACD_hist={hist_val}")

        holding = holdings.get(code)

        if holding:
            pnl_ntd = round((price - holding["entry_price"]) * holding["shares"])
            pnl_pct = (price - holding["entry_price"]) / holding["entry_price"] * 100
            print(f"  持倉中 進場={holding['entry_price']:.2f}  未實現={pnl_ntd:+,}元（{pnl_pct:+.1f}%）")

            close_now, reason = should_sell(holding, price, rsi, k, d)
            if close_now:
                sell_amount = round(price * holding["shares"])
                cash += sell_amount
                pf.setdefault("trade_log", []).append({
                    "date": TODAY, "time": NOW_STR,
                    "action": "SELL", "code": code, "name": name,
                    "price": round(price, 2), "shares": holding["shares"],
                    "amount": sell_amount, "pnl": pnl_ntd,
                    "session": "day", "reason": reason,
                })
                del holdings[code]
                changed = True
                print(f"  → 賣出 {code} @ {price:.2f}  損益={pnl_ntd:+,}元")
                print(f"    理由：{reason}")
            else:
                print(f"  → 繼續持倉")
        else:
            go_buy, reason = should_buy(rsi, k, d, hist_val)
            if go_buy and cash >= price and len(holdings) < MAX_POSITIONS:
                # 剩餘空位平均分配現金，最少買 1 股
                slots = max(MAX_POSITIONS - len(holdings), 1)
                budget = cash / slots
                shares = max(int(budget / price), 1)
                cost = round(price * shares)
                tp   = round(price * (1 + TP_PCT), 2)
                sl   = round(price * (1 - SL_PCT), 2)
                cash -= cost
                holdings[code] = {
                    "code": code, "name": name,
                    "shares": shares, "entry_price": round(price, 2),
                    "entry_date": TODAY, "entry_time": NOW_STR,
                    "cost": cost, "take_profit": tp, "stop_loss": sl,
                }
                pf.setdefault("trade_log", []).append({
                    "date": TODAY, "time": NOW_STR,
                    "action": "BUY", "code": code, "name": name,
                    "price": round(price, 2), "shares": shares,
                    "amount": cost, "pnl": 0,
                    "session": "day", "reason": reason,
                })
                changed = True
                print(f"  → 買入 {shares}股 @ {price:.2f}  TP={tp} SL={sl}")
                print(f"    理由：{reason}")
            elif not go_buy:
                print(f"  → 條件未觸發，觀望")
            elif len(holdings) >= MAX_POSITIONS:
                print(f"  → 已達最大持倉數 {MAX_POSITIONS}，略過")
            else:
                print(f"  → 現金不足（現金={cash:.0f} < 現價={price:.2f}），略過")

        time.sleep(0.5)

    if changed:
        stock_value = sum(
            round(prices.get(h["code"], h["entry_price"]) * h["shares"])
            for h in holdings.values()
        )
        pf["cash"]         = round(cash)
        pf["holdings"]     = list(holdings.values())
        pf["total_value"]  = round(cash + stock_value)
        pf["last_updated"] = f"{TODAY} {NOW_STR}"

        with open("portfolio.json", "w", encoding="utf-8") as f:
            json.dump(pf, f, ensure_ascii=False, indent=2)
        print("\n✅ portfolio.json 已更新")
    else:
        print("\n→ 無操作，portfolio.json 未異動")

if __name__ == "__main__":
    main()
