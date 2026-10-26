#!/bin/zsh
# One-time setup on an Apple Silicon Mac. Run from the repo folder: ./scripts/setup_mac.sh
set -e
cd "$(dirname "$0")/.."

if ! command -v brew >/dev/null; then
  echo "Install Homebrew first: https://brew.sh" && exit 1
fi
# ffmpeg-full: the plain ffmpeg bottle lacks libass, which the caption burn-in needs.
brew list ffmpeg-full >/dev/null 2>&1 || brew install ffmpeg-full
PY=""
for v in 3.12 3.13; do
  [ -x /opt/homebrew/bin/python$v ] && PY=/opt/homebrew/bin/python$v && break
done
if [ -z "$PY" ]; then brew install python@3.12; PY=/opt/homebrew/bin/python3.12; fi
echo "Using $PY"
"$PY" -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

[ -f config.yaml ]   || cp config.example.yaml config.yaml
[ -f creators.yaml ] || cp creators.example.yaml creators.yaml
[ -f .env ]          || printf 'export ANTHROPIC_API_KEY=""\nexport TWITCH_CLIENT_ID=""\nexport TWITCH_CLIENT_SECRET=""\nexport YOUTUBE_API_KEY=""\n' > .env

echo
echo "Done. Next:"
echo "  1. Put your keys in .env"
echo "  2. Add creators you have permission for to creators.yaml (status: allowed)"
echo "  3. source .venv/bin/activate && source .env && clip run <url> --creator <name>"
