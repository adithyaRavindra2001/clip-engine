"""Analytics proof: a screen recording + screenshots of a clip's YouTube Studio analytics, for campaigns that pay only
after you show them views, retention and audience geography (Ramp: "screen-record your analytics after 2,000 views",
Iced Coffee Hour: "send demographics once past the minimum payout").

  due(cfg, db)                 clips whose views crossed their campaign's `proof_views` and have no proof yet
  record(cfg, db, video, ...)  open Studio on the right channel, walk the Overview / Reach / Engagement / Audience tabs
                               while Chrome records the screen -> data/proof/<name>/<name>.mp4 + one PNG per tab
  send(cfg, row)               push the recording and screenshots to the phone (Telegram sendDocument)

Nothing here submits anything to a campaign: the files land on the phone and in data/proof/, the clipper forwards
them wherever the brief says (ticket, form, Discord). The 09:00 health job runs `clip proof --due --send`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS proofs (
    video       TEXT PRIMARY KEY,
    name        TEXT,
    campaign    TEXT,
    views       INTEGER,
    recorded_at TEXT,
    files       TEXT,      -- JSON list of paths
    sent_at     TEXT,
    error       TEXT
);
"""

# Studio video-analytics tabs, in the order a reviewer wants to see them.
TABS = [
    ("overview", "tab-overview", "Views"),
    ("reach", "tab-reach_viewers", "Impressions"),
    ("engagement", "tab-interest_viewers", "Average view duration"),
    ("audience", "tab-build_audience", "Returning viewers|Top geographies|Age and gender|Viewer age"),
]


def _ensure(db) -> None:
    db.executescript(SCHEMA)


def thresholds(cfg: Config) -> dict[str, int]:
    """{campaign slug: views at which proof is due}; campaigns without proof_views are skipped."""
    return {c.slug: int(c.proof_views) for c in cfg.creators() if int(c.proof_views or 0) > 0}


def due(cfg: Config, db) -> list[dict]:
    """Published clips at or past their campaign's proof_views with no recording yet (needs `clip analytics` stats)."""
    _ensure(db)
    th = thresholds(cfg)
    if not th:
        return []
    out = []
    for r in db.execute("SELECT s.video, s.name, s.campaign, s.views, s.title FROM stats s "
                        "LEFT JOIN proofs p ON p.video = s.video WHERE p.video IS NULL ORDER BY s.views DESC"):
        need = th.get(r["campaign"])
        if need and r["views"] >= need:
            out.append(dict(r))
    return out


def _channel_for_slug(cfg: Config, slug: str) -> str:
    for c in cfg.creators():
        if c.slug == slug:
            return c.youtube_channel or ""
    return ""


def record(cfg: Config, db, video: str, name: str = "", campaign: str = "", views: int | None = None) -> dict:
    """Record Studio's analytics for one video. Returns the proofs row (files, error)."""
    from . import uploader
    _ensure(db)
    name = name or video
    out = cfg.data_dir / "proof" / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    frames = out / "_frames"
    frames.mkdir()
    files: list[str] = []
    error = None
    n_frames = 0

    def frame(page, hold: int = 1) -> None:
        # The "screen recording" is a stream of viewport screenshots stitched at 2 fps: real Chrome refuses
        # Playwright's native video capture on the persistent profile, and frames are what a reviewer reads anyway.
        nonlocal n_frames
        for _ in range(hold):
            n_frames += 1
            page.screenshot(path=str(frames / f"f{n_frames:04d}.png"))

    pw, ctx = uploader.launch(cfg)
    page = ctx.new_page()
    try:
        channel = _channel_for_slug(cfg, campaign or name.rsplit("-", 1)[0])
        uploader._channel(page, channel, cfg)
        for key, tab, ready in TABS:
            page.goto(f"{uploader.STUDIO}/video/{video}/analytics/{tab}/period-default", wait_until="domcontentloaded")
            try:
                page.wait_for_selector(f"text=/{ready}/", timeout=45_000)
            except Exception:
                pass
            time.sleep(3)
            shot = out / f"{name}-{key}.png"
            page.screenshot(path=str(shot))
            files.append(str(shot))
            frame(page, hold=4)                      # 2 s on the top of the tab
            page.mouse.move(820, 520)                # Studio scrolls an inner pane: the wheel must be over it
            for _ in range(10):                      # scroll through every card (geography sits at the bottom)
                page.mouse.wheel(0, 300)
                time.sleep(0.4)
                frame(page)
            frame(page, hold=3)
            shot = out / f"{name}-{key}-bottom.png"
            page.screenshot(path=str(shot))
            files.append(str(shot))
    except Exception as e:
        error = str(e)[:300]
    finally:
        try:
            page.close()
        except Exception:
            pass
        ctx.close()
        pw.stop()
    if n_frames:
        mp4 = out / f"{name}.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "2", "-i", str(frames / "f%04d.png"),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", str(mp4)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            files.insert(0, str(mp4))
            shutil.rmtree(frames, ignore_errors=True)
        else:
            error = (error or "") + f" ffmpeg: {r.stderr[:120]}"
    row = {"video": video, "name": name, "campaign": campaign, "views": views,
           "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": json.dumps(files),
           "sent_at": None, "error": error}
    db.execute("INSERT OR REPLACE INTO proofs(video, name, campaign, views, recorded_at, files, sent_at, error) "
               "VALUES (:video, :name, :campaign, :views, :recorded_at, :files, :sent_at, :error)", row)
    db.commit()
    return row


def send(cfg: Config, db, row: dict) -> bool:
    from . import notify
    files = [Path(p) for p in json.loads(row["files"] or "[]") if Path(p).exists()]
    if not files:
        return False
    caption = (f"analytics proof: {row['name']} ({row['campaign']}) at {row.get('views') or '?'} views\n"
               f"https://youtube.com/shorts/{row['video']}\n"
               f"forward these where the campaign brief says (ticket / form), then they approve the payout.")
    ok = notify.send_files(cfg, caption, files)
    if ok:
        db.execute("UPDATE proofs SET sent_at=? WHERE video=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["video"]))
        db.commit()
    return ok


def run_due(cfg: Config, db, do_send: bool = True, limit: int = 5) -> int:
    """Record (and push) every due clip. Returns how many were recorded."""
    todo = due(cfg, db)[:limit]
    if not todo:
        print("no clips past their proof threshold")
        return 0
    n = 0
    for r in todo:
        print(f"  {r['name']}: {r['views']} views, recording Studio analytics…", flush=True)
        row = record(cfg, db, r["video"], r["name"], r["campaign"], r["views"])
        print(f"    {len(json.loads(row['files']))} file(s)" + (f"  error: {row['error']}" if row["error"] else ""), flush=True)
        n += 1
        if do_send:
            print("    sent to phone" if send(cfg, db, row) else "    (not sent: no Telegram or no files)", flush=True)
    return n
