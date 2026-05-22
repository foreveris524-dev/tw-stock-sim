"""
每日收盤後抓 0050 / 2330 / 00981A / ^TWII 的 OHLC 存成 JSON
由 GitHub Actions 在收盤後執行，存入 price_data/*.json
"""
import json, os, time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

TZ_TW = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ_TW).strftime("%Y-%m-%d")

STOCKS = [
    ("0050",    "0050.TW",   "元大台灣50"),
    ("2330",    "2330.TW",   "台積電"),
    ("00981A",  "00981A.TW", "統一主動台股增長"),
    ("taiex",   "^TWII",     "台灣加權指數"),
]

def fetch_ohlc(ticker, range_="3mo"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            result = data["chart"]["result"][0]
            ts     = result["timestamp"]
            q      = result["indicators"]["quote"][0]
            rows = []
            for i, t in enumerate(ts):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c):
                    continue
                date_str = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
                rows.append({"time": date_str, "open": round(o,2),
                              "high": round(h,2), "low": round(l,2), "close": round(c,2)})
            return rows
        except Exception as e:
            if attempt == 2:
                print(f"  Failed: {e}")
                return []
            time.sleep(2 ** attempt)

os.makedirs("price_data", exist_ok=True)

for code, ticker, name in STOCKS:
    print(f"Fetching {code} ({ticker})...", end=" ", flush=True)
    rows = fetch_ohlc(ticker)
    if rows:
        out = {"code": code, "name": name, "updated": TODAY, "ohlc": rows}
        with open(f"price_data/{code}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print(f"OK ({len(rows)} days, last: {rows[-1]['close']})")
    else:
        print("SKIP")
    time.sleep(0.5)

print(f"\n完成：{TODAY}")
