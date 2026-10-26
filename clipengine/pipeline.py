"""One VOD in, a batch of reviewed-ready draft clips out."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import ingest, pick, render, signals, store, transcribe
from .config import Config

LONG_VIDEO = 45 * 60  # above this, transcribe only around hotspots when we have enough of them


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "video"


def process(cfg: Config, db, source: str, creator_name: str,
            twitch_hotspots: list[tuple[float, int]] | None = None, force: bool = False) -> int:
    creator = cfg.creator(creator_name)
    if creator is None:
        raise SystemExit(f"'{creator_name}' is not in creators.yaml. Add them first.")
    if not creator.allowed:
        raise SystemExit(
            f"'{creator.name}' is status '{creator.status}', not 'allowed'. Join their campaign or get "
            f"written permission, then set status: allowed (and note the proof under `permission`).")
    if not force and store.video_seen(db, source):
        print(f"  already processed: {source}")
        return 0

    print(f"→ {creator.name}: {source}")
    meta = ingest.info(source)
    title = meta.get("title") or source
    duration = float(meta.get("duration") or 0)
    video_id = store.upsert_video(db, source, creator.name, title)
    work = cfg.data_dir / "videos" / f"{video_id}-{slug(title)}"
    work.mkdir(parents=True, exist_ok=True)

    try:
        print("  downloading audio…")
        audio = ingest.download_audio(source, work)
        if not duration:
            duration = ingest.probe_duration(audio)

        print("  finding hotspots…")
        tw = twitch_hotspots or []
        replay = ingest.heatmap_hotspots(meta)
        loud = signals.loud_spikes(audio)
        centers = [t + 15 for t, _ in tw[:12]] + [t for t, _ in replay[:6]] + [t for t, _ in loud[:6]]

        windows = None
        if duration > LONG_VIDEO and len(centers) >= 3:
            windows = transcribe.merge_windows(centers, duration)
            mins = sum(e - s for s, e in windows) / 60
            print(f"  transcribing {len(windows)} windows (~{mins:.0f} of {duration / 60:.0f} min)…")
        else:
            print(f"  transcribing full audio ({duration / 60:.0f} min)…")
        tcfg = cfg["transcription"]
        segments = transcribe.transcribe(audio, work / "transcript.json", tcfg["model"], tcfg["language"], windows)
        if not segments:
            raise RuntimeError("transcript is empty")

        used = store.used_ranges(db, video_id)
        if used:
            print(f"  {len(used)} moments already cut from this video; asking for new ones…")
        print("  asking Claude for the best moments…")
        moments = pick.pick_moments(cfg, db, creator, title, segments,
                                    pick.describe_hotspots(tw, replay, loud), work=work, used=used)
        print(f"  {len(moments)} moments picked; rendering…")

        made = 0
        offset_n = len(used)  # continue numbering after clips from earlier rounds
        for n, m in enumerate(moments, offset_n + 1):
            clip_dir = cfg.data_dir / "clips" / f"{video_id}-{n:02d}"
            clip_dir.mkdir(parents=True, exist_ok=True)
            try:
                src = ingest.download_section(source, m.start, m.end, clip_dir / "src.mp4",
                                              cfg["clips"]["max_height"])
                words = transcribe.words_between(segments, m.start, m.end)
                out = render.render(src, clip_dir / "clip.mp4", words, m.start, m.hook,
                                    creator.layout, creator.facecam, brand=cfg["brand"],
                                    music=render.choose_music(cfg.music_dir, m.mood, video_id * 100 + n))
            except Exception as e:
                print(f"    [warn] clip {n} failed: {e}")
                shutil.rmtree(clip_dir, ignore_errors=True)
                continue
            db.execute(
                "INSERT INTO clips(video_id, start, end, title, hook, caption, why, score, path, mood) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (video_id, m.start, m.end, m.title, m.hook, m.caption, m.why, m.score, str(out), m.mood))
            db.commit()
            made += 1
            print(f"    ✓ [{m.score}] {m.title}")

        store.set_video_status(db, video_id, "done")
        # Archive this round's answer so the next `--force` round starts from a fresh request.
        answer = work / "picks.json"
        if answer.exists():
            answer.rename(work / f"picks.round{offset_n + 1:02d}-{offset_n + len(moments):02d}.json")
        return made
    except Exception as e:
        store.set_video_status(db, video_id, "failed", str(e))
        raise


def rerender(cfg: Config, db, clip_id: int, hook: str, trim_start: float = 0.0, trim_end: float = 0.0) -> None:
    """Re-render a clip after the reviewer edits the hook or trims it (no re-download for trims inward)."""
    row = db.execute(
        "SELECT c.*, v.url, v.creator FROM clips c JOIN videos v ON v.id = c.video_id WHERE c.id = ?",
        (clip_id,)).fetchone()
    creator = cfg.creator(row["creator"])
    clip_dir = Path(row["path"]).parent
    work = next((cfg.data_dir / "videos").glob(f"{row['video_id']}-*"))
    segments = json.loads((work / "transcript.json").read_text())

    start, end = row["start"] + trim_start, row["end"] - trim_end
    src = clip_dir / "src.mp4"
    if trim_start or trim_end:
        trimmed = clip_dir / "src_trim.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{trim_start:.2f}", "-to", f"{row['end'] - row['start'] - trim_end:.2f}",
             "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", str(trimmed)],
            check=True)
        trimmed.replace(src)
    n = int(clip_dir.name.rsplit("-", 1)[-1])
    render.render(src, clip_dir / "clip.mp4", transcribe.words_between(segments, start, end), start, hook,
                  creator.layout if creator else "auto", creator.facecam if creator else None,
                  brand=cfg["brand"],
                  music=render.choose_music(cfg.music_dir, row["mood"] or "neutral", row["video_id"] * 100 + n))
    db.execute("UPDATE clips SET hook = ?, start = ?, end = ? WHERE id = ?", (hook, start, end, clip_id))
    db.commit()
