"""Claude reads the transcript plus signals and returns ranked clip candidates."""
from __future__ import annotations

import os
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from .config import Config, Creator

SYSTEM = """You are the head editor of a short-form clipping team. You pick moments from long \
livestreams and podcasts that will perform as standalone YouTube Shorts / Instagram Reels.

What makes a moment work:
- The first 2 seconds hook a cold viewer: a shocking line, a punchline, a confrontation, a big \
reaction, a bold claim, or an unresolved question.
- It is self-contained: someone who has never seen the creator understands it without context \
(or with one line of on-screen text you write).
- It has a payoff: a laugh, a reveal, a fail, a win, an argument that lands.
- It is the creator's best self — avoid moments that humiliate third parties, reveal private \
info, or would embarrass the creator (they're paying for this).

Hard rules:
- Clip length between {min_s} and {max_s} seconds. Start on the hook, not on preamble. End right \
after the payoff.
- Timestamps must come from the transcript. Do not invent dialogue.
- Moments must not overlap.
- Follow the campaign rules exactly; drop any moment that breaks them.
- The hook text is on-screen text in the clip's first seconds: max 8 words, punchy, adds \
context or tension — this is our original contribution, so make it genuinely useful, not clickbait \
that the clip doesn't deliver.
"""


class Moment(BaseModel):
    start: float = Field(description="Start time in seconds, from the transcript")
    end: float = Field(description="End time in seconds, from the transcript")
    score: int = Field(description="Predicted performance 1-100")
    title: str = Field(description="Short title for YouTube Shorts, under 70 characters")
    hook: str = Field(description="On-screen hook text, max 8 words")
    caption: str = Field(description="Post description: one line + 3-5 relevant hashtags")
    why: str = Field(description="One sentence: why this will perform")
    mood: str = Field(default="neutral",
                      description="Music mood for the background bed: energetic | tense | funny | emotional | neutral")


class Picks(BaseModel):
    moments: list[Moment]


