"""
台指期夜盤模擬交易
每 15 分鐘由 GitHub Actions 觸發，與日盤邏輯相同：
  - 無持倉：RSI / KD / MACD 綜合判斷，符合條件開倉
  - 有持倉：達到 TP / SL 或反向技術信號則平倉
價格來源：Yahoo Finance ^TWII (日線 indicators) + TW=F (夜盤近即時報價)
"""
import json, time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

TZ_TW = timezone(timedelta(hours=8))
NOW_TW = datetime.now(TZ_TW)
TODAY = NOW_TW.strftime("%Y-%m-%d")
NOW_STR = NOW_TW.strftime("%H:%M")
POINT_VALUE = 50  # 小台指每點 50 元

# ── 工具 ─────────────────────────────────────────────────────────────────────

def fetch(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; NightBot/1.0)"})
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

def get_current_price():
    """嘗試抓夜盤期貨現價（TW=F），失敗則用日線最後收盤"""
    # 先試夜盤期貨
    for ticker in ("TW=F", "TXFB5.TWO"):
        data = get_ohlc(ticker, interval="5m", range_="1d")
        if data and data["closes"]:
            price = data["closes"][-1]
            print(f"  夜盤現價({ticker}): {price:.0f}")
            return price
    # fallback：日線最後收盤
    data = get_ohlc("^TWII", interval="1d", range_="5d")
    if data and data["closes"]:
        price = data["closes"][-1]
        print(f"  fallback ^TWII 收盤: {price:.0f}")
        return price
    return None

# ── 技術指標 ─────────────────────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in diffs]
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
        rsv = (closes[i]-lmin)/(hmax-lmin)*100 if hmax != lmin else 50
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
    ml = [ef[slow-1+i]-es[slow-1+i] for i in range(len(closes)-slow+1)]
    sl_ = ema_s(ml, signal)
    return round(ml[-1], 3), round(sl_[-1], 3), round(ml[-1]-sl_[-1], 3)

# ── 主邏輯 ───────────────────────────────────────────────────────────────────

def should_open_long(rsi, k, d, hist):
    """做多條件：KD 金叉且 K<80 + RSI 40-65 + MACD 翻多"""
    if k is None or d is None or rsi is None or hist is None:
        return False, ""
    if k > d and k < 80 and 40 <= rsi <= 65 and hist > 0:
        return True, f"KD金叉(K{k}/D{d})，RSI={rsi}健康，MACD多頭"
    return False, ""

def should_open_short(rsi, k, d, hist):
    """做空條件：KD 死叉且 K>20 + RSI 35-60 + MACD 翻空"""
    if k is None or d is None or rsi is None or hist is None:
        return False, ""
    if k < d and k > 20 and 35 <= rsi <= 60 and hist < 0:
        return True, f"KD死叉(K{k}/D{d})，RSI={rsi}健康，MACD空頭"
    return False, ""

def should_close(position, price, rsi, k, d):
    """平倉條件：TP/SL 或反向技術信號"""
    direction = position["direction"]
    entry     = position["entry_price"]
    tp        = position["take_profit"]
    sl        = position["stop_loss"]

    if direction == "LONG":
        pnl_pts = price - entry
        if price >= tp:
            return True, f"達停利 TP={tp}（+{price-entry:.0f}pt）"
        if price <= sl:
            return True, f"達停損 SL={sl}（{price-entry:.0f}pt）"
        if k and d and k < d and rsi and rsi > 75:
            return True, f"KD死叉且RSI超買({rsi})，平多"
    else:
        pnl_pts = entry - price
        if price <= tp:
            return True, f"達停利 TP={tp}（+{entry-price:.0f}pt）"
        if price >= sl:
            return True, f"達停損 SL={sl}（{entry-price:.0f}pt）"
        if k and d and k > d and rsi and rsi < 25:
            return True, f"KD金叉且RSI超賣({rsi})，平空"

    return False, ""

