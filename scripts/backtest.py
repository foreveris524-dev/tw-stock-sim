"""
回測腳本 — 以目前 watchlist 前 20 支股票，
模擬過去三個月（2026-02-27 ~ 2026-05-27）的買賣邏輯

買入條件（同 day_trade.py should_buy）:
  K > D, K < 80, 40 <= RSI <= 65, MACD_hist > 0
  → 掛限價單 = 當日收盤 * 0.99，次日最低 <= 限價則成交

賣出條件（同 day_trade.py should_sell）:
  TP = +5%, SL = -3%
  或 K < D 且 RSI > 75 → 以當日收盤平倉

初始資金 1,000,000 NTD，最多同時 10 個部位
每支股票每次買進：可用資金 / 剩餘槽位
"""
import json, time, sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS   = 10
LIMIT_DISCOUNT  = 0.01
TP_PCT          = 0.05
SL_PCT          = 0.03

# 回測區間：過去三個月
TZ_TW      = timezone(timedelta(hours=8))
TODAY_TW   = datetime.now(TZ_TW).date()
START_DATE = (TODAY_TW - timedelta(days=91)).strftime("%Y-%m-%d")
END_DATE   = TODAY_TW.strftime("%Y-%m-%d")


# ── 從 watchlist.json 讀取前 20 支標的 ────────────────────────────────────────
def load_watchlist():
    with open("watchlist.json", encoding="utf-8") as f:
        wl = json.load(f)
    candidates = wl.get("candidates", [])
    top = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:MAX_POSITIONS * 2]
    return [(s["code"], s["code"] + ".TW", s["name"]) for s in top]


