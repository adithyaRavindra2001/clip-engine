#!/bin/zsh
# launchd entry point for every clip-engine job: scripts/install_schedule.sh copies this to
# ~/Library/Application Support/clipengine/job.sh and the plists call it with the job name.
#   job.sh daily    campaign check (keep 5 live) + cut + quota top-up      (17:00, retried 19:30)
#   job.sh upload   publish the pack through Chrome + reconcile           (21:30, retried 02:00)
#   job.sh health   morning audit -> notification                          (09:00)
#   job.sh review   the local review page (keep-alive)
# Why a wrapper: macOS blocks launchd-started shells from reading ~/Desktop until zsh has Full Disk Access. This file
# is readable anywhere, so it can TELL you that instead of failing silently; it also stops two copies of a job
# overlapping (the retry slot is a no-op while the first run is still going) and notifies on failure.
ROOT="__ROOT__"
JOB="${1:?job name}"
STATE="$HOME/Library/Application Support/clipengine"
LOGS="$HOME/Library/Logs/clipengine"
mkdir -p "$STATE" "$LOGS"
notify() { /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true }
echo "=== $(date '+%F %T') job $JOB start ==="

if ! cat "$ROOT/config.yaml" >/dev/null 2>&1; then
  msg="macOS is blocking launchd from reading the project. System Settings > Privacy & Security > Full Disk Access > add /bin/zsh, then rerun scripts/install_schedule.sh"
  echo "BLOCKED: $msg"; notify "clip-engine: $JOB BLOCKED" "$msg"; exit 78
fi

lock="$STATE/$JOB.lock"
if mkdir "$lock" 2>/dev/null; then
  echo $$ > "$lock/pid"; trap 'rm -rf "$lock"' EXIT
else
  old="$(cat "$lock/pid" 2>/dev/null)"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then echo "another $JOB run (pid $old) is still going; skipping"; exit 0; fi
  rm -rf "$lock"; mkdir "$lock"; echo $$ > "$lock/pid"; trap 'rm -rf "$lock"' EXIT
fi

cd "$ROOT" || exit 1
source .venv/bin/activate
export PYTHONUNBUFFERED=1
notify() { clip notify "$1" "$2" >/dev/null 2>&1 || /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true }
export PATH="$HOME/.nvm/versions/node/v22.21.1/bin:/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:$PATH"
rc=0
case "$JOB" in
  daily)
    clip campaigns --auto-join --min-active 5 || echo "[campaigns] check failed (exit $?), cutting anyway"
    ./scripts/daily_claude.sh 1 || rc=$?
    short="$(clip quota --short | tr '\n' ' ')"
    if [ -n "$short" ]; then notify "clip-engine: pack short" "$(date +%F): below 5 clips for $short"; rc=1; fi
    ;;
  upload)
    clip upload --limit 30 --spacing 1500 || rc=$?     # 5 per campaign, ~25 min apart from 21:30 IST through the US day
    clip reconcile || true
    if grep -q "^IG_ACCESS_TOKEN=." .env 2>/dev/null; then clip instagram --limit 10 --spacing 1200 || echo "[instagram] failed (exit $?)"; fi
    ;;
  health)
    clip analytics --days 7 || echo "[analytics] refresh failed (exit $?)"   # views + hold rate first, so the report has them
    clip health || rc=$?
    ;;
  review)  exec clip review ;;
  *) echo "unknown job $JOB"; exit 2 ;;
esac
[ "$rc" -ne 0 ] && notify "clip-engine: $JOB failed (exit $rc)" "see $LOGS/$JOB.log"
echo "=== $(date '+%F %T') job $JOB done (exit $rc) ==="
exit $rc