def main():
    print(f"\n{'='*50}")
    print(f"台指期夜盤模擬  {TODAY}  {NOW_STR}")
    print(f"{'='*50}")

    # 取得日線資料（計算技術指標）
    print("載入日線資料（指標用）...")
    hist = get_ohlc("^TWII", interval="1d", range_="3mo")
    if not hist:
        print("無法取得日線資料，跳過本次")
        return

    closes = hist["closes"]
    highs  = hist["highs"]
    lows   = hist["lows"]

    rsi  = calc_rsi(closes)
    k, d = calc_kd(highs, lows, closes)
    macd, sig, hist_val = calc_macd(closes)

    # 取得夜盤現價
    print("取得夜盤現價...")
    price = get_current_price()
    if price is None:
        print("無法取得現價，跳過")
        return

    print(f"\n現價={price:.0f}  RSI={rsi}  K={k}/D={d}  MACD_hist={hist_val}")

    # 讀取期貨帳戶
    with open("futures_portfolio.json", encoding="utf-8") as f:
        fp = json.load(f)

    position = fp.get("position")

    log = {
        "date":          TODAY,
        "time":          NOW_STR,
        "session":       "night",
        "current_price": round(price),
        "rsi": rsi, "k": k, "d": d, "macd_hist": hist_val,
    }

    if position is None:
        # ── 尋找進場機會 ─────────────────────────────────────────────────
        go_long,  long_reason  = should_open_long(rsi, k, d, hist_val)
        go_short, short_reason = should_open_short(rsi, k, d, hist_val)

        if go_long or go_short:
            direction    = "LONG" if go_long else "SHORT"
            reason       = long_reason if go_long else short_reason
            entry_price  = round(price)
            margin_req   = 48000
            tp = entry_price + 600 if direction == "LONG" else entry_price - 600
            sl = entry_price - 600 if direction == "LONG" else entry_price + 600

            fp["position"] = {
                "direction":       direction,
                "contracts":       1,
                "entry_price":     entry_price,
                "entry_date":      TODAY,
                "entry_time":      NOW_STR,
                "take_profit":     tp,
                "stop_loss":       sl,
                "margin_required": margin_req,
            }
            fp["used_margin"]      = margin_req
            fp["available_margin"] = fp.get("available_margin", 200000) - margin_req
            log.update({
                "action": f"OPEN_{direction}", "direction": direction,
                "contracts": 1, "entry_price": entry_price, "pnl": 0,
                "reason": reason,
            })
            print(f"\n→ 開倉 {direction} @ {entry_price}  TP={tp} SL={sl}")
            print(f"  理由：{reason}")
        else:
            log.update({
                "action": "OBSERVE", "direction": "NONE",
                "contracts": 0, "pnl": 0,
                "reason": "條件未觸發，觀望",
            })
            print("\n→ 觀望，條件未觸發")

    else:
        # ── 持倉管理 ──────────────────────────────────────────────────────
        direction   = position["direction"]
        entry_price = position["entry_price"]
        contracts   = position.get("contracts", 1)

        pnl_pts = (price - entry_price) if direction == "LONG" else (entry_price - price)
        pnl_ntd = round(pnl_pts * POINT_VALUE * contracts)
        position["unrealized_pnl"] = pnl_ntd
        fp["position"] = position

        close_now, close_reason = should_close(position, price, rsi, k, d)

        if close_now:
            fp["cumulative_pnl"]  = fp.get("cumulative_pnl", 0) + pnl_ntd
            fp["available_margin"] = fp.get("available_margin", 0) + position.get("margin_required", 48000)
            fp["used_margin"]     = 0
            fp["position"]        = None
            log.update({
                "action": "CLOSE", "direction": direction,
                "contracts": contracts, "entry_price": entry_price,
                "exit_price": round(price), "pnl": pnl_ntd,
                "reason": close_reason,
            })
            print(f"\n→ 平倉 {direction} @ {round(price)}  損益={pnl_ntd:+,}元")
            print(f"  理由：{close_reason}")
        else:
            log.update({
                "action": "HOLD", "direction": direction,
                "contracts": contracts, "pnl": pnl_ntd,
                "reason": f"持倉中 未實現={pnl_ntd:+,}元，維持",
            })
            print(f"\n→ 持倉 {direction} @ {entry_price}  現={price:.0f}  未實現={pnl_ntd:+,}元")

    fp.setdefault("trade_log", []).append(log)
    fp["last_updated"] = f"{TODAY} {NOW_STR}"

    with open("futures_portfolio.json", "w", encoding="utf-8") as f:
        json.dump(fp, f, ensure_ascii=False, indent=2)

    print("\n✅ futures_portfolio.json 已更新")

if __name__ == "__main__":
    main()