# ── Yahoo Finance 日線資料 ─────────────────────────────────────────────────────
def fetch_ohlc(ticker, range_="6mo"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&range={range_}")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BacktestBot/1.0)"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            result = data["chart"]["result"][0]
            q  = result["indicators"]["quote"][0]
            ts = result["timestamp"]
            rows = []
            for i in range(len(ts)):
                if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
                    continue
                dt = datetime.fromtimestamp(ts[i], tz=TZ_TW).strftime("%Y-%m-%d")
                rows.append({
                    "date":  dt,
                    "open":  q["open"][i],
                    "high":  q["high"][i],
                    "low":   q["low"][i],
                    "close": q["close"][i],
                })
            return rows
        except Exception as e:
            if attempt == 2:
                return None
            time.sleep(2 ** attempt)


# ── 技術指標（同 day_trade.py） ───────────────────────────────────────────────
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
    return 100 - 100 / (1 + ag/al) if al else 100.0

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
    for kv in k_vals:
        prev_d = (prev_d*(smooth-1)+kv)/smooth
        d_vals.append(prev_d)
    return k_vals[-1], d_vals[-1]

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    def ema(data, p):
        k, e = 2/(p+1), [data[0]]
        for v in data[1:]:
            e.append(v*k + e[-1]*(1-k))
        return e
    ef, es = ema(closes, fast), ema(closes, slow)
    ml  = [ef[slow-1+i]-es[slow-1+i] for i in range(len(closes)-slow+1)]
    sl_ = ema(ml, signal)
    return ml[-1] - sl_[-1]

def calc_ma(closes, period=20):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def should_buy(rsi, k, d, hist, price, ma20, ma20_prev):
    """ma20_prev: 5 天前的 MA20，用來判斷均線是否上揚"""
    if any(v is None for v in [rsi, k, d, hist, price, ma20, ma20_prev]):
        return False
    if ma20 <= ma20_prev:   # MA20 必須向上（過濾均線下彎的股票）
        return False
    return k > d and k < 80 and 40 <= rsi <= 65 and hist > 0

def should_sell(entry, price, rsi, k, d):
    tp = entry * (1 + TP_PCT)
    sl = entry * (1 - SL_PCT)
    if price >= tp:
        return True, f"TP +5%"
    if price <= sl:
        return True, f"SL -3%"
    if k and d and rsi and k < d and rsi > 75:
        return True, f"KD死叉/RSI超買"
    return False, ""


# ── 主回測 ───────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  回測區間：{START_DATE} → {END_DATE}")
    print(f"  初始資金：{INITIAL_CAPITAL:,} NTD")
    print(f"  TP={TP_PCT*100:.0f}%  SL={SL_PCT*100:.0f}%  限價=-{LIMIT_DISCOUNT*100:.0f}%  最多{MAX_POSITIONS}部位")
    print(f"{'='*60}")

    watchlist = load_watchlist()
    print(f"\n標的清單（前 {len(watchlist)} 支）：")
    for code, _, name in watchlist:
        print(f"  {code} {name}")

    # 抓所有標的的 6 個月日線資料
    print(f"\n抓取歷史資料中...", flush=True)
    price_data = {}
    for code, ticker, name in watchlist:
        rows = fetch_ohlc(ticker, range_="6mo")
        if rows:
            price_data[code] = {"name": name, "rows": rows}
            print(f"  {code} {name}: {len(rows)} 天")
        else:
            print(f"  {code} {name}: 無資料，略過")
        time.sleep(0.3)

    # 取得所有在回測區間內的交易日清單（聯集）
    all_dates = sorted({
        row["date"]
        for info in price_data.values()
        for row in info["rows"]
        if START_DATE <= row["date"] <= END_DATE
    })

    print(f"\n回測交易日：{all_dates[0]} ~ {all_dates[-1]}，共 {len(all_dates)} 天")

    # 初始化投資組合
    cash        = float(INITIAL_CAPITAL)
    holdings    = {}    # code -> {entry_price, shares, entry_date, ...}
    pending     = {}    # code -> {limit_price, shares, reserved, placed_date}
    sl_cooldown = {}    # code -> {sl_dates: [...], cooldown_until: date_str}
    trade_log   = []
    daily_value = []

    def get_row(code, date):
        for r in price_data[code]["rows"]:
            if r["date"] == date:
                return r
        return None

    def get_history_up_to(code, date, n=60):
        rows = [r for r in price_data[code]["rows"] if r["date"] <= date]
        return rows[-n:]

    for date in all_dates:
        # ── 1. 處理昨日掛單：用今日 low 判斷是否成交 ───────────────────────
        filled_codes = []
        for code, order in list(pending.items()):
            if order["placed_date"] >= date:
                continue  # 同日掛單，今日才開始判斷
            row = get_row(code, date)
            if not row:
                continue
            if row["low"] <= order["limit_price"]:
                lp     = order["limit_price"]
                shares = order["shares"]
                cost   = lp * shares
                cash  += order["reserved"] - cost
                tp     = lp * (1 + TP_PCT)
                sl     = lp * (1 - SL_PCT)
                holdings[code] = {
                    "entry_price": lp, "shares": shares,
                    "entry_date": date, "tp": tp, "sl": sl,
                    "name": price_data[code]["name"],
                }
                trade_log.append({
                    "date": date, "action": "BUY", "code": code,
                    "name": price_data[code]["name"],
                    "price": round(lp, 2), "shares": shares,
                    "pnl": 0,
                })
                filled_codes.append(code)
            # 跨日仍未成交 → 視為取消（限價單當日有效）
            elif order["placed_date"] < date:
                cash += order["reserved"]
                filled_codes.append(code)
        for code in filled_codes:
            del pending[code]

        # ── 2. 管理現有持倉 ─────────────────────────────────────────────────
        for code in list(holdings.keys()):
            row = get_row(code, date)
            if not row:
                continue
            price  = row["close"]
            hist   = get_history_up_to(code, date)
            closes = [r["close"] for r in hist]
            highs  = [r["high"]  for r in hist]
            lows   = [r["low"]   for r in hist]
            rsi  = calc_rsi(closes)
            k, d = calc_kd(highs, lows, closes)
            h    = holdings[code]

            # 先檢查日內是否觸及 SL（用 low）
            if row["low"] <= h["sl"]:
                exit_price = h["sl"]
                pnl = round((exit_price - h["entry_price"]) * h["shares"])
                cash += exit_price * h["shares"]
                trade_log.append({
                    "date": date, "action": "SELL", "code": code,
                    "name": h["name"], "price": round(exit_price, 2),
                    "shares": h["shares"], "pnl": pnl, "reason": "SL -3%",
                })
                del holdings[code]
                # 停損冷靜期計數
                cd = sl_cooldown.setdefault(code, {"sl_dates": [], "cooldown_until": None})
                cd["sl_dates"].append(date)
                cutoff = (datetime.strptime(date, "%Y-%m-%d") - timedelta(weeks=8)).strftime("%Y-%m-%d")
                cd["sl_dates"] = [d for d in cd["sl_dates"] if d >= cutoff]
                if len(cd["sl_dates"]) >= 2:
                    from datetime import datetime as dt2
                    cd["cooldown_until"] = (dt2.strptime(date, "%Y-%m-%d") + timedelta(weeks=4)).strftime("%Y-%m-%d")
                continue

            # 檢查日內是否觸及 TP（用 high）
            if row["high"] >= h["tp"]:
                exit_price = h["tp"]
                pnl = round((exit_price - h["entry_price"]) * h["shares"])
                cash += exit_price * h["shares"]
                trade_log.append({
                    "date": date, "action": "SELL", "code": code,
                    "name": h["name"], "price": round(exit_price, 2),
                    "shares": h["shares"], "pnl": pnl, "reason": "TP +5%",
                })
                del holdings[code]
                continue

            # 收盤技術面賣出
            close_now, reason = should_sell(h["entry_price"], price, rsi, k, d)
            if close_now:
                pnl = round((price - h["entry_price"]) * h["shares"])
                cash += price * h["shares"]
                trade_log.append({
                    "date": date, "action": "SELL", "code": code,
                    "name": h["name"], "price": round(price, 2),
                    "shares": h["shares"], "pnl": pnl, "reason": reason,
                })
                del holdings[code]

        # ── 3. 尋找新進場機會 ───────────────────────────────────────────────
        active = len(holdings) + len(pending)
        for code, ticker, name in watchlist:
            if active >= MAX_POSITIONS:
                break
            if code in holdings or code in pending:
                continue
            row = get_row(code, date)
            if not row:
                continue
            hist   = get_history_up_to(code, date)
            closes = [r["close"] for r in hist]
            highs  = [r["high"]  for r in hist]
            lows   = [r["low"]   for r in hist]
            rsi      = calc_rsi(closes)
            k, d     = calc_kd(highs, lows, closes)
            macd_h   = calc_macd(closes)
            ma20     = calc_ma(closes, 20)
            ma20_prev = calc_ma(closes[:-5], 20) if len(closes) >= 25 else None
            price    = row["close"]

            # 冷靜期檢查
            cd = sl_cooldown.get(code, {})
            cu = cd.get("cooldown_until")
            if cu and cu >= date:
                continue
            elif cu and cu < date:
                sl_cooldown[code] = {"sl_dates": [], "cooldown_until": None}

            if should_buy(rsi, k, d, macd_h, price, ma20, ma20_prev):
                lp     = round(price * (1 - LIMIT_DISCOUNT), 2)
                slots  = max(MAX_POSITIONS - active, 1)
                budget = cash / slots
                shares = max(int(budget / lp), 1)
                reserved = lp * shares
                if cash >= reserved:
                    cash    -= reserved
                    pending[code] = {
                        "limit_price":  lp,
                        "shares":       shares,
                        "reserved":     reserved,
                        "placed_date":  date,
                    }
                    trade_log.append({
                        "date": date, "action": "ORDER", "code": code,
                        "name": name, "price": round(lp, 2), "shares": shares,
                        "pnl": 0,
                    })
                    active += 1

        # ── 4. 記錄每日總值 ──────────────────────────────────────────────────
        stock_val = sum(
            (get_row(c, date) or {"close": h["entry_price"]})["close"] * h["shares"]
            for c, h in holdings.items()
        )
        reserved_val = sum(o["reserved"] for o in pending.values())
        total = cash + stock_val + reserved_val
        daily_value.append({"date": date, "total": round(total)})

    # ── 統計結果 ─────────────────────────────────────────────────────────────
    # 結束時將持倉以最後一天收盤賣出
    last_date = all_dates[-1]
    for code, h in list(holdings.items()):
        row = get_row(code, last_date)
        if row:
            price = row["close"]
            pnl   = round((price - h["entry_price"]) * h["shares"])
            cash += price * h["shares"]
            trade_log.append({
                "date": last_date, "action": "CLOSE", "code": code,
                "name": h["name"], "price": round(price, 2),
                "shares": h["shares"], "pnl": pnl, "reason": "回測結束強制平倉",
            })
    # 退回掛單凍結資金
    for code, o in pending.items():
        cash += o["reserved"]

    final_value = round(cash)
    total_pnl   = final_value - INITIAL_CAPITAL
    pnl_pct     = total_pnl / INITIAL_CAPITAL * 100

    buys  = [t for t in trade_log if t["action"] in ("BUY",)]
    sells = [t for t in trade_log if t["action"] in ("SELL", "CLOSE")]
    win   = [t for t in sells if t["pnl"] > 0]
    loss  = [t for t in sells if t["pnl"] <= 0]

    print(f"\n{'='*60}")
    print(f"  ✅ 回測結束")
    print(f"  初始資金：{INITIAL_CAPITAL:>12,} NTD")
    print(f"  最終資金：{final_value:>12,} NTD")
    print(f"  累積損益：{total_pnl:>+12,} NTD  ({pnl_pct:+.2f}%)")
    print(f"  成交筆數：{len(buys)} 買進 / {len(sells)} 賣出")
    win_rate = len(win)/len(sells)*100 if sells else 0
    print(f"  勝率：{win_rate:.1f}%（獲利 {len(win)} 次 / 虧損 {len(loss)} 次）")
    if win:
        avg_win = sum(t["pnl"] for t in win) / len(win)
        print(f"  平均獲利：{avg_win:>+,.0f} NTD")
    if loss:
        avg_loss = sum(t["pnl"] for t in loss) / len(loss)
        print(f"  平均虧損：{avg_loss:>+,.0f} NTD")
    print(f"{'='*60}")

    print(f"\n── 各股交易明細 ──")
    by_code = {}
    for t in trade_log:
        if t["action"] in ("SELL", "CLOSE"):
            by_code.setdefault(t["code"], {"name": t["name"], "trades": [], "pnl": 0})
            by_code[t["code"]]["trades"].append(t)
            by_code[t["code"]]["pnl"] += t["pnl"]

    for code, info in sorted(by_code.items(), key=lambda x: x[1]["pnl"], reverse=True):
        tag = "🟢" if info["pnl"] > 0 else "🔴"
        print(f"  {tag} {code} {info['name']:10s}  {len(info['trades'])} 筆  損益合計 {info['pnl']:>+,} NTD")
        for t in info["trades"]:
            print(f"       {t['date']}  賣 {t['shares']:>5} 股 @ {t['price']:>8.2f}  損益 {t['pnl']:>+,} NTD  ({t.get('reason','')})")

    # 每月損益摘要
    print(f"\n── 月度損益 ──")
    monthly = {}
    for t in trade_log:
        if t["action"] in ("SELL", "CLOSE"):
            ym = t["date"][:7]
            monthly[ym] = monthly.get(ym, 0) + t["pnl"]
    for ym in sorted(monthly):
        bar = "▓" * int(abs(monthly[ym]) / 5000)
        sign = "+" if monthly[ym] >= 0 else ""
        print(f"  {ym}  {sign}{monthly[ym]:>+8,} NTD  {bar}")

    # 寫入結果 JSON
    result = {
        "run_date": TODAY_TW.strftime("%Y-%m-%d"),
        "start_date": START_DATE,
        "end_date": END_DATE,
        "initial_capital": INITIAL_CAPITAL,
        "final_value": final_value,
        "total_pnl": total_pnl,
        "pnl_pct": round(pnl_pct, 2),
        "trade_count": len(sells),
        "win_rate": round(win_rate, 1),
        "trade_log": trade_log,
        "daily_value": daily_value,
    }
    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果已寫入 backtest_result.json")


if __name__ == "__main__":
    main()
