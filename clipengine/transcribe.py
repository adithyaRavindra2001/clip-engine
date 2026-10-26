"""Speech-to-text with word timestamps. mlx-whisper on Apple Silicon, faster-whisper elsewhere.

For long streams with known hotspots, only windows around those hotspots are transcribed,
which turns an hour of transcription into a few minutes.
"""
from __future__ import annotations

import json
import platform
import subprocess
import tempfile
from pathlib import Path

WINDOW = 150.0  # seconds of context on each side of a hotspot


def merge_windows(centers: list[float], duration: float, pad: float = WINDOW) -> list[tuple[float, float]]:
    spans = sorted((max(0.0, c - pad), min(duration, c + pad)) for c in centers)
    merged: list[tuple[float, float]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _run_model(audio: Path, model: str, language: str | None) -> list[dict]:
    """Returns segments: [{start, end, text, words: [{word, start, end}]}]."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        import mlx_whisper
        res = mlx_whisper.transcribe(str(audio), path_or_hf_repo=model, word_timestamps=True, language=language)
        return [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
                 "words": [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in s.get("words", [])]}
                for s in res["segments"]]

    from faster_whisper import WhisperModel
    name = model.split("/")[-1].removeprefix("whisper-") if "/" in model else model
    wm = WhisperModel(name, device="auto", compute_type="auto")
    segments, _ = wm.transcribe(str(audio), word_timestamps=True, vad_filter=True, language=language)
    return [{"start": s.start, "end": s.end, "text": s.text.strip(),
             "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (s.words or [])]}
            for s in segments]


def transcribe(audio: Path, out: Path, model: str, language: str | None,
               windows: list[tuple[float, float]] | None = None) -> list[dict]:
    if out.exists():
        return json.loads(out.read_text())

    if not windows:
        segments = _run_model(audio, model, language)
    else:
        segments = []
        with tempfile.TemporaryDirectory() as tmp:
            for i, (s, e) in enumerate(windows):
                part = Path(tmp) / f"w{i}.wav"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.2f}", "-to", f"{e:.2f}",
                                "-i", str(audio), "-ac", "1", "-ar", "16000", str(part)], check=True)
                for seg in _run_model(part, model, language):
                    seg["start"] += s
                    seg["end"] += s
                    for w in seg["words"]:
                        w["start"] += s
                        w["end"] += s
                    segments.append(seg)

    out.write_text(json.dumps(segments))
    return segments


def words_between(segments: list[dict], start: float, end: float) -> list[dict]:
    return [w for s in segments for w in s["words"] if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]
