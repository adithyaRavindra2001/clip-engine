"""Push a message to the user: macOS notification always, plus Telegram and/or ntfy when configured (both free).

Telegram (recommended): make a bot with @BotFather, put TELEGRAM_BOT_TOKEN=... in .env, send the bot any message
from your phone, then run `clip notify --setup-telegram` once: it reads the chat id and stores it in data/notify.json.
ntfy: install the ntfy app, subscribe to a topic, put that topic under notify.ntfy_topic in config.yaml.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

from .config import Config


def _env(cfg: Config, key: str) -> str:
    val = os.environ.get(key, "")
    env = cfg.root / ".env"
    if not val and env.exists():
        m = re.search(rf"^\s*(?:export\s+)?{key}=['\"]?([^'\"\n]+)", env.read_text(), re.M)
        val = m.group(1).strip() if m else ""
    return val


def _state(cfg: Config) -> dict:
    f = cfg.data_dir / "notify.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _post(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 20) -> str:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def telegram_setup(cfg: Config) -> str:
    token = _env(cfg, "TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not in .env (create a bot with @BotFather first)")
    upd = json.loads(_post(f"https://api.telegram.org/bot{token}/getUpdates"))
    chats = [u["message"]["chat"] for u in upd.get("result", []) if "message" in u]
    if not chats:
        raise SystemExit("No message found: open the bot in Telegram, press Start, send 'hi', then rerun")
    chat = chats[-1]
    f = cfg.data_dir / "notify.json"
    f.write_text(json.dumps({**_state(cfg), "telegram_chat_id": chat["id"]}, indent=1))
    send(cfg, "clip-engine", "Connected. You'll get the 09:00 report and any failure here.")
    return f"Telegram connected to chat {chat.get('first_name') or chat['id']}"


def send(cfg: Config, title: str, body: str) -> list[str]:
    """Returns the channels that accepted the message."""
    ok = []
    try:
        subprocess.run(["osascript", "-e", f'display notification "{body[:200]}" with title "{title}"'],
                       capture_output=True, timeout=10)
        ok.append("mac")
    except Exception:
        pass
    token = _env(cfg, "TELEGRAM_BOT_TOKEN")
    chat = _state(cfg).get("telegram_chat_id") or _env(cfg, "TELEGRAM_CHAT_ID")
    if token and chat:
        text = f"*{title}*\n```\n{body[:3900]}\n```" if "\n" in body else f"*{title}*\n{body[:3900]}"
        try:
            _post(f"https://api.telegram.org/bot{token}/sendMessage",
                  urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode())
            ok.append("telegram")
        except Exception:
            try:  # Markdown can choke on odd characters: resend plain
                _post(f"https://api.telegram.org/bot{token}/sendMessage",
                      urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{body[:3900]}"}).encode())
                ok.append("telegram")
            except Exception as e:
                print(f"[notify] telegram failed: {e}")
    topic = (cfg["notify"] or {}).get("ntfy_topic") if "notify" in cfg.raw else ""
    if topic:
        try:
            _post(f"https://ntfy.sh/{topic}", body[:4000].encode(), {"Title": title})
            ok.append("ntfy")
        except Exception as e:
            print(f"[notify] ntfy failed: {e}")
    return ok


def send_files(cfg: Config, caption: str, paths: list) -> bool:
    """Telegram sendDocument for each file (video first), caption on the first one. Returns True if any went."""
    import mimetypes
    import uuid
    from pathlib import Path
    token = _env(cfg, "TELEGRAM_BOT_TOKEN")
    chat = _state(cfg).get("telegram_chat_id") or _env(cfg, "TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[notify] telegram not configured; files stay on disk")
        return False
    sent = 0
    for i, p in enumerate(paths):
        p = Path(p)
        if not p.exists():
            continue
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        method = "sendVideo" if p.suffix == ".mp4" else "sendDocument"
        field = "video" if method == "sendVideo" else "document"
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n".encode()]
        if i == 0 and caption:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1000]}\r\n".encode())
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{p.name}\"\r\n"
                      f"Content-Type: {ctype}\r\n\r\n").encode() + p.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        try:
            _post(f"https://api.telegram.org/bot{token}/{method}", b"".join(parts),
                  {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=120)
            sent += 1
        except Exception as e:
            print(f"[notify] telegram {method} failed for {p.name}: {str(e)[:80]}")
    return sent > 0
