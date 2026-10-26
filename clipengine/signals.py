"""Cheap 'something happened here' signals from audio: loudness spikes (yelling, laughter, hype)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

RATE = 8000  # Hz — plenty for loudness, keeps memory tiny


def loudness_per_second(audio: Path) -> np.ndarray:
    """RMS loudness for each second, streamed so long VODs don't load into memory at once."""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    values = []
    chunk = RATE * 2  # one second of 16-bit samples
    while True:
        buf = proc.stdout.read(chunk)
        if len(buf) < 2:
            break
        x = np.frombuffer(buf[: len(buf) // 2 * 2], dtype=np.int16).astype(np.float32)
        values.append(float(np.sqrt(np.mean(x * x))))
    proc.wait()
    return np.array(values)


def loud_spikes(audio: Path, top: int = 15, min_gap: float = 60.0) -> list[tuple[float, float]]:
    """(second, z-score) for the loudest moments relative to the stream's own baseline."""
    rms = loudness_per_second(audio)
    if len(rms) < 30:
        return []
    # Smooth over 3s, compare against a rolling 5-minute baseline.
    smooth = np.convolve(rms, np.ones(3) / 3, mode="same")
    win = min(300, len(rms))
    baseline = np.convolve(rms, np.ones(win) / win, mode="same")
    z = (smooth - baseline) / (np.std(rms) + 1e-6)

    picks: list[tuple[float, float]] = []
    for t in np.argsort(-z):
        if z[t] < 1.5 or len(picks) >= top:
            break
        if all(abs(t - p) >= min_gap for p, _ in picks):
            picks.append((float(t), round(float(z[t]), 2)))
    return picks
