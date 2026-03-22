#!/bin/bash
# Adreel daily marketing runner
# Add to crontab: crontab -e
# Run daily at 9am: 0 9 * * * /Users/bytedance/Desktop/ads_video_hero/marketing/schedule.sh

cd /Users/bytedance/Desktop/ads_video_hero
source .env 2>/dev/null || true

LOG_FILE="marketing/logs/daily_$(date +%Y%m%d).log"
mkdir -p marketing/logs

echo "[$(date)] Starting daily run..." >> "$LOG_FILE"
python3.11 -m marketing.daily_runner >> "$LOG_FILE" 2>&1
echo "[$(date)] Done." >> "$LOG_FILE"
