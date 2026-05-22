"""
台指期模擬交易（日盤 + 夜盤共用）
每 15 分鐘由 GitHub Actions 觸發：
  - 無持倉且無掛單：RSI/KD/MACD 符合條件 → 掛限價單
  - 有掛單：用 5 分鐘 K 線判斷是否成交；逾時 90 分自動取消
  - 有持倉：達到 TP/SL 或反向技術信號則平倉（支援當沖）
  - 做多/做空均可，視訊號而定
"""
import json, time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

TZ_TW = timezone(timedelta(hours=8))
NOW_TW  = datetime.now(TZ_TW)
TODAY   = NOW_TW.strftime("%Y-%m-%d")
NOW_STR = NOW_TW.strftime("%H:%M")
NOW_TS  = int(NOW_TW.timestamp())
SESSION = "day" if 9 <= NOW_TW.hour < 14 else "night"

POINT_VALUE    = 50      # 小台指每點 50 元
MARGIN_REQ     = 48_000  # 保證金
TP_POINTS      = 600     # 停利點數
SL_POINTS      = 600     # 停損點數
LIMIT_DISCOUNT = 0.002   # 限價折讓 0.2%
ORDER_TIMEOUT  = 90      # 掛單逾時分鐘

# ── 工具 ─────────────────────────────────────────────────────────────────────

def fetch(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FuturesBot/1.0)"})
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
    for ticker in ("TW=F", "TXFB5.TWO"):
        data = get_ohlc(ticker, interval="5m", range_="1d")
        if data and data["closes"]:
            p = data["closes"][-1]
            print(f"  現價({ticker}): {p:.0f}")
            return p
    data = get_ohlc("^TWII", interval="1d", range_="5d")
    if data and data["closes"]:
        p = data["closes"][-1]
        print(f"  fallback ^TWII: {p:.0f}")
        return p
    return None

def get_intraday_ohlc():
    """取今日 5 分鐘 OHLC（用於判斷限價單是否觸及）"""
    return get_ohlc("TW=F", interval="5m", range_="1d") or \
           get_ohlc("^TWII", interval="5m", range_="1d")

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

def should_open_long(rsi, k, d, hist):
    if any(v is None for v in [rsi, k, d, hist]):
        return False, ""
    if k > d and k < 80 and 40 <= rsi <= 65 and hist > 0:
        return True, f"KD金叉(K{k}/D{d})，RSI={rsi}健康，MACD多頭"
    return False, ""

def should_open_short(rsi, k, d, hist):
    if any(v is None for v in [rsi, k, d, hist]):
        return False, ""
    if k < d and k > 20 and 35 <= rsi <= 60 and hist < 0:
        return True, f"KD死叉(K{k}/D{d})，RSI={rsi}健康，MACD空頭"
    return False, ""

def should_close(position, price, rsi, k, d):
    direction = position["direction"]
    entry     = position["entry_price"]
    tp        = position["take_profit"]
    sl        = position["stop_loss"]
    if direction == "LONG":
        if price >= tp:
            return True, f"達停利 TP={tp}（+{price-entry:.0f}pt）"
        if price <= sl:
            return True, f"達停損 SL={sl}（{price-entry:.0f}pt）"
        if k and d and k < d and rsi and rsi > 75:
            return True, f"KD死叉且RSI超買({rsi})，平多"
    else:
        if price <= tp:
            return True, f"達停利 TP={tp}（+{entry-price:.0f}pt）"
        if price >= sl:
            return True, f"達停損 SL={sl}（{entry-price:.0f}pt）"
        if k and d and k > d and rsi and rsi < 25:
            return True, f"KD金叉且RSI超賣({rsi})，平空"
    return False, ""

# ── 限價單成交判斷 ────────────────────────────────────────────────────────────

