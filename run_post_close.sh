#!/bin/bash
# 台股收盤後 — 抓收盤價 + 產生報表
# 每日 14:35 執行

SIM_DIR="/Users/alvin/Downloads/alvin-agent/tw-stock-sim"
LOG_FILE="$SIM_DIR/logs/post_close.log"
PYTHON="/usr/bin/python3"

mkdir -p "$SIM_DIR/logs"

echo "──────────────────────────────" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 收盤後處理" >> "$LOG_FILE"

cd "$SIM_DIR"

echo "[fetch_prices]" >> "$LOG_FILE"
"$PYTHON" scripts/fetch_prices.py >> "$LOG_FILE" 2>&1

echo "[generate_report]" >> "$LOG_FILE"
"$PYTHON" generate_report.py >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG_FILE"
