"""Weekly: Claude studies your approvals, rejections and real view counts, and rewrites the style guide
that steers future picks."""
from __future__ import annotations

import anthropic

from .config import Config

PROMPT = """Below is the track record of a short-form clipping channel: every clip the human approved or \
rejected (with their reasons) and, for posted clips, real view counts and earnings.

Write a concise house style guide (max 300 words, bullet points) for the editor who picks moments. Cover: \
which kinds of moments and hooks perform, which to avoid, ideal length, and creator-specific notes. Base \
every point on the evidence below; say "not enough data yet" where that's true. Output only the guide.

Current guide:
{current}

Track record:
{records}"""


def update_style_guide(cfg: Config, db) -> str:
    rows = db.execute(
        "SELECT c.status, c.title, c.hook, c.reason, c.views, c.earnings, c.score, c.end - c.start AS len, v.creator "
        "FROM clips c JOIN videos v ON v.id = c.video_id WHERE c.status IN ('approved','rejected','posted') "
        "ORDER BY c.decided_at DESC LIMIT 300").fetchall()
    if len(rows) < 10:
        return "Not enough decisions yet (need 10+). Keep reviewing."
    records = "\n".join(
        f"- {r['status']} | {r['creator']} | {r['len']:.0f}s | predicted {r['score']} | "
        f"views={r['views'] if r['views'] is not None else '?'} | ${r['earnings'] or 0:.2f} | "
        f"\"{r['title']}\" | hook \"{r['hook']}\" | {r['reason'] or ''}" for r in rows)
    current = cfg.style_guide_path.read_text() if cfg.style_guide_path.exists() else "(none yet)"

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=cfg["claude"]["model"],
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": cfg["claude"]["effort"]},
        messages=[{"role": "user", "content": PROMPT.format(current=current, records=records)}],
    )
    if resp.stop_reason == "refusal":
        return "Claude declined to update the guide; kept the old one."
    guide = "".join(b.text for b in resp.content if b.type == "text").strip()
    cfg.style_guide_path.write_text(guide + "\n")
    return guide
