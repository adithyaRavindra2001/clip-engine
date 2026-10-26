#!/bin/zsh
# Nightly pipeline with NO Anthropic API key: the engine does discovery, transcription and rendering,
# and Claude Code (headless `claude -p`, your subscription) does the moment picking.
#
#   1. `clip daily` transcribes the day's video and writes data/videos/<id>/pick_request.md
#   2. this script hands that prompt to `claude -p`, validates the JSON, saves picks.json
#   3. `clip daily` again renders the clips (audio + transcript are cached)
# Drafts are then waiting at http://localhost:8765. Log: data/daily.log
set -u
cd "$(dirname "$0")/.."
source .venv/bin/activate
[ -f .env ] && source .env
export PATH="$HOME/.nvm/versions/node/v22.21.1/bin:/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:$PATH"  # newest claude first
MAX_VIDEOS="${1:-1}"
echo "=== $(date '+%F %T') daily start (max $MAX_VIDEOS video) ==="

answer_pending() {
  local n=0
  for req in data/videos/*/pick_request.md; do
    [ -f "$req" ] || continue
    local dir="${req%/pick_request.md}"
    [ -f "$dir/picks.json" ] && continue
    echo "--- picking: $dir"
    for attempt in 1 2; do
      claude -p --output-format text --model claude-opus-5-5 \
        "You are the head editor of a short-form clipping team. Answer the request below.
Output ONLY a JSON object of the form {\"moments\": [...]} matching the ANSWER FORMAT schema at the end.
No prose, no markdown fences. Every caption must satisfy the campaign rules in the request (required tags/mentions).
Set \"mood\" on each moment to one of: energetic, tense, funny, emotional, neutral.

$(cat "$req")" > "$dir/picks.raw" 2>>data/daily.log
      python - "$dir" <<'PY' && break
import json, re, sys, pathlib
d = pathlib.Path(sys.argv[1]); raw = (d / "picks.raw").read_text()
m = re.search(r"\{.*\}", raw, re.S)
try:
    obj = json.loads(m.group(0)) if m else None
    assert obj and isinstance(obj.get("moments"), list) and obj["moments"]
except Exception as e:
    print(f"    invalid picks ({e}); retrying"); sys.exit(1)
from clipengine.pick import Picks
Picks.model_validate(obj)
(d / "picks.json").write_text(json.dumps(obj, indent=1)); print(f"    {len(obj['moments'])} moments picked")
PY
    done
    n=$((n+1))
  done
  return 0
}

clip daily --max-videos "$MAX_VIDEOS" --no-prospects
answer_pending
clip daily --max-videos "$MAX_VIDEOS"
echo "=== $(date '+%F %T') daily done ==="
