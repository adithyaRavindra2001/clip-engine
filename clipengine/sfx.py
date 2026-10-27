"""Sound effects, placed where they help: a whoosh as the hook pops in, an impact on the punchline.

All sounds are synthesised here with ffmpeg (filtered noise and sine sweeps), so they are
royalty-free by construction and never trigger Content ID. Drop your own .wav/.mp3 files into
sfx/<name>/ to override any of them; the synthesised versions are only written when a folder is empty.

    sfx/whoosh/   hook entrance (t = 0)                 short airy sweep
    sfx/impact/   each `beat` Claude marks               low thump + click
Real royalty-free samples (Mixkit Sound Effects Free License) live in sfx/whoosh and sfx/impact; the
synthesised fallbacks below are only generated when a folder is empty.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac")

# name -> (ffmpeg lavfi source + filter chain, duration seconds)
SYNTH = {
    "whoosh": (
        "anoisesrc=color=pink:amplitude=0.9:d=0.75,"
        "highpass=f=400,lowpass=f=6000,"
        "volume='if(lt(t,0.25),t*4,max(0,1-(t-0.25)*2))':eval=frame,"
        "afade=t=out:st=0.55:d=0.2", 0.75),
    "impact": (
        "sine=f=55:d=0.6,"
        "volume='max(0,1-t*1.8)':eval=frame,"
        "lowpass=f=240,"
        "aecho=0.8:0.6:40:0.3,"
        "volume=2.2,alimiter=limit=0.95", 0.6),
}


def ensure(sfx_dir: Path) -> None:
    """Create sfx/<name>/<name>.wav for every sound whose folder has no audio file yet."""
    for name, (chain, dur) in SYNTH.items():
        d = sfx_dir / name
        d.mkdir(parents=True, exist_ok=True)
        if any(p.suffix.lower() in AUDIO_EXT for p in d.iterdir()):
            continue
        raw = d / f".{name}.raw.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", chain, "-t", f"{dur}",
             "-ar", "48000", "-ac", "2", str(raw)], check=True)
        # Peak-normalise to -1 dBFS so `sfx_db` means the same thing for every sound.
        det = subprocess.run(["ffmpeg", "-v", "info", "-i", str(raw), "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        m = re.search(r"max_volume: (-?[\d.]+) dB", det)
        gain = -1.0 - float(m.group(1)) if m else 0.0
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af", f"volume={gain:.2f}dB",
                        str(d / f"{name}.wav")], check=True)
        raw.unlink()


def pick(sfx_dir: Path, name: str, seed: int) -> Path | None:
    d = sfx_dir / name
    if not d.is_dir():
        return None
    cands = sorted(p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT)
    return cands[seed % len(cands)] if cands else None


def plan(beats: list[float], duration: float, mood: str, hook: bool) -> list[tuple[str, float]]:
    """(sound, time-in-clip) pairs. Few and deliberate: one whoosh, at most two impacts, one riser."""
    ev: list[tuple[str, float]] = []
    if hook:
        ev.append(("whoosh", 0.0))
    # Impacts land exactly on the punch-in zoom (render.punch_in_filter uses the same beat times), so the
    # sound always has a visible cause. Risers and bells were tried and dropped: with no visual cue they
    # felt random to the viewer.
    kept = sorted(b for b in beats if 0.8 < b < duration - 0.4)[:2]
    for b in kept:
        ev.append(("impact", b))
    return ev


# Per-sound trim relative to `sfx_db`: the whoosh is texture under the hook, the impact must be felt.
GAIN = {"whoosh": -5.0, "impact": 0.0, "pop": -3.0}


def filter_graph(sfx_dir: Path, events: list[tuple[str, float]], seed: int, level_db: float,
                 first_input: int) -> tuple[list[str], str, int]:
    """Extra ffmpeg inputs + a filter snippet that produces [sfx] (a mixed effects stem) from them.

    Returns (input_args, filter_snippet, n_inputs). The snippet is empty when no sound is available.
    """
    inputs: list[str] = []
    parts: list[str] = []
    labels: list[str] = []
    idx = first_input
    for name, t in events:
        f = pick(sfx_dir, name, seed)
        if f is None:
            continue
        inputs += ["-i", str(f)]
        ms = max(0, int(round(t * 1000)))
        parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"volume={level_db + GAIN.get(name, 0.0):.1f}dB,adelay={ms}|{ms}[fx{idx}]")
        labels.append(f"[fx{idx}]")
        idx += 1
    if not labels:
        return [], "", 0
    if len(labels) == 1:
        parts.append(f"{labels[0]}anull[sfx]")
    else:
        parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[sfx]")
    return inputs, ";".join(parts), idx - first_input