def check_limit_filled(direction, limit_price, placed_ts):
    """用 5 分 K 判斷掛單後是否有任何 K 棒觸及限價"""
    data = get_intraday_ohlc()
    if not data:
        return False
    for ts, low, high in zip(data["ts"], data["lows"], data["highs"]):
        if ts < placed_ts:
            continue
        if direction == "LONG" and low <= limit_price:
            return True
        if direction == "SHORT" and high >= limit_price:
            return True
    return False

# ── 主邏輯 ───────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"台指期模擬 [{SESSION}]  {TODAY}  {NOW_STR}")
    print(f"{'='*50}")

    print("載入日線資料（指標用）...")
    hist = get_ohlc("^TWII", interval="1d", range_="3mo")
    if not hist:
        print("無法取得日線資料，跳過")
        return

    rsi  = calc_rsi(hist["closes"])
    k, d = calc_kd(hist["highs"], hist["lows"], hist["closes"])
    _, _, hist_val = calc_macd(hist["closes"])

    print("取得現價...")
    price = get_current_price()
    if price is None:
        print("無法取得現價，跳過")
        return

    print(f"\n現價={price:.0f}  RSI={rsi}  K={k}/D={d}  MACD_hist={hist_val}")

    with open("futures_portfolio.json", encoding="utf-8") as f:
        fp = json.load(f)

    position       = fp.get("position")
    pending_orders = fp.get("pending_orders", [])

    log = {
        "date": TODAY, "time": NOW_STR, "session": SESSION,
        "current_price": round(price),
        "rsi": rsi, "k": k, "d": d, "macd_hist": hist_val,
    }

    # ── 第一步：處理待成交限價單 ──────────────────────────────────────────────
    remaining = []
    for order in pending_orders:
        direction   = order["direction"]
        limit_price = order["limit_price"]
        placed_ts   = order["placed_ts"]
        elapsed     = (NOW_TS - placed_ts) // 60

        if elapsed >= ORDER_TIMEOUT:
            fp["available_margin"] = fp.get("available_margin", 200000) + MARGIN_REQ
            fp["used_margin"]      = max(fp.get("used_margin", 0) - MARGIN_REQ, 0)
            fp.setdefault("trade_log", []).append({
                "date": TODAY, "time": NOW_STR, "session": SESSION,
                "action": "ORDER_CANCEL", "direction": direction,
                "current_price": round(price), "rsi": rsi, "k": k, "d": d, "macd_hist": hist_val,
                "contracts": 1, "entry_price": limit_price, "pnl": 0,
                "reason": f"限價單逾時取消（{elapsed}分未成交）",
            })
            print(f"  限價單 {direction} @ {limit_price} 逾時取消")
            continue

        filled = check_limit_filled(direction, limit_price, placed_ts)
        if filled:
            tp = limit_price + TP_POINTS if direction == "LONG" else limit_price - TP_POINTS
            sl = limit_price - SL_POINTS if direction == "LONG" else limit_price + SL_POINTS
            position = {
                "direction": direction, "contracts": 1,
                "entry_price": limit_price,
                "entry_date": TODAY, "entry_time": NOW_STR,
                "take_profit": tp, "stop_loss": sl,
                "margin_required": MARGIN_REQ,
            }
            fp["position"] = position
            fp.setdefault("trade_log", []).append({
                "date": TODAY, "time": NOW_STR, "session": SESSION,
                "action": f"OPEN_{direction}", "direction": direction,
                "current_price": round(price), "rsi": rsi, "k": k, "d": d, "macd_hist": hist_val,
                "contracts": 1, "entry_price": limit_price, "pnl": 0,
                "reason": f"限價單成交（{direction} @ {limit_price}，5分K觸及）",
            })
            print(f"  → 限價單成交！{direction} @ {limit_price}  TP={tp} SL={sl}")
        else:
            remaining.append(order)
            print(f"  {direction} 限價 {limit_price} 尚未成交（{elapsed}分）")

    pending_orders = remaining
    fp["pending_orders"] = pending_orders

    # ── 第二步：持倉管理 ──────────────────────────────────────────────────────
    position = fp.get("position")
    if position:
        direction   = position["direction"]
        entry_price = position["entry_price"]
        contracts   = position.get("contracts", 1)
        pnl_pts = (price - entry_price) if direction == "LONG" else (entry_price - price)
        pnl_ntd = round(pnl_pts * POINT_VALUE * contracts)
        position["unrealized_pnl"] = pnl_ntd
        fp["position"] = position

        close_now, close_reason = should_close(position, price, rsi, k, d)
        if close_now:
            fp["cumulative_pnl"]   = fp.get("cumulative_pnl", 0) + pnl_ntd
            fp["available_margin"] = fp.get("available_margin", 0) + MARGIN_REQ
            fp["used_margin"]      = 0
            fp["position"]         = None
            log.update({
                "action": "CLOSE", "direction": direction,
                "contracts": contracts, "entry_price": entry_price,
                "exit_price": round(price), "pnl": pnl_ntd,
                "reason": close_reason,
            })
            fp.setdefault("trade_log", []).append(log)
            print(f"\n→ 平倉 {direction} @ {round(price)}  損益={pnl_ntd:+,}元")
            print(f"  理由：{close_reason}")
        else:
            log.update({
                "action": "HOLD", "direction": direction,
                "contracts": contracts, "pnl": pnl_ntd,
                "reason": f"持倉中 未實現={pnl_ntd:+,}元，維持",
            })
            fp.setdefault("trade_log", []).append(log)
            print(f"\n→ 持倉 {direction} @ {entry_price}  現={price:.0f}  未實現={pnl_ntd:+,}元")

    # ── 第三步：尋找新進場（無持倉且無掛單） ──────────────────────────────────
    elif not pending_orders:
        go_long,  long_reason  = should_open_long(rsi, k, d, hist_val)
        go_short, short_reason = should_open_short(rsi, k, d, hist_val)

        if go_long or go_short:
            direction   = "LONG" if go_long else "SHORT"
            reason      = long_reason if go_long else short_reason
            # 做多：限價低於現價；做空：限價高於現價
            if direction == "LONG":
                limit_price = round(price * (1 - LIMIT_DISCOUNT))
            else:
                limit_price = round(price * (1 + LIMIT_DISCOUNT))

            pending_orders.append({
                "direction":   direction,
                "limit_price": limit_price,
                "placed_ts":   NOW_TS,
                "placed_time": NOW_STR,
            })
            fp["pending_orders"]   = pending_orders
            fp["available_margin"] = fp.get("available_margin", 200000) - MARGIN_REQ
            fp["used_margin"]      = fp.get("used_margin", 0) + MARGIN_REQ
            log.update({
                "action": f"ORDER_PLACE", "direction": direction,
                "contracts": 1, "entry_price": limit_price, "pnl": 0,
                "reason": f"掛限價單 {direction} @ {limit_price}（現價{price:.0f}）｜{reason}",
            })
            fp.setdefault("trade_log", []).append(log)
            print(f"\n→ 掛限價單 {direction} @ {limit_price}（現價 {price:.0f}）")
            print(f"  理由：{reason}")
        else:
            log.update({
                "action": "OBSERVE", "direction": "NONE",
                "contracts": 0, "pnl": 0,
                "reason": "條件未觸發，觀望",
            })
            fp.setdefault("trade_log", []).append(log)
            print("\n→ 觀望，條件未觸發")
    else:
        log.update({
            "action": "OBSERVE", "direction": "NONE",
            "contracts": 0, "pnl": 0,
            "reason": "有掛單待成交，等待中",
        })
        fp.setdefault("trade_log", []).append(log)
        print("\n→ 有掛單待成交，等待中")

    fp["last_updated"] = f"{TODAY} {NOW_STR}"
    with open("futures_portfolio.json", "w", encoding="utf-8") as f:
        json.dump(fp, f, ensure_ascii=False, indent=2)
    print("\n✅ futures_portfolio.json 已更新")

if __name__ == "__main__":
    main()