def _fmt(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"


def transcript_text(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)


def feedback_examples(db, limit: int = 15) -> str:
    rows = db.execute(
        "SELECT status, title, hook, reason, views FROM clips WHERE status IN ('approved','rejected','posted') "
        "ORDER BY decided_at DESC LIMIT ?", (limit,),
    ).fetchall()
    if not rows:
        return ""
    lines = []
    for r in rows:
        perf = f", {r['views']:,} views" if r["views"] else ""
        note = f" — {r['reason']}" if r["reason"] else ""
        lines.append(f"- {r['status'].upper()}{perf}: \"{r['title']}\" / hook \"{r['hook']}\"{note}")
    return "Recent decisions by the human reviewer (learn their taste):\n" + "\n".join(lines)


def pick_via_file(work: Path, system: str, user: str) -> Picks:
    """Claude mode 'file': no API key. Write the full prompt to disk, read picks back from picks.json.

    Lets a Claude Code session (or you) do the picking by hand. Run `clip run` once to produce
    pick_request.md, answer it as picks.json next to it, then run the same command again.
    """
    req, ans = work / "pick_request.md", work / "picks.json"
    if ans.exists() and req.exists() and ans.stat().st_mtime < req.stat().st_mtime:
        ans.rename(work / "picks.stale.json")  # answer predates this request: never reuse it
    if ans.exists() and ans.stat().st_mtime < (work / "transcript.json").stat().st_mtime:
        ans.rename(work / "picks.stale.json")
    req.write_text(f"# SYSTEM\n\n{system}\n\n# USER\n\n{user}\n\n"
                   "# ANSWER FORMAT\n\nWrite picks.json next to this file:\n"
                   + Picks.model_json_schema().__repr__())
    if not ans.exists():
        raise SystemExit(
            f"claude.mode is 'file' and no picks yet.\n  Prompt written to: {req}\n"
            f"  Answer it by writing: {ans}  (JSON: {{\"moments\": [...]}} matching the schema at the end of the prompt)\n"
            f"  then rerun the same `clip run` command (audio + transcript are cached).")
    return Picks.model_validate_json(ans.read_text())


def pick_moments(cfg: Config, db, creator: Creator, title: str, segments: list[dict],
                 hotspots: list[str], work: Path | None = None,
                 used: list[tuple[float, float]] | None = None) -> list[Moment]:
    c = cfg["clips"]
    style = cfg.style_guide_path.read_text() if cfg.style_guide_path.exists() else ""

    context = [f"Creator: {creator.name} ({creator.platform})", f"Video title: {title}"]
    if creator.campaign_rules:
        context.append(f"Campaign rules:\n{creator.campaign_rules}")
    if hotspots:
        context.append("Signals (moments flagged by viewer clips, replay data or audio spikes):\n" + "\n".join(hotspots))
    if style:
        context.append(f"House style guide (learned from past performance):\n{style}")
    fb = feedback_examples(db)
    if fb:
        context.append(fb)
    if used:
        context.append("ALREADY CLIPPED from this video (do not pick anything overlapping these; find NEW moments):\n"
                       + "\n".join(f"- {_fmt(s)}-{_fmt(e)} ({s:.0f}s-{e:.0f}s)" for s, e in used))

    user = (
        "\n\n".join(context)
        + f"\n\nTranscript (timestamps in seconds):\n{transcript_text(segments)}\n\n"
        + f"Return the {c['per_video']} best moments, best first."
    )

    system = SYSTEM.format(min_s=c["min_seconds"], max_s=c["max_seconds"])
    mode = cfg["claude"].get("mode", "api")
    if mode == "file" or (mode == "auto" and not os.environ.get("ANTHROPIC_API_KEY")):
        if work is None:
            raise RuntimeError("file mode needs a work dir")
        return snap(pick_via_file(work, system, user).moments, segments, c["min_seconds"], c["max_seconds"], used)

    client = anthropic.Anthropic()
    resp = client.beta.messages.parse(
        model=cfg["claude"]["model"],
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": cfg["claude"]["effort"]},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=Picks,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        print(f"  [warn] Claude returned no picks (stop_reason={resp.stop_reason})")
        return []
    return snap(resp.parsed_output.moments, segments, c["min_seconds"], c["max_seconds"], used)


def snap(moments: list[Moment], segments: list[dict], min_s: float, max_s: float,
         used: list[tuple[float, float]] | None = None) -> list[Moment]:
    """Align to word boundaries, enforce length, drop overlaps (with each other and with already-cut moments)."""
    words = [w for s in segments for w in s["words"]]
    out: list[Moment] = []
    taken = list(used or [])
    for m in sorted(moments, key=lambda m: -m.score):
        if words:
            starts = [w["start"] for w in words if abs(w["start"] - m.start) < 2.0]
            ends = [w["end"] for w in words if abs(w["end"] - m.end) < 2.0]
            if starts:
                m.start = min(starts, key=lambda t: abs(t - m.start))
            if ends:
                m.end = min(ends, key=lambda t: abs(t - m.end))
        m.start = max(0.0, m.start - 0.15)
        m.end = m.end + 0.35
        if m.end - m.start > max_s:
            m.end = m.start + max_s
        if m.end - m.start < min_s:
            continue
        if any(m.start < o.end and o.start < m.end for o in out):
            continue
        if any(m.start < e and s < m.end for s, e in taken):
            continue
        out.append(m)
    return out


def describe_hotspots(twitch: list[tuple[float, int]], replay: list[tuple[float, float]],
                      loud: list[tuple[float, float]]) -> list[str]:
    lines = [f"- {_fmt(t)} ({t:.0f}s): viewers clipped this ({v:,} views)" for t, v in twitch[:15]]
    lines += [f"- {_fmt(t)} ({t:.0f}s): YouTube 'most replayed' (intensity {v:.2f})" for t, v in replay[:10]]
    lines += [f"- {_fmt(t)} ({t:.0f}s): audio spike (z={z})" for t, z in loud[:10]]
    return lines
