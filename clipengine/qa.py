"""Automatic quality assessment of a rendered clip, and thumbnail generation.

Samples frames across the clip and checks what a human reviewer would: is there a person on screen,
fully in frame, most of the time; is the picture bright and sharp; is there speech; is the hook present.
Returns a 0-100 QA score plus the reasons, so the morning pack can select without a human.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT_W, OUT_H = 1080, 1920
_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def _frames(path: Path, n: int = 16):
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / n))
        ok, frame = cap.read()
        if ok:
            yield i, frame
    cap.release()


def _loudness(path: Path) -> float | None:
    out = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True)
    for line in out.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    return None


def assess(clip: Path, hook: str, duration_range=(15, 60)) -> dict:
    faces_ok = faces_cut = bright = sharp = n = 0
    best = None  # (score, frame, index) for the thumbnail
    for i, frame in _frames(clip):
        n += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        mean = float(gray.mean())
        lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        bright += mean > 45
        sharp += lap > 60
        found = _CASCADE.detectMultiScale(gray[: int(h * 0.72)], scaleFactor=1.1, minNeighbors=5,
                                          minSize=(w // 10, w // 10))
        if len(found):
            x, y, fw, fh = max(found, key=lambda b: b[2] * b[3])
            inside = x > w * 0.04 and x + fw < w * 0.96
            faces_ok += inside
            faces_cut += not inside
            score = (fw * fh) / (w * h) * (1 if inside else 0.3) + min(lap, 400) / 4000
            if i >= 2 and (best is None or score > best[0]):   # skip the first ~12%: the hook box is on screen
                best = (score, frame, i)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(clip)], capture_output=True, text=True).stdout.strip() or 0)
    vol = _loudness(clip)

    reasons, score = [], 100.0
    face_frac = faces_ok / max(n, 1)
    if face_frac < 0.5:
        score -= 35; reasons.append(f"person fully in frame only {face_frac:.0%} of the time")
    elif face_frac < 0.75:
        score -= 12; reasons.append(f"person in frame {face_frac:.0%} of the time")
    if faces_cut / max(n, 1) > 0.25:
        score -= 20; reasons.append("speaker cut off at the frame edge in many shots")
    if bright / max(n, 1) < 0.7:
        score -= 15; reasons.append("dark picture")
    if sharp / max(n, 1) < 0.5:
        score -= 10; reasons.append("soft / blurry picture")
    if not (duration_range[0] <= dur <= duration_range[1]):
        score -= 25; reasons.append(f"duration {dur:.0f}s outside {duration_range}")
    if vol is not None and vol < -32:
        score -= 20; reasons.append(f"quiet audio ({vol:.0f} dB)")
    if not hook or len(hook.split()) < 2:
        score -= 10; reasons.append("weak or missing hook text")
    thumb_t = (best[2] + 0.5) / 16 * dur if best else min(6.0, dur / 2)
    return {"qa": max(0, round(score)), "reasons": reasons, "face_frac": round(face_frac, 2),
            "duration": round(dur, 1), "mean_volume_db": vol, "thumb_t": round(thumb_t, 2),
            "thumb_frame_index": best[2] if best else None, "_thumb": best[1] if best else None}


def thumbnail(clip: Path, hook: str, out: Path, frame=None, accent: str = "#FFD400",
              clean: dict | None = None) -> Path:
    """Sharpest in-frame face shot, with the hook text as a bold title block.

    `clean` = {"src": <original src.mp4>, "t": seconds, "layout", "facecam", "crop_top", "crop_bottom"}:
    when given, the frame is re-cut from the uncaptioned source so no burned-in text shows under the title.
    """
    if clean:
        from . import render as _r
        try:
            frame = _r.frame_at(Path(clean["src"]), clean["t"], clean.get("layout", "auto"), clean.get("facecam"),
                                clean.get("crop_top", 0.0), clean.get("crop_bottom", 0.0))
        except Exception:
            frame = None
    if frame is None:
        res = assess(clip, hook)
        frame = res["_thumb"]
    if frame is None:
        cap = cv2.VideoCapture(str(clip)); cap.set(cv2.CAP_PROP_POS_FRAMES, 30); ok, frame = cap.read(); cap.release()
    im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize((OUT_W, OUT_H))
    d = ImageDraw.Draw(im, "RGBA")
    # darken the top band so the title pops
    d.rectangle([0, 0, OUT_W, 560], fill=(0, 0, 0, 110))
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 96)
    words, lines, cur = (hook or "").upper().split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) > OUT_W - 120:
            lines.append(cur); cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    y = 90
    for i, line in enumerate(lines[:4]):
        x = (OUT_W - d.textlength(line, font=font)) / 2
        colour = accent if i == 0 else "white"
        for dx, dy in ((-4, 0), (4, 0), (0, -4), (0, 4), (-3, -3), (3, 3), (-3, 3), (3, -3)):
            d.text((x + dx, y + dy), line, font=font, fill="black")
        d.text((x, y), line, font=font, fill=colour)
        y += 112
    im.convert("RGB").save(out, quality=92)
    return out
