"""Per-clip performance, pulled daily and fed back into the machine.

  refresh()               views/likes/duration for every published clip via the Data API (1 quota unit per 50),
                          plus the Shorts-feed hold rate (stayed vs swiped away, average view duration, engaged
                          views) scraped from YouTube Studio for clips that actually got traffic. -> `stats` table,
                          and clips.views for the posted clip rows.
  report()                per-campaign table + the throttle verdict, for the morning health message.
  throttle_state()        is the Shorts feed still testing our new uploads? (a new channel that uploads too fast
                          stops getting test impressions: every clip sits at 0 views, "not enough traffic data")
  performance_examples()  top and bottom clips, for the picker prompt: what this channel's feed actually rewards.

Why Studio and not the Analytics API: the hold rate ("viewed vs swiped away") is only exposed in Studio, and the
automation Chrome profile is already signed in there for uploads.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stats (
    video        TEXT PRIMARY KEY,   -- YouTube video id
    name         TEXT,               -- pack name, e.g. fomo-01
    campaign     TEXT,               -- slug
    day          TEXT,               -- pack date
    title        TEXT,
    published_at TEXT,               -- ISO, UTC
    duration_s   REAL,
    views        INTEGER DEFAULT 0,
    likes        INTEGER DEFAULT 0,
    comments     INTEGER DEFAULT 0,
    engaged      INTEGER,            -- Studio: views that stayed past the first seconds
    stayed_pct   REAL,               -- Studio: % who stayed to watch (vs swiped away)
    avd_s        REAL,               -- Studio: average view duration in seconds
    fetched_at   TEXT,
    studio_at    TEXT
);
"""

IST = timezone(timedelta(hours=5, minutes=30))


def _ensure(db) -> None:
    db.executescript(SCHEMA)


def _num(s: str) -> float:
    s = s.strip().replace(",", "")
    m = re.fullmatch(r"([\d.]+)\s*([KkMm]?)", s)
    if not m:
        return 0.0
    v = float(m.group(1))
    return v * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]


def _iso_duration(s: str) -> float:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0.0
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def _ids(cfg: Config, days: int) -> list[dict]:
    from . import uploader
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for r in uploader.tally_rows(cfg):
        if r["date"] < since:
            continue
        m = re.search(r"(?:shorts/|v=)([\w-]{11})", r.get("youtube_url") or "")
        if m:
            out.append({"video": m.group(1), "name": r["name"], "campaign": r["name"].rsplit("-", 1)[0],
                        "day": r["date"], "title": r["title"]})
    return out


def refresh_api(cfg: Config, db, rows: list[dict]) -> int:
    """Data API pass: views/likes/comments/duration/publishedAt for every id. Returns rows updated."""
    from . import uploader
    key = uploader._api_key(cfg)
    if not key or not rows:
        return 0
    _ensure(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for i in range(0, len(rows), 50):
        chunk = rows[i:i + 50]
        q = urllib.parse.urlencode({"part": "snippet,statistics,contentDetails", "id": ",".join(r["video"] for r in chunk), "key": key})
        with urllib.request.urlopen("https://www.googleapis.com/youtube/v3/videos?" + q, timeout=30) as resp:
            data = json.load(resp)
        by = {r["video"]: r for r in chunk}
        for it in data.get("items", []):
            r = by.get(it["id"])
            if not r:
                continue
            s, sn = it.get("statistics", {}), it.get("snippet", {})
            db.execute(
                "INSERT INTO stats(video, name, campaign, day, title, published_at, duration_s, views, likes, comments, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(video) DO UPDATE SET views=excluded.views, likes=excluded.likes, "
                "comments=excluded.comments, published_at=excluded.published_at, duration_s=excluded.duration_s, "
                "title=excluded.title, fetched_at=excluded.fetched_at",
                (it["id"], r["name"], r["campaign"], r["day"], sn.get("title") or r["title"], sn.get("publishedAt"),
                 _iso_duration(it.get("contentDetails", {}).get("duration")), int(s.get("viewCount", 0)),
                 int(s.get("likeCount", 0)), int(s.get("commentCount", 0)), now))
            db.execute("UPDATE clips SET views=? WHERE posted_url LIKE ?", (int(s.get("viewCount", 0)), f"%{it['id']}%"))
            n += 1
    db.commit()
    return n


def parse_engagement(text: str) -> dict:
    """Pull the Shorts metrics out of a Studio 'Engagement' tab's visible text."""
    text = re.sub(r"\n\s*\n+", "\n", text)
    out: dict = {}
    m = re.search(r"Engaged views\nEngaged views\n([\d.,]+[KkMm]?)\n", text)
    if m:
        out["engaged"] = int(_num(m.group(1)))
    m = re.search(r"Average view duration\n(\d+):(\d+)\n", text)
    if m:
        out["avd_s"] = int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"How viewers engaged\nSince published\n([\d.]+)%\n([\d.]+)%\nStayed to watch\nSwiped away", text)
    if m:
        out["stayed_pct"] = float(m.group(1))
    return out


