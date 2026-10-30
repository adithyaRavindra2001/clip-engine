"""Loads config.yaml and creators.yaml, with environment-variable fallbacks for secrets."""
from __future__ import annotations

import re

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Homebrew's plain `ffmpeg` bottle ships without libass/freetype, so it cannot burn in captions.
# `brew install ffmpeg-full` is keg-only (not on PATH); prefer it whenever it is installed.
_FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin")
if _FFMPEG_FULL.is_dir() and str(_FFMPEG_FULL) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_FFMPEG_FULL}:{os.environ.get('PATH', '')}"

DEFAULTS: dict[str, Any] = {
    "data_dir": "./data",
    "ready_dir": "./ready",
    "claude": {"model": "claude-opus-5-5", "effort": "medium", "mode": "api"},
    "twitch": {"client_id": "", "client_secret": ""},
    "youtube": {"api_key": ""},
    "transcription": {"model": "mlx-community/whisper-large-v3-turbo", "language": "en"},
    "brand": {"accent": "#FFD400", "hook_animation": True, "progress_bar": True, "music": True, "music_db": -22,
              "music_dir": "./music", "sfx": True, "sfx_db": -9, "sfx_dir": "./sfx"},
    "clips": {"per_video": 8, "min_seconds": 15, "max_seconds": 55, "max_height": 2160},
    "morning": {"per_campaign": 5, "min_qa": 60, "stock_factor": 2},
    "discovery": {
        "twitch_language": "en",
        "twitch_top_streams": 100,
        "vods_per_creator": 2,
        "max_vod_age_days": 3,
        "podcast_queries": ["podcast"],
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _secs(t) -> float:
    """'1:02:05' / '4:34' / 274 -> seconds."""
    if isinstance(t, (int, float)):
        return float(t)
    parts = [float(p) for p in str(t).strip().split(":")]
    out = 0.0
    for p in parts:
        out = out * 60 + p
    return out


@dataclass
class Creator:
    name: str
    status: str
    platform: str
    handle: str
    permission: str = ""
    campaign_rules: str = ""
    required_tags: list[str] = field(default_factory=list)
    instagram: bool = True                                   # also post this campaign's clips as Reels
    instagram_tags: list[str] = field(default_factory=list)  # Instagram handles/hashtags the brief requires (replace YouTube ones)
    layout: str = "auto"
    facecam: list[float] | None = None
    # Fixed campaign footage (URLs or local files). `clip daily` rotates through these, cutting NEW moments
    # each day, instead of discovering fresh uploads. Use for brand campaigns that ship a folder of videos.
    sources: list = field(default_factory=list)   # str, or {url, crop_bottom, crop_top} (fractions of frame height)
    cpm: float = 0.0          # $ per 1,000 views the campaign pays; daily works highest-paying creators first
    # Default trims for every video of this creator, as fractions of the frame: {top, bottom, left, right}.
    # Use it to cut stream overlays (chat column, viewer badge, tickers). Per-source entries override it.
    crop: dict = field(default_factory=dict)
    music: bool = True        # set false when the campaign brief forbids music (e.g. FOMO: "no music")
    # "streamer" = IRL/gaming/reaction content: Claude may put meme sounds (vine boom, fahh, bruh) on the beats.
    # "podcast" (default) = interviews, panels, informative: punch-in zoom only, never a sound.
    edit_style: str = "podcast"
    # Campaign-mandated overlay (PNG with alpha, 1080x1920 or smaller), path relative to the project root.
    watermark: str = ""
    # Short lowercase label used for pack folder names (todays-yt/<date>/<slug>-01/). Default: the name, lowercased.
    slug: str = ""
    # Views at which the campaign wants analytics proof (screen recording of Studio); 0 = never. See proof.py.
    proof_views: int = 0
    # Keep the hook text on screen for the whole clip (briefs that demand a persistent headline, e.g. Ramp).
    hook_hold: bool = False
    # YouTube channel this campaign publishes to (the UC... id from the Studio URL). Empty = the profile's default
    # channel. One channel per niche: the Shorts feed models one audience per channel, so crypto, streamer and
    # sports clips on one channel starve each other. All channels must be brand channels of the signed-in account.
    youtube_channel: str = ""

    def __post_init__(self):
        self.slug = re.sub(r"[^a-z0-9]+", "", (self.slug or self.name).lower()) or "clip"

    @property
    def source_urls(self) -> list[str]:
        return [x["url"] if isinstance(x, dict) else x for x in self.sources]

    # Source keys that describe the footage, not how to render it (never passed to render.render).
    _SOURCE_META = ("url", "windows", "prescribed", "note", "card", "headline", "caption_base")

    def source_options(self, url: str) -> dict:
        """Per-source render tweaks, e.g. crop_bottom: 0.14 to cut burned-in subtitles off a file.
        Starts from the creator-level `crop` defaults."""
        opts = {f"crop_{k}": float(v) for k, v in (self.crop or {}).items() if k in ("top", "bottom", "left", "right")}
        for x in self.sources:
            if isinstance(x, dict) and x.get("url") == url:
                opts.update({k: v for k, v in x.items() if k not in self._SOURCE_META})
        return opts

    def source_windows(self, url: str) -> list[dict] | None:
        """Prescribed windows for a source, when the brief fixes WHICH moments may be clipped (Ramp's 12 cards).
        Source form: {url, prescribed: true}            -> the whole file is the one allowed window
                     {url, windows: [{start: "4:34", end: "5:19", headline: "...", card: "02"}, ...]}
        Returns [{start, end (seconds), headline, card}] or None when the source is free footage."""
        for x in self.sources:
            if not (isinstance(x, dict) and x.get("url") == url):
                continue
            if x.get("windows"):
                return [{"start": _secs(w["start"]), "end": _secs(w["end"]), "headline": w.get("headline") or x.get("headline") or "",
                         "card": str(w.get("card") or x.get("card") or "")} for w in x["windows"]]
            if x.get("prescribed"):
                return [{"start": 0.0, "end": None, "headline": x.get("headline") or "", "card": str(x.get("card") or "")}]
        return None

    @property
    def allowed(self) -> bool:
        return self.status == "allowed"


class Config:
    def __init__(self, root: Path):
        self.root = root
        path = root / "config.yaml"
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        self.raw = _merge(DEFAULTS, raw or {})

        env = os.environ.get
        self.raw["twitch"]["client_id"] = env("TWITCH_CLIENT_ID") or self.raw["twitch"]["client_id"]
        self.raw["twitch"]["client_secret"] = env("TWITCH_CLIENT_SECRET") or self.raw["twitch"]["client_secret"]
        self.raw["youtube"]["api_key"] = env("YOUTUBE_API_KEY") or self.raw["youtube"]["api_key"]

        self.data_dir = (root / self.raw["data_dir"]).resolve()
        self.ready_dir = (root / self.raw["ready_dir"]).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ready_dir.mkdir(parents=True, exist_ok=True)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def music_dir(self) -> Path:
        return (self.root / self.raw["brand"]["music_dir"]).resolve()

    @property
    def sfx_dir(self) -> Path:
        """Sound-effects folder; synthesised royalty-free defaults are written here on first use."""
        from . import sfx
        d = (self.root / self.raw["brand"].get("sfx_dir", "./sfx")).resolve()
        if self.raw["brand"].get("sfx", True):
            sfx.ensure(d)
        return d

    @property
    def style_guide_path(self) -> Path:
        return self.data_dir / "style_guide.md"

    def creators(self) -> list[Creator]:
        path = self.root / "creators.yaml"
        if not path.exists():
            return []
        items = (yaml.safe_load(path.read_text()) or {}).get("creators") or []
        return [Creator(**c) for c in items]

    def creator(self, name: str) -> Creator | None:
        for c in self.creators():
            if c.name.lower() == name.lower() or c.handle.lower().lstrip("@") == name.lower().lstrip("@"):
                return c
        return None
