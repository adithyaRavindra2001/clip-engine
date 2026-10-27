"""Instagram Reels publisher: the same clips, posted through Meta's official Content Publishing API (no browser bot).

Setup (once):  Instagram account -> Professional (Creator).  developers.facebook.com -> create app -> add the
"Instagram" product -> "API setup with Instagram login" -> add the account as an Instagram tester and generate
an access token.  Put IG_USER_ID=... and IG_ACCESS_TOKEN=... in .env.  `clip instagram --check` verifies them.

The API needs the video on a public URL, so for the duration of a run the pack folder is served locally and
exposed through a throwaway Cloudflare quick tunnel (free, no account): https://<random>.trycloudflare.com/<file>.
"""
from __future__ import annotations

import csv
import json
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import Config
from .notify import _env

GRAPH = "https://graph.instagram.com/v21.0"
TALLY = "tally/instagram.csv"
HEADER = "date,name,campaign,caption,permalink,posted_at,submitted_at\n"


def _api(method: str, path: str, token: str, **params) -> dict:
    params["access_token"] = token
    url = f"{GRAPH}/{path}"
    data = urllib.parse.urlencode(params).encode() if method == "POST" else None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise RuntimeError(f"Instagram API {e.code} on {path}: {body[:300]}") from None


def creds(cfg: Config) -> tuple[str, str]:
    uid, tok = _env(cfg, "IG_USER_ID"), _env(cfg, "IG_ACCESS_TOKEN")
    if not uid or not tok:
        raise SystemExit("IG_USER_ID / IG_ACCESS_TOKEN missing in .env (see clipengine/instagram.py for the setup)")
    return uid, tok


def check(cfg: Config) -> str:
    uid, tok = creds(cfg)
    me = _api("GET", uid, tok, fields="id,username,account_type,media_count")
    quota = _api("GET", f"{uid}/content_publishing_limit", tok, fields="quota_usage,config")
    q = (quota.get("data") or [{}])[0]
    return (f"@{me.get('username')} ({me.get('account_type')}), {me.get('media_count')} posts; "
            f"publishing used {q.get('quota_usage')}/{(q.get('config') or {}).get('quota_total')} today")