def refresh_studio(cfg: Config, db, min_views: int = 20, max_age_h: float = 12.0, limit: int = 60, quiet: bool = False) -> int:
    """Studio pass for clips with real traffic: hold rate + AVD. Skips clips refreshed in the last `max_age_h`."""
    from . import uploader
    _ensure(db)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_h)).isoformat(timespec="seconds")
    rows = db.execute("SELECT video, name FROM stats WHERE views >= ? AND (studio_at IS NULL OR studio_at < ?) "
                      "ORDER BY views DESC LIMIT ?", (min_views, cutoff, limit)).fetchall()
    if not rows:
        return 0
    pw, ctx = uploader.launch(cfg)
    page = ctx.new_page()
    n = 0
    try:
        for r in rows:
            try:
                page.goto(f"{uploader.STUDIO}/video/{r['video']}/analytics/tab-interest_viewers/period-default",
                          wait_until="domcontentloaded")
                page.wait_for_selector("text=Average view duration", timeout=30_000)
                time.sleep(2.5)
                got = parse_engagement(page.inner_text("body"))
            except Exception as e:
                if not quiet:
                    print(f"  {r['name']}: studio read failed ({str(e)[:60]})")
                continue
            if got:
                db.execute("UPDATE stats SET engaged=COALESCE(?, engaged), stayed_pct=COALESCE(?, stayed_pct), "
                           "avd_s=COALESCE(?, avd_s), studio_at=? WHERE video=?",
                           (got.get("engaged"), got.get("stayed_pct"), got.get("avd_s"),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"), r["video"]))
                db.commit()
                n += 1
                if not quiet:
                    print(f"  {r['name']}: stayed {got.get('stayed_pct', '?')}%  avd {got.get('avd_s', '?')}s  engaged {got.get('engaged', '?')}")
    finally:
        ctx.close()
        pw.stop()
    return n


def refresh(cfg: Config, db, days: int = 7, studio: bool = True, quiet: bool = False) -> int:
    rows = _ids(cfg, days)
    n = refresh_api(cfg, db, rows)
    if not quiet:
        print(f"views refreshed for {n} clip(s) from the last {days} day(s)")
    if studio and n:
        m = refresh_studio(cfg, db, quiet=quiet)
        if not quiet:
            print(f"studio hold rate refreshed for {m} clip(s)")
    return n


def _age_h(published_at: str | None) -> float:
    if not published_at:
        return 0.0
    t = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def throttle_state(db) -> tuple[str, str]:
    """('ok'|'warn'|'throttled', explanation). Looks at clips 6-48 h old: a healthy channel gets a test batch of
    impressions on every Short within hours, so a majority still at zero means the feed is not distributing us."""
    _ensure(db)
    rows = db.execute("SELECT views, published_at FROM stats WHERE published_at IS NOT NULL").fetchall()
    window = [r for r in rows if 6 <= _age_h(r["published_at"]) <= 48]
    if len(window) < 4:
        return "ok", "not enough recent clips to judge distribution"
    zero = sum(1 for r in window if r["views"] == 0)
    low = sum(1 for r in window if r["views"] < 10)
    msg = f"{zero}/{len(window)} clips aged 6-48 h have 0 views, {low} have <10"
    # A tested Short gets dozens of views within hours even when it flops; under 10 means it was barely shown.
    if zero / len(window) >= 0.5 or low / len(window) >= 0.6:
        return "throttled", f"THROTTLED: {msg}. The Shorts feed is not testing new uploads; fewer uploads, spaced out, one niche per channel."
    if low / len(window) >= 0.3:
        return "warn", f"weak distribution: {msg}"
    return "ok", f"distribution ok: {msg}"


def report(cfg: Config, db, days: int = 7) -> str:
    _ensure(db)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.execute("SELECT * FROM stats WHERE day >= ? ORDER BY campaign, views DESC", (since,)).fetchall()
    if not rows:
        return "(no analytics yet: run `clip analytics`)"
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["campaign"], []).append(r)
    lines = [f"{'campaign':<12}{'clips':>6}{'views':>8}{'median':>8}{'best':>7}{'zero':>6}{'hold%':>7}{'avd%':>6}"]
    for slug, rs in sorted(by.items(), key=lambda kv: -sum(r["views"] for r in kv[1])):
        v = sorted(r["views"] for r in rs)
        med = v[len(v) // 2]
        hold = [r["stayed_pct"] for r in rs if r["stayed_pct"] is not None]
        avd = [r["avd_s"] / r["duration_s"] * 100 for r in rs if r["avd_s"] and r["duration_s"]]
        hold_s = f"{sum(hold) / len(hold):>7.0f}" if hold else f"{'-':>7}"
        avd_s = f"{sum(avd) / len(avd):>6.0f}" if avd else f"{'-':>6}"
        lines.append(f"{slug:<12}{len(rs):>6}{sum(v):>8}{med:>8}{v[-1]:>7}{sum(1 for x in v if x == 0):>6}{hold_s}{avd_s}")
    state, why = throttle_state(db)
    lines.append("")
    lines.append(("⚠️ " if state != "ok" else "") + why)
    top = [r for r in rows if r["stayed_pct"] is not None]
    if top:
        lines.append("")
        lines.append("hold rate (stayed past the first seconds), best to worst:")
        for r in sorted(top, key=lambda r: -r["stayed_pct"])[:8]:
            lines.append(f"  {r['stayed_pct']:>3.0f}%  {r['views']:>6} views  {r['name']:<14} {r['title'][:40]}")
    return "\n".join(lines)


def performance_examples(db, creator_slug: str | None = None, n: int = 5) -> str:
    """For the picker prompt: this channel's real winners and flops (clips at least 12 h old) with hold rates."""
    _ensure(db)
    rows = db.execute("SELECT s.*, c.hook FROM stats s LEFT JOIN clips c ON c.posted_url LIKE '%' || s.video || '%' "
                      "WHERE s.published_at IS NOT NULL").fetchall()
    rows = [r for r in rows if _age_h(r["published_at"]) >= 12]
    if len(rows) < 6:
        return ""
    state, _ = throttle_state(db)
    rows.sort(key=lambda r: -r["views"])
    mine = [r for r in rows if r["campaign"] == creator_slug] if creator_slug else []
    best = rows[:n]
    worst = [r for r in rows[::-1] if r["views"] < 5][:n]

    def fmt(r):
        hold = f", {r['stayed_pct']:.0f}% stayed past the first seconds" if r["stayed_pct"] is not None else ""
        avd = f", avg watched {r['avd_s']:.0f}s of {r['duration_s']:.0f}s" if r["avd_s"] and r["duration_s"] else ""
        hook = f' / hook "{r["hook"]}"' if r["hook"] else ""
        return f"- {r['views']:,} views{hold}{avd}: \"{r['title']}\"{hook} [{r['campaign']}]"

    out = ["Real performance of this channel's recent Shorts (what the feed actually rewarded):",
           "BEST:"] + [fmt(r) for r in best]
    if worst:
        out += ["FLOPS (never got going):"] + [fmt(r) for r in worst]
    if mine:
        out += [f"This campaign's best so far: {fmt(mine[0])}"]
    if state == "throttled":
        out.append("Note: the channel is currently under-distributed, so low view counts are mostly not the clip's fault; "
                   "weight the hold rates more than the raw views.")
    out.append("Lesson to apply: the winners open on a concrete number, a name, or a confrontation with zero wind-up, "
               "and the title is a short specific claim, not a quote. Pick and title accordingly.")
    return "\n".join(out)
