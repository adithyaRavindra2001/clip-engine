"""Discovery: find fresh VODs from allowed creators, and surface prospects worth pitching.

Uses only official APIs (Twitch Helix, YouTube Data API v3). Kick has no stable public
VOD-listing API we rely on, so Kick VODs are queued by pasting URLs into `clip run`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from .config import Config, Creator

TWITCH_API = "https://api.twitch.tv/helix"
YT_API = "https://www.googleapis.com/youtube/v3"


@dataclass
class Vod:
    creator: str
    url: str
    title: str
    published: datetime
    duration_s: float
    views: int
    # Seconds into the VOD where viewers made clips, with that clip's view count.
    # Viewer-made clips are a strong "something happened here" signal.
    hotspots: list[tuple[float, int]]

    @property
    def views_per_hour(self) -> float:
        hours = max((datetime.now(timezone.utc) - self.published).total_seconds() / 3600, 1)
        return self.views / hours


def _parse_twitch_duration(s: str) -> float:
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", s):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def _parse_iso_duration(s: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Twitch:
    def __init__(self, cfg: Config):
        tw = cfg["twitch"]
        if not tw["client_id"] or not tw["client_secret"]:
            raise RuntimeError("Twitch client_id/client_secret missing (config.yaml or TWITCH_CLIENT_ID/SECRET).")
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": tw["client_id"], "client_secret": tw["client_secret"], "grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        self.headers = {"Client-Id": tw["client_id"], "Authorization": f"Bearer {r.json()['access_token']}"}

    def get(self, path: str, **params) -> list[dict]:
        r = requests.get(f"{TWITCH_API}/{path}", headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])

    def user_id(self, login: str) -> str | None:
        data = self.get("users", login=login)
        return data[0]["id"] if data else None

    def top_streams(self, language: str, n: int) -> list[dict]:
        return self.get("streams", first=min(n, 100), language=language)

    def recent_vods(self, creator: Creator, limit: int, max_age_days: int) -> list[Vod]:
        uid = self.user_id(creator.handle)
        if not uid:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        vods = []
        for v in self.get("videos", user_id=uid, type="archive", first=limit):
            published = _ts(v["created_at"])
            if published < cutoff:
                continue
            vods.append(Vod(
                creator=creator.name, url=v["url"], title=v["title"], published=published,
                duration_s=_parse_twitch_duration(v["duration"]), views=v["view_count"],
                hotspots=self.hotspots(uid, v["id"], published),
            ))
        return vods

    def hotspots(self, broadcaster_id: str, video_id: str, since: datetime) -> list[tuple[float, int]]:
        clips = self.get("clips", broadcaster_id=broadcaster_id, first=100,
                         started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"))
        spots = [(float(c["vod_offset"]), int(c["view_count"]))
                 for c in clips if c.get("video_id") == video_id and c.get("vod_offset") is not None]
        return sorted(spots, key=lambda s: -s[1])


class YouTube:
    def __init__(self, cfg: Config):
        self.key = cfg["youtube"]["api_key"]
        if not self.key:
            raise RuntimeError("YouTube api_key missing (config.yaml or YOUTUBE_API_KEY).")

    def get(self, path: str, **params) -> dict:
        r = requests.get(f"{YT_API}/{path}", params={**params, "key": self.key}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _videos(self, ids: list[str], creator: str) -> list[Vod]:
        if not ids:
            return []
        items = self.get("videos", part="snippet,statistics,contentDetails", id=",".join(ids))["items"]
        return [Vod(
            creator=creator or it["snippet"]["channelTitle"],
            url=f"https://www.youtube.com/watch?v={it['id']}",
            title=it["snippet"]["title"],
            published=_ts(it["snippet"]["publishedAt"]),
            duration_s=_parse_iso_duration(it["contentDetails"]["duration"]),
            views=int(it["statistics"].get("viewCount", 0)),
            hotspots=[],
        ) for it in items]

    def recent_vods(self, creator: Creator, limit: int, max_age_days: int) -> list[Vod]:
        handle = creator.handle if creator.handle.startswith("@") else f"@{creator.handle}"
        ch = self.get("channels", part="contentDetails", forHandle=handle).get("items", [])
        if not ch:
            return []
        uploads = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        items = self.get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=10)["items"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        vods = [v for v in self._videos([i["contentDetails"]["videoId"] for i in items], creator.name)
                if v.published >= cutoff and v.duration_s >= 10 * 60]  # long-form only
        return sorted(vods, key=lambda v: -v.views_per_hour)[:limit]

    def trending_long_videos(self, query: str, days: int = 7, n: int = 15) -> list[Vod]:
        """Search costs 100 quota units per call (default quota: 10,000/day) — keep queries few."""
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = self.get("search", part="snippet", q=query, type="video", order="viewCount",
                       videoDuration="long", publishedAfter=after, maxResults=n, relevanceLanguage="en")
        return self._videos([i["id"]["videoId"] for i in res.get("items", [])], "")


def fresh_vods(cfg: Config) -> list[Vod]:
    """New VODs from creators you have permission to clip, best first."""
    d = cfg["discovery"]
    out: list[Vod] = []
    twitch = youtube = None
    for c in cfg.creators():
        if not c.allowed:
            continue
        try:
            if c.platform == "twitch":
                twitch = twitch or Twitch(cfg)
                out += twitch.recent_vods(c, d["vods_per_creator"], d["max_vod_age_days"])
            elif c.platform == "youtube":
                youtube = youtube or YouTube(cfg)
                out += youtube.recent_vods(c, d["vods_per_creator"], d["max_vod_age_days"])
            else:
                print(f"  [skip] {c.name}: {c.platform} has no auto-discovery — queue VODs with `clip run <url>`")
        except Exception as e:  # one broken creator shouldn't stop the run
            print(f"  [warn] discovery failed for {c.name}: {e}")
    return sorted(out, key=lambda v: (-len(v.hotspots), -v.views_per_hour))


def prospects_report(cfg: Config) -> str:
    """Markdown report of big streamers / hot long-form videos you don't have permission for yet."""
    known = {c.handle.lower().lstrip("@") for c in cfg.creators()}
    d = cfg["discovery"]
    lines = [f"# Prospects — {datetime.now():%Y-%m-%d}", "",
             "Not in your allowed list. Look for their clipping campaign, or pitch them.", ""]

    try:
        tw = Twitch(cfg)
        lines += ["## Top live Twitch streams", "", "| Streamer | Viewers | Category | Status |", "|---|---|---|---|"]
        for s in tw.top_streams(d["twitch_language"], d["twitch_top_streams"])[:40]:
            tag = "in list" if s["user_login"].lower() in known else "new"
            lines.append(f"| [{s['user_name']}](https://twitch.tv/{s['user_login']}) | {s['viewer_count']:,} | {s['game_name']} | {tag} |")
        lines.append("")
    except Exception as e:
        lines += [f"_Twitch skipped: {e}_", ""]

    try:
        yt = YouTube(cfg)
        lines += ["## Hot long-form YouTube videos (last 7 days)", "", "| Video | Channel | Views/hour |", "|---|---|---|"]
        seen = set()
        for q in d["podcast_queries"]:
            for v in yt.trending_long_videos(q):
                if v.url in seen:
                    continue
                seen.add(v.url)
                lines.append(f"| [{v.title[:70]}]({v.url}) | {v.creator} | {v.views_per_hour:,.0f} |")
        lines.append("")
    except Exception as e:
        lines += [f"_YouTube skipped: {e}_", ""]

    return "\n".join(lines)
