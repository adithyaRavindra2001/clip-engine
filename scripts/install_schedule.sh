#!/bin/zsh
# Schedules `clip daily` at 03:30 every night via launchd. Your Mac must be awake (or set to wake) then.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/data" ~/Library/LaunchAgents ~/Library/Logs/clipengine
for job in daily review; do
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME|g" "$ROOT/scripts/com.clipengine.$job.plist" > ~/Library/LaunchAgents/com.clipengine.$job.plist
  launchctl unload ~/Library/LaunchAgents/com.clipengine.$job.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/com.clipengine.$job.plist
done
echo "Scheduled: daily_claude.sh at 17:30 (log: ~/Library/Logs/clipengine/daily.log); review page kept alive at http://localhost:8765"
echo "Tip: System Settings > Battery > Options > prevent sleep when plugged in, or: sudo pmset repeat wake MTWRFSU 03:25:00"