class Tunnel:
    """Serves `folder` on a public throwaway URL for the length of a `with` block."""
    def __init__(self, folder: Path, port: int = 8787):
        self.folder, self.port, self.procs, self.base = folder, port, [], None

    def __enter__(self):
        self.procs.append(subprocess.Popen(["python3", "-m", "http.server", str(self.port), "--bind", "127.0.0.1",
                                            "--directory", str(self.folder)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        cf = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(cf)
        deadline = time.time() + 40
        while time.time() < deadline and cf.poll() is None:
            line = cf.stdout.readline()
            m = re.search(r"https://[\w-]+\.trycloudflare\.com", line or "")
            if m:
                self.base = m.group(0)
                break
        if not self.base:
            self.__exit__(None, None, None)
            raise RuntimeError("cloudflared did not give a public URL (is it installed? brew install cloudflared)")
        time.sleep(3)   # DNS for the new hostname settles
        return self

    def url(self, name: str) -> str:
        return f"{self.base}/{urllib.parse.quote(name)}"

    def __exit__(self, *a):
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass


def publish_reel(cfg: Config, tunnel: Tunnel, mp4: Path, caption: str, cover: Path | None = None) -> tuple[str, str]:
    """Upload one Reel. Returns (media id, permalink)."""
    uid, tok = creds(cfg)
    tmp = f"{secrets.token_hex(8)}.mp4"
    shutil.copy2(mp4, tunnel.folder / tmp)
    try:
        params = {"media_type": "REELS", "video_url": tunnel.url(tmp), "caption": caption[:2200], "share_to_feed": "true"}
        if cover and cover.exists():
            tmpc = tmp.replace(".mp4", ".jpg")
            shutil.copy2(cover, tunnel.folder / tmpc)
            params["cover_url"] = tunnel.url(tmpc)
        container = _api("POST", f"{uid}/media", tok, **params)["id"]
        for _ in range(60):          # Meta fetches + transcodes: usually 20-90 s
            st = _api("GET", container, tok, fields="status_code,status")
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") == "ERROR":
                raise RuntimeError(f"Instagram rejected the video: {st.get('status')}")
            time.sleep(5)
        else:
            raise RuntimeError("Instagram container never finished processing")
        media = _api("POST", f"{uid}/media_publish", tok, creation_id=container)["id"]
        link = _api("GET", media, tok, fields="permalink").get("permalink", "")
        return media, link
    finally:
        for f in tunnel.folder.glob(tmp.replace(".mp4", ".*")):
            f.unlink(missing_ok=True)


def _caption(cfg: Config, e: dict) -> str:
    """YouTube description -> Instagram caption: swap the YouTube handles for the creator's Instagram ones."""
    name = e["name"].rsplit("-", 1)[0]
    creator = next((c for c in cfg.creators() if c.slug == name), None)
    text = f"{e['title']}\n\n{e['description']}"
    if creator and creator.instagram_tags:
        for t in creator.required_tags:
            if t.startswith("@") and t not in creator.instagram_tags:
                text = text.replace(t, "")
        missing = [t for t in creator.instagram_tags if t.lower() not in text.lower()]
        if missing:
            text += "\n" + " ".join(missing)
    return re.sub(r"[ \t]+", " ", text).strip()


def _tally(cfg: Config, row: list[str]) -> None:
    f = cfg.root / TALLY
    f.parent.mkdir(exist_ok=True)
    new = not f.exists()
    with f.open("a", newline="") as fh:
        if new:
            fh.write(HEADER)
        csv.writer(fh).writerow(row)


def run(cfg: Config, db, day: str | None = None, limit: int = 30, spacing: int = 600, dry_run: bool = False) -> int:
    """Post every clip in the pack that is already on YouTube (POSTED:) and not yet on Instagram (IG:), then submit
    the Reel link to the clip's campaign. Clips sit in the pack root or done/."""
    from . import uploader
    day = day or uploader.latest_pack(cfg) or datetime.now().strftime("%Y-%m-%d")
    root = cfg.root / "todays-yt" / day
    upload_txt = root / "UPLOAD.txt"
    if not upload_txt.exists():
        print(f"no pack for {day}")
        return 0
    todo = []
    for e in uploader.parse_pack(upload_txt):
        if not e["posted"] or re.search(r"^IG: ", e["block"], re.M):
            continue
        mp4 = next((p for p in (root / f"{e['name']}.mp4", root / "done" / f"{e['name']}.mp4") if p.exists()), None)
        if mp4:
            e["mp4"] = mp4
            todo.append(e)
    print(f"{day}: {len(todo)} Reel(s) to post (cap {limit}, ~{spacing}s apart)")
    if dry_run or not todo:
        for e in todo[:limit]:
            print(f"  {e['name']}: {_caption(cfg, e)[:90]!r}")
        return 0
    creator_names = {c.slug for c in cfg.creators() if c.allowed and c.instagram}
    todo = [e for e in todo if e["name"].rsplit("-", 1)[0] in creator_names]
    done = 0
    pw = ctx = page = None
    serve = root / ".ig-serve"
    serve.mkdir(exist_ok=True)
    with Tunnel(serve) as tunnel:
        for e in todo[:limit]:
            try:
                cap = _caption(cfg, e)
                print(f"  {e['name']}: posting Reel", flush=True)
                media, link = publish_reel(cfg, tunnel, e["mp4"], cap, e["mp4"].with_suffix(".jpg"))
                uploader._note(upload_txt, e["name"], f"IG: {link}")
                print(f"    live -> {link}", flush=True)
                sub = ""
                if e["campaign_url"] and link:
                    if page is None:
                        pw, ctx = uploader.launch(cfg)
                        page = ctx.new_page()
                    print(f"    submitting on {e['campaign_url']}", flush=True)
                    uploader.content_rewards_submit(page, cfg, e["campaign_url"], link)
                    sub = datetime.now().strftime("%Y-%m-%d %H:%M")
                    uploader._note(upload_txt, e["name"], f"IG-SUBMITTED: {sub}")
                    print("    submitted", flush=True)
                _tally(cfg, [day, e["name"], e["name"].rsplit("-", 1)[0], cap.splitlines()[0][:80], link,
                             datetime.now().strftime("%Y-%m-%d %H:%M"), sub])
                done += 1
            except Exception as ex:
                print(f"  [stop] {e['name']}: {ex}", flush=True)
                uploader._notify("instagram stopped", f"{e['name']}: {str(ex)[:90]}")
                break
            if done < min(limit, len(todo)):
                time.sleep(spacing * (0.8 + 0.4 * secrets.randbelow(100) / 100))
    if ctx is not None:
        try:
            ctx.close(); pw.stop()
        except Exception:
            pass
    print(f"{done} Reel(s) posted.")
    return done
