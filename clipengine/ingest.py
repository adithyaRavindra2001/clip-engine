"""Getting media: metadata, audio-only for transcription, and just the needed sections for clips.

Downloading only audio first (then only the clip sections in video) keeps an 8-hour VOD
from costing 10+ GB of disk. `source` may be a URL or a local file (e.g. raw footage
a creator sent you — the safest source of all).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func


def is_local(source: str) -> bool:
    return Path(source).expanduser().exists()


def info(source: str) -> dict:
    if is_local(source):
        p = Path(source).expanduser()
        return {"title": p.stem, "duration": probe_duration(p), "heatmap": None}
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return ydl.extract_info(source, download=False)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def heatmap_hotspots(meta: dict, top: int = 10) -> list[tuple[float, float]]:
    """YouTube's 'Most replayed' graph, when present: (seconds, intensity 0..1), strongest first."""
    hm = meta.get("heatmap") or []
    spots = [((h["start_time"] + h["end_time"]) / 2, float(h["value"])) for h in hm]
    return sorted(spots, key=lambda s: -s[1])[:top]


def download_audio(source: str, work: Path) -> Path:
    out = work / "audio.m4a"
    if out.exists():
        return out
    if is_local(source):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(Path(source).expanduser()),
                        "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "96k", str(out)], check=True)
        return out
    opts = {
        "quiet": True, "no_warnings": True,
        "format": "bestaudio/worstaudio/worst",
        "outtmpl": str(work / "audio_raw.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([source])
    raw = next(work.glob("audio_raw.*"))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-vn", "-ac", "1",
                    "-c:a", "aac", "-b:a", "96k", str(out)], check=True)
    raw.unlink()
    return out


def download_section(source: str, start: float, end: float, out: Path, max_height: int = 1080) -> Path:
    """Video for [start, end] only."""
    if out.exists():
        return out
    if is_local(source):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}",
                        "-i", str(Path(source).expanduser()), "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "18", "-c:a", "aac", str(out)], check=True)
        return out
    opts = {
        "quiet": True, "no_warnings": True,
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "merge_output_format": "mp4",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "outtmpl": str(out.with_suffix("")) + ".%(ext)s",
    }
    for attempt in (1, 2):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([source])
        got = next(out.parent.glob(out.stem + ".*"))
        if got != out:
            got.rename(out)
        # A throttled/aborted range download can leave a file with full audio but only a few seconds of
        # video. Never render that; retry once, then fail loudly.
        have = _video_duration(out)
        if have >= (end - start) - 1.5:
            return out
        print(f"    [warn] section video is {have:.1f}s of {end - start:.1f}s (truncated download), retry {attempt}")
        out.unlink(missing_ok=True)
    raise RuntimeError(f"truncated section download for {source} @ {start:.0f}-{end:.0f}s")


def _video_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip().split("\n")[0])
    except ValueError:
        return 0.0
