#!/bin/zsh
# Moves the project out of ~/Desktop (macOS blocks launchd jobs from reading Desktop/Documents/Downloads:
# every binary - zsh, python, ffmpeg, node - would need Full Disk Access one by one). Leaves a symlink at the old
# path so old commands keep working, fixes the venv and DB paths in place, and reinstalls the schedule.
#   ./scripts/relocate.sh            (defaults: ~/clip-engine)
set -e
OLD="$(cd "$(dirname "$0")/.." && pwd -P)"
NEW="${1:-$HOME/clip-engine}"
[ -e "$NEW" ] && { echo "$NEW already exists"; exit 1; }
if pgrep -f "clip (upload|daily|morning|reconcile|campaigns|run)" >/dev/null || pgrep -f daily_claude.sh >/dev/null; then
  echo "a clip job is still running; try again when it is done"; exit 1
fi
echo "moving $OLD -> $NEW"
mv "$OLD" "$NEW"
ln -s "$NEW" "$OLD"
cd "$NEW"
# venv: shebangs, activate scripts, pyvenv.cfg, the editable-install finder
grep -rlI --exclude-dir=__pycache__ "$OLD" .venv/bin .venv/pyvenv.cfg .venv/lib/python3.13/site-packages 2>/dev/null \
  | while IFS= read -r f; do sed -i '' "s|$OLD|$NEW|g" "$f"; done
sed -i '' "s|$OLD|$NEW|g" creators.yaml
.venv/bin/python - "$OLD" "$NEW" <<'PY'
import sqlite3, sys
old, new = sys.argv[1], sys.argv[2]
c = sqlite3.connect("data/clipengine.db")
for t, col in (("clips", "path"), ("videos", "url"), ("videos", "path")):
    try:
        c.execute(f"UPDATE {t} SET {col} = REPLACE({col}, ?, ?) WHERE {col} LIKE ?", (old, new, old + "%"))
    except sqlite3.OperationalError:
        pass
c.commit(); print("db paths updated")
PY
.venv/bin/clip quota >/dev/null && echo "clip works from $NEW"
./scripts/install_schedule.sh
echo "done. The old path $OLD is now a symlink to $NEW."
