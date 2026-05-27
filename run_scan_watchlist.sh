#!/bin/bash
# 選股雷達 — 每週五 14:40 收盤後執行，更新 watchlist.json

SIM_DIR="/Users/alvin/Downloads/alvin-agent/tw-stock-sim"
LOG_FILE="$SIM_DIR/logs/scan_watchlist.log"
PYTHON="/usr/bin/python3"

mkdir -p "$SIM_DIR/logs"

echo "══════════════════════════════════════" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 選股雷達啟動" >> "$LOG_FILE"

cd "$SIM_DIR" && "$PYTHON" scripts/scan_watchlist.py >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG_FILE"
