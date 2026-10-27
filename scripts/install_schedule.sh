#!/bin/zsh
# Installs the launchd schedule (IST): 17:00 cut (+19:30 top-up), 21:30 upload (+02:00 retry), 09:00 health, review page.
# The Mac must stay awake: sudo pmset -c sleep 0 (plugged in).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/Library/Application Support/clipengine"
mkdir -p "$ROOT/data" ~/Library/LaunchAgents ~/Library/Logs/clipengine "$STATE"
sed -e "s|__ROOT__|$ROOT|g" "$ROOT/scripts/job.sh" > "$STATE/job.sh" && chmod 755 "$STATE/job.sh"
for job in daily review upload health; do
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME|g" "$ROOT/scripts/com.clipengine.$job.plist" > ~/Library/LaunchAgents/com.clipengine.$job.plist
  launchctl unload ~/Library/LaunchAgents/com.clipengine.$job.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/com.clipengine.$job.plist
done
echo "Scheduled (IST): 17:00 campaign check + cut to 5/campaign (top-up 19:30), 21:30 upload ~25 min apart + reconcile (retry 02:00), 09:00 health check."
echo "Logs: ~/Library/Logs/clipengine/{daily,upload,health}.log   Review page: http://localhost:8765"
if ! launchctl list | grep -q com.clipengine.daily; then echo "!! jobs did not load"; exit 1; fi
echo "If a notification says a job is BLOCKED: System Settings > Privacy & Security > Full Disk Access > + > /bin/zsh, then rerun this."
echo "Tip: keep the Mac awake on the charger: sudo pmset -c sleep 0"
