#!/bin/zsh
# Schedules the nightly cut (21:30 IST) and the upload run (03:30 IST, US prime time) via launchd.
# The Mac must stay awake: sudo pmset -c sleep 0 (plugged in).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/data" ~/Library/LaunchAgents ~/Library/Logs/clipengine
for job in daily review; do
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME|g" "$ROOT/scripts/com.clipengine.$job.plist" > ~/Library/LaunchAgents/com.clipengine.$job.plist
  launchctl unload ~/Library/LaunchAgents/com.clipengine.$job.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/com.clipengine.$job.plist
done
echo "Scheduled: daily_claude.sh at 17:30 (log: ~/Library/Logs/clipengine/daily.log); review page kept alive at http://localhost:8765"
echo "Tip: keep the Mac awake on the charger: sudo pmset -c sleep 0"
