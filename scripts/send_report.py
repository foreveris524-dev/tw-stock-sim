import json, os, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

tz_tw = timezone(timedelta(hours=8))
today = datetime.now(tz_tw).strftime("%Y-%m-%d")

with open("portfolio.json") as f:
    p = json.load(f)
with open("futures_portfolio.json") as f:
    fut = json.load(f)

cash = p.get("cash", 0)
total_value = p.get("total_value", 0)
holdings = p.get("holdings", [])
initial_capital = p.get("initial_capital", 1000000)
today_trades = [t for t in p.get("trade_log", [])
                if t.get("date") == today and t.get("action") not in
                ("RESET", "PLAN_BUY", "PLAN_SKIP", "PLAN_SHORT")]

position = fut.get("position")
cumulative_pnl = fut.get("cumulative_pnl", 0)
used_margin = fut.get("used_margin", 0)
initial_margin = fut.get("initial_margin", 200000)
today_fut_trades = [t for t in fut.get("trade_log", [])
                    if t.get("date") == today and t.get("action") not in
                    ("RESET", "PLAN_SHORT")]
today_fut_pnl = sum(t.get("pnl", 0) for t in today_fut_trades)
stock_pnl = total_value - initial_capital
total_pnl = stock_pnl + cumulative_pnl

def sign(n):
    return "+" if n >= 0 else ""

lines = []
lines.append("📊 台股模擬操盤 每日報表")
lines.append(f"日期：{today}")
lines.append("")
lines.append("═══ 現貨帳戶 ═══")
lines.append(f"現金：${cash:,.0f}")
if holdings:
    lines.append("持股：")
    for h in holdings:
        pct = h.get("value", 0) / total_value * 100 if total_value else 0
        upnl = h.get("unrealized_pnl", 0)
        upct = h.get("unrealized_pnl_pct", 0)
        lines.append(f"  {h['code']} {h.get('name','')}  均價${h.get('avg_price',0):,.1f}  {h.get('shares',0)}股  市值${h.get('value',0):,.0f}（{pct:.1f}%）")
        if upnl != 0:
            lines.append(f"    未實現損益：{sign(upnl)}${upnl:,.0f}（{sign(upct)}{upct:.1f}%）")
else:
    lines.append("持股：無")
if today_trades:
    lines.append("今日操作：")
    for t in today_trades:
        lines.append(f"  {t['action']} {t.get('code','')} ${t.get('price',0):,.0f} x {t.get('shares',0)}股")
else:
    lines.append("今日操作：觀望，無交易")
lines.append(f"帳戶總值：${total_value:,.0f}")
lines.append(f"相較起始：{sign(stock_pnl)}${stock_pnl:,.0f}")
lines.append("")
lines.append("═══ 期貨帳戶 ═══")
if position:
    d = "做多" if position.get("direction") == "LONG" else "做空"
    lines.append(f"目前持倉：{d} {position.get('contracts',0)}口 @ {position.get('entry_price',0):,}點")
    upnl = position.get("unrealized_pnl", 0)
    if upnl != 0:
        lines.append(f"未實現損益：{sign(upnl)}${upnl:,.0f}")
else:
    lines.append("目前持倉：無")
if today_fut_trades:
    lines.append("今日操作：")
    for t in today_fut_trades:
        lines.append(f"  {t['action']}  損益：{sign(t.get('pnl',0))}${t.get('pnl',0):,.0f}  {t.get('reason','')}")
else:
    lines.append("今日操作：觀望，無交易")
lines.append(f"今日損益：{sign(today_fut_pnl)}${today_fut_pnl:,.0f}")
lines.append(f"累計損益：{sign(cumulative_pnl)}${cumulative_pnl:,.0f}")
margin_pct = used_margin / initial_margin * 100 if initial_margin else 0
lines.append(f"保證金使用率：{margin_pct:.0f}%")
lines.append("")
lines.append("═══ 整體損益 ═══")
lines.append(f"現貨 {sign(stock_pnl)}${stock_pnl:,.0f}  +  期貨 {sign(cumulative_pnl)}${cumulative_pnl:,.0f}")
lines.append(f"合計：{sign(total_pnl)}${total_pnl:,.0f}")

report = "\n".join(lines)
print(report)

gmail_user = "foreveris524@gmail.com"
gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
if not gmail_password:
    print("ERROR: GMAIL_APP_PASSWORD secret not set")
    raise SystemExit(1)

msg = MIMEMultipart("alternative")
msg["Subject"] = f"【模擬操盤報表】{today} 台股收盤"
msg["From"] = gmail_user
msg["To"] = gmail_user
msg.attach(MIMEText(report, "plain", "utf-8"))
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(gmail_user, gmail_password)
    server.send_message(msg)
    print("報表已寄出！")
