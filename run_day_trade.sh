#!/bin/bash
# 台股日盤模擬 — cron 觸發腳本
# 每 15 分鐘執行一次（由 crontab 呼叫）

SIM_DIR="/Users/alvin/Downloads/alvin-agent/tw-stock-sim"
LOG_FILE="$SIM_DIR/logs/day_trade.log"
PYTHON="/usr/bin/python3"

mkdir -p "$SIM_DIR/logs"

echo "──────────────────────────────" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 啟動 day_trade" >> "$LOG_FILE"

cd "$SIM_DIR" && "$PYTHON" scripts/day_trade.py >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 結束" >> "$LOG_FILE"
