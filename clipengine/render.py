"""Turns a horizontal source section into a 1080x1920 Short with burned-in captions and hook text."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

OUT_W, OUT_H = 1080, 1920
TOP_H = 640  # facecam panel height in split layout


def _even(x: float) -> int:
    return int(x) // 2 * 2


def probe_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(path)], capture_output=True, text=True, check=True)
    s = json.loads(out.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def detect_faces(path: Path, samples: int = 12) -> list[tuple[int, int, int, int]]:
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    boxes = []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h = gray.shape[0]
        found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(h // 18, h // 18))
        if len(found):
            boxes.append(tuple(int(v) for v in max(found, key=lambda b: b[2] * b[3])))
    cap.release()
    return boxes


def _region_sharpness(path: Path, cam: tuple, samples: int = 4) -> float:
    """Median Laplacian variance of the candidate facecam region, upscaled as the split layout would show it."""
    x, y, w, h = (int(v) for v in cam)
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    vals = []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        reg = frame[y:y + max(h, 2), x:x + max(w, 2)]
        if reg.size == 0:
            continue
        reg = cv2.resize(reg, (OUT_W, TOP_H))
        vals.append(float(cv2.Laplacian(cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()))
    cap.release()
    return float(np.median(vals)) if vals else 0.0


def frame_at(src: Path, t: float, layout: str = "auto", facecam: list[float] | None = None,
             crop_top: float = 0.0, crop_bottom: float = 0.0):
    """One 1080x1920 frame from the ORIGINAL source at time t, laid out exactly like the clip but
    without captions or hook, as a BGR array. Used for clean thumbnails."""
    L = choose_layout(src, layout, facecam)
    graph = video_filter(L, crop_top, crop_bottom) + ";[base]null[v]"
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-filter_complex", graph, "-map", "[v]",
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    arr = np.frombuffer(out, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def choose_layout(path: Path, layout: str, facecam: list[float] | None) -> dict:
    W, H = probe_size(path)
    if facecam:
        fx, fy, fw, fh = facecam
        return {"kind": "split", "W": W, "H": H, "cam": (fx * W, fy * H, fw * W, fh * H)}

    boxes = detect_faces(path) if layout in ("auto", "crop", "split") else []
    if boxes:
        # Several people on stage: a plain median of face positions can land between two speakers.
        # Cluster by horizontal position and keep the cluster that appears in the most sampled frames.
        W_ = probe_size(path)[0]
        clusters: list[list[tuple[int, int, int, int]]] = []
        for b in sorted(boxes, key=lambda b: b[0] + b[2] / 2):
            cx_b = b[0] + b[2] / 2
            if clusters and abs(cx_b - (clusters[-1][-1][0] + clusters[-1][-1][2] / 2)) < 0.12 * W_:
                clusters[-1].append(b)
            else:
                clusters.append([b])
        boxes = max(clusters, key=len)
        x, y, w, h = (float(np.median([b[i] for b in boxes])) for i in range(4))
        cx = x + w / 2
        small_corner_face = w < 0.12 * W and (cx < 0.3 * W or cx > 0.7 * W)
        if layout == "split" or (layout == "auto" and small_corner_face):
            # Expand the face box to roughly the facecam frame.
            cw, ch = w * 2.8, h * 2.4
            cam = (max(0, cx - cw / 2), max(0, y + h / 2 - ch / 2), min(cw, W), min(ch, H))
            # A real facecam overlay is sharp; a distant speaker on a wide stage is not, and would be
            # magnified into mush. Fall back to a plain crop in that case.
            if layout == "auto" and _region_sharpness(path, cam) < 45:
                return {"kind": "crop", "W": W, "H": H, "cx": cx}
            return {"kind": "split", "W": W, "H": H, "cam": cam}
        return {"kind": "crop", "W": W, "H": H, "cx": cx}
    return {"kind": "crop", "W": W, "H": H, "cx": W / 2}


def video_filter(L: dict, crop_top: float = 0.0, crop_bottom: float = 0.0) -> str:
    W, H = L["W"], L["H"]
    if L["kind"] == "crop":
        # Optionally drop bands at the top/bottom of the source first (burned-in subtitles, tickers).
        y0 = _even(H * crop_top)
        ch = _even(H * (1 - crop_top - crop_bottom))
        cw = min(_even(ch * OUT_W / OUT_H), _even(W))
        x = _even(min(max(L["cx"] - cw / 2, 0), W - cw))
        return f"[0:v]crop={cw}:{ch}:{x}:{y0},scale={OUT_W}:{OUT_H},setsar=1[base]"

    # split: facecam on top, centre of gameplay below
    fx, fy, fw, fh = L["cam"]
    aspect = OUT_W / TOP_H
    if fw / fh > aspect:          # too wide -> trim width
        nw = fh * aspect
        fx, fw = fx + (fw - nw) / 2, nw
    else:                          # too tall -> trim height
        nh = fw / aspect
        fy, fh = fy + (fh - nh) / 2, nh
    fx, fy = max(0, min(fx, W - fw)), max(0, min(fy, H - fh))
    bot_h = OUT_H - TOP_H
    gw = min(_even(H * OUT_W / bot_h), _even(W))
    gx = _even((W - gw) / 2)
    return (
        f"[0:v]split=2[a][b];"
        f"[a]crop={_even(fw)}:{_even(fh)}:{_even(fx)}:{_even(fy)},scale={OUT_W}:{TOP_H},setsar=1[top];"
        f"[b]crop={gw}:{_even(H)}:{gx}:0,scale={OUT_W}:{bot_h},setsar=1[bot];"
        f"[top][bot]vstack=inputs=2[base]"
    )


# ---------- captions ----------

ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,2,60,60,560,1
Style: Hook,Arial,70,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,1,0,0,0,100,100,0,0,3,18,0,8,80,80,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

DEFAULT_BRAND = {
    "accent": "#FFD400",     # caption highlight + progress bar colour
    "hook_animation": True,  # hook text pops in instead of sitting static
    "progress_bar": True,    # thin bar along the bottom edge that fills over the clip
    "music": True,           # mix a quiet bed from music/<mood>/ under the speech (skipped if no files)
    "music_db": -22,         # music level relative to full scale; speech ducks it further
}
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac")


def _ass_colour(hex_rgb: str) -> str:
    """'#RRGGBB' -> ASS '&HBBGGRR&'."""
    h = hex_rgb.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _ff_colour(hex_rgb: str) -> str:
    return "0x" + hex_rgb.lstrip("#")


HIGHLIGHT = "{\\c" + _ass_colour(DEFAULT_BRAND["accent"]) + "}"
RESET = r"{\c&HFFFFFF&}"
HOOK_POP = r"{\fad(120,250)\fscx70\fscy70\t(0,160,\fscx106\fscy106)\t(160,260,\fscx100\fscy100)}"
MIN_EVENT = 0.04  # seconds; shortest caption event we will emit


def _t(sec: float) -> str:
    sec = max(sec, 0)
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{sec % 60:05.2f}"


def _clean(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).strip()


def chunk_words(words: list[dict], max_words: int = 3, max_gap: float = 0.6) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    for w in words:
        if not _clean(w["word"]):
            continue
        cur = chunks[-1] if chunks else None
        if (cur is None or len(cur) >= max_words or w["start"] - cur[-1]["end"] > max_gap
                or re.search(r"[.!?,]$", cur[-1]["word"].strip())):
            chunks.append([w])
        else:
            cur.append(w)
    return chunks


def build_ass(words: list[dict], offset: float, duration: float, hook: str, split: bool = False,
              accent: str = DEFAULT_BRAND["accent"], hook_animation: bool = True) -> str:
    global HIGHLIGHT
    HIGHLIGHT = "{\\c" + _ass_colour(accent) + "}"
    lines = [ASS_HEADER]
    if hook:
        # In split layout, sit just under the facecam so the hook doesn't cover the face.
        margin = TOP_H + 30 if split else 0
        pop = HOOK_POP if hook_animation else ""
        lines.append(f"Dialogue: 1,{_t(0)},{_t(min(4.0, duration))},Hook,,0,0,{margin},,{pop}{_clean(hook).upper()}\n")
    rel = [{**w, "start": w["start"] - offset, "end": w["end"] - offset} for w in words]
    chunks = chunk_words(rel)
    for ci, chunk in enumerate(chunks):
        # A chunk must leave the screen when the next one arrives, or two lines stack up.
        next_start = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else duration
        for i, w in enumerate(chunk):
            start = w["start"]
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else min(w["end"] + 0.15, next_start)
            end = min(end, duration)
            if end - start < MIN_EVENT:
                # Zero-length events make libass misrender *other* events (the hook vanished and
                # reappeared as giant sliding letters). Drop them; the next word's line takes over.
                continue
            text = " ".join(
                (HIGHLIGHT + _clean(x["word"]).upper() + RESET) if j == i else _clean(x["word"]).upper()
                for j, x in enumerate(chunk))
            lines.append(f"Dialogue: 0,{_t(start)},{_t(end)},Cap,,0,0,0,,{text}\n")
    return "".join(lines)


def choose_music(music_dir: Path, mood: str, seed: int) -> Path | None:
    """A track from music/<mood>/, else any track under music/. Deterministic per clip so re-renders match."""
    if not music_dir.is_dir():
        return None
    cands = sorted(p for p in (music_dir / (mood or "neutral")).glob("*") if p.suffix.lower() in AUDIO_EXT)
    if not cands:
        cands = sorted(p for p in music_dir.rglob("*") if p.suffix.lower() in AUDIO_EXT)
    return cands[seed % len(cands)] if cands else None


def _has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return "audio" in out.stdout


def render(src: Path, out: Path, words: list[dict], offset: float, hook: str,
           layout: str = "auto", facecam: list[float] | None = None,
           brand: dict | None = None, music: Path | None = None,
           crop_top: float = 0.0, crop_bottom: float = 0.0) -> Path:
    b = {**DEFAULT_BRAND, **(brand or {})}
    L = choose_layout(src, layout, facecam)
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, check=True).stdout.strip())
    ass = out.with_suffix(".ass")
    ass.write_text(build_ass(words, offset, duration, hook, split=L["kind"] == "split",
                             accent=b["accent"], hook_animation=b["hook_animation"]))

    graph = video_filter(L, crop_top, crop_bottom) + f";[base]subtitles={ass.name}[s]"
    if b["progress_bar"]:
        graph += (f";[s]drawbox=x=0:y=ih-14:w='iw*min(t/{duration:.3f},1)':h=14:"
                  f"color={_ff_colour(b['accent'])}@0.9:t=fill[v]")
    else:
        graph += ";[s]null[v]"

    inputs = ["-i", src.name]
    amap = ["-map", "0:a?"]
    has_audio = _has_audio(src)
    # Speech is normalised to -14 LUFS (YouTube/Instagram target) so quiet stage mics don't sink the clip.
    LOUD = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if music and b["music"] and has_audio:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        fade_at = max(duration - 1.2, 0)
        graph += (f";[1:a]volume={float(b['music_db']):.1f}dB,atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[m];"
                  f"[0:a]{LOUD},asplit[sp][sc];[m][sc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=400[md];"
                  f"[sp][md]amix=inputs=2:duration=first:normalize=0,afade=t=out:st={fade_at:.2f}:d=1.2[a]")
        amap = ["-map", "[a]"]
    elif has_audio:
        graph += f";[0:a]{LOUD}[a]"
        amap = ["-map", "[a]"]
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph,
         "-map", "[v]", *amap, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name],
        cwd=src.parent, check=True)
    return out
