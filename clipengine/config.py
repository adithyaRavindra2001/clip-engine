"""Loads config.yaml and creators.yaml, with environment-variable fallbacks for secrets."""
from __future__ import annotations

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
              "music_dir": "./music"},
    "clips": {"per_video": 8, "min_seconds": 15, "max_seconds": 55, "max_height": 1080},
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


@dataclass
class Creator:
    name: str
    status: str
    platform: str
    handle: str
    permission: str = ""
    campaign_rules: str = ""
    required_tags: list[str] = field(default_factory=list)
    layout: str = "auto"
    facecam: list[float] | None = None
    # Fixed campaign footage (URLs or local files). `clip daily` rotates through these, cutting NEW moments
    # each day, instead of discovering fresh uploads. Use for brand campaigns that ship a folder of videos.
    sources: list[str] = field(default_factory=list)
    cpm: float = 0.0          # $ per 1,000 views the campaign pays; daily works highest-paying creators first

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
