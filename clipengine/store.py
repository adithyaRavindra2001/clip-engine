"""SQLite store for processed videos and candidate clips."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY,
    url         TEXT UNIQUE NOT NULL,
    creator     TEXT NOT NULL,
    title       TEXT,
    status      TEXT NOT NULL DEFAULT 'new',   -- new | done | failed
    error       TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS clips (
    id          INTEGER PRIMARY KEY,
    video_id    INTEGER NOT NULL REFERENCES videos(id),
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    title       TEXT,
    hook        TEXT,
    caption     TEXT,
    why         TEXT,
    score       INTEGER,
    path        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | posted
    reason      TEXT,
    views       INTEGER,
    earnings    REAL,
    posted_url  TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    decided_at  TEXT
);
"""


def connect(data_dir: Path) -> sqlite3.Connection:
    db = sqlite3.connect(data_dir / "clipengine.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = [r[1] for r in db.execute("PRAGMA table_info(clips)")]
    for col, ddl in (("mood", "TEXT DEFAULT 'neutral'"), ("qa", "INTEGER"), ("qa_reasons", "TEXT"),
                     ("beats", "TEXT DEFAULT '[]'"), ("sounds", "TEXT DEFAULT '[]'")):
        if col not in cols:
            db.execute(f"ALTER TABLE clips ADD COLUMN {col} {ddl}")
    db.commit()
    return db


def pending_count(db: sqlite3.Connection, creator: str) -> int:
    """Unreviewed drafts in stock for a campaign; daily skips cutting more when there are plenty."""
    row = db.execute("SELECT COUNT(*) n FROM clips c JOIN videos v ON v.id = c.video_id "
                     "WHERE v.creator = ? AND c.status = 'pending'", (creator,)).fetchone()
    return int(row["n"]) if row else 0


def video_seen(db: sqlite3.Connection, url: str) -> bool:
    row = db.execute("SELECT status FROM videos WHERE url = ?", (url,)).fetchone()
    return row is not None and row["status"] == "done"


def upsert_video(db: sqlite3.Connection, url: str, creator: str, title: str | None) -> int:
    db.execute(
        "INSERT INTO videos(url, creator, title) VALUES (?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET title = COALESCE(excluded.title, videos.title), status = 'new', error = NULL",
        (url, creator, title),
    )
    db.commit()
    return db.execute("SELECT id FROM videos WHERE url = ?", (url,)).fetchone()["id"]


def set_video_status(db: sqlite3.Connection, video_id: int, status: str, error: str | None = None) -> None:
    db.execute("UPDATE videos SET status = ?, error = ? WHERE id = ?", (status, error, video_id))
    db.commit()


def used_ranges(db: sqlite3.Connection, video_id: int) -> list[tuple[float, float]]:
    """Moments already cut from this video (any status), so a re-pick chooses different ones."""
    return [(r["start"], r["end"]) for r in
            db.execute("SELECT start, end FROM clips WHERE video_id = ? ORDER BY start", (video_id,))]


def clip_count(db: sqlite3.Connection, url: str) -> int:
    row = db.execute("SELECT COUNT(*) n FROM clips c JOIN videos v ON v.id = c.video_id WHERE v.url = ?", (url,)).fetchone()
    return int(row["n"]) if row else 0


def headroom(db: sqlite3.Connection, data_dir: Path, url: str) -> float | None:
    """Seconds of a fixed source not yet cut into clips (duration from its cached transcript minus the used ranges).
    None when the video was never transcribed, i.e. an untouched source."""
    import json
    from .pipeline import slug
    row = db.execute("SELECT id, title FROM videos WHERE url = ?", (url,)).fetchone()
    if not row:
        return None
    t = data_dir / "videos" / f"{row['id']}-{slug(row['title'] or '')}" / "transcript.json"
    if not t.exists():
        return None
    try:
        segs = json.loads(t.read_text())
        segs = segs["segments"] if isinstance(segs, dict) else segs
        duration = float(segs[-1]["end"])
    except Exception:
        return None
    used = sum(e - s for s, e in used_ranges(db, row["id"]))
    return max(0.0, duration - used)
