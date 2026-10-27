"""The morning pack: pick today's best clips automatically and lay them out for one-click uploading.

    todays-yt/<date>/fomo-01/vid.mp4        the clip   (folder = campaign slug + number within that campaign)
                             thumbnail.jpg  sharpest face frame + hook text
                             info.txt       TITLE / DESCRIPTION / HASHTAGS / where to submit / checklist
                     SUMMARY.txt            the day's list, in upload order, with why each was chosen

Selection = Claude's performance score blended with the frame-level QA score; clips that fail QA are
rejected with the reason recorded, so the learning loop still sees every decision.
"""
from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

from . import qa
from .config import Config, Creator

GENERIC_TAGS = ["#shorts"]


def hashtags(caption: str, creator) -> list[str]:
    tags = re.findall(r"#\w+", caption)
    for t in (creator.required_tags if creator else []):
        if t.startswith("#") and t not in tags:
            tags.append(t)
    for t in GENERIC_TAGS:
        if t not in tags:
            tags.append(t)
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower()); out.append(t)
    return out[:12]


def build(cfg: Config, db, day: str | None = None, limit: int = 5, min_qa: int = 60,
          per_video: int | None = None, only: str | None = None) -> Path:
    """`limit` is PER CAMPAIGN: every allowed campaign with pending clips gets up to `limit` of its best.
    Running twice on the same day APPENDS (numbering continues per campaign), so campaigns can be packed in stages."""
    day = day or date.today().isoformat()
    root = cfg.root / "todays-yt" / day
    per_video = per_video or limit
    rows = db.execute(
        "SELECT c.*, v.creator, v.url FROM clips c JOIN videos v ON v.id = c.video_id "
        "WHERE c.status = 'pending' ORDER BY c.score DESC").fetchall()
    assessed = []
    for r in rows:
        clip = Path(r["path"])
        creator = cfg.creator(r["creator"])
        if only and (not creator or creator.name.lower() != only.lower()):
            continue
        # Only campaign clips go out: never test material, never anyone without permission.
        if not clip.exists() or not creator or not creator.allowed or "TEST ONLY" in (creator.permission or ""):
            continue
        res = qa.assess(clip, r["hook"])
        assessed.append((r, res))
        db.execute("UPDATE clips SET qa = ?, qa_reasons = ? WHERE id = ?",
                   (res["qa"], "; ".join(res["reasons"]), r["id"]))
    db.commit()

    ranked = sorted(assessed, key=lambda t: -(0.6 * t[0]["score"] + 0.4 * t[1]["qa"]))
    chosen, per_src, per_campaign = [], {}, {}
    for r, res in ranked:
        if res["qa"] < min_qa:
            db.execute("UPDATE clips SET status='rejected', reason=?, decided_at=CURRENT_TIMESTAMP WHERE id=?",
                       ("auto-QA: " + "; ".join(res["reasons"]), r["id"]))
            continue
        if per_campaign.get(r["creator"], 0) >= limit:
            continue
        if per_src.get(r["video_id"], 0) >= per_video:
            continue  # spread across sources so a day isn't one stage four times
        chosen.append((r, res))
        per_src[r["video_id"]] = per_src.get(r["video_id"], 0) + 1
        per_campaign[r["creator"]] = per_campaign.get(r["creator"], 0) + 1
    db.commit()
    # Best-paying campaign first in the upload order; within a campaign, best clip first.
    chosen.sort(key=lambda t: (-(cfg.creator(t[0]["creator"]).cpm if cfg.creator(t[0]["creator"]) else 0),
                               t[0]["creator"], -(0.6 * t[0]["score"] + 0.4 * t[1]["qa"])))

    root.mkdir(parents=True, exist_ok=True)
    # Folders are <campaign slug>-NN; numbering continues per campaign when appending to today's pack.
    existing = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"[a-z0-9]+-\d+", p.name)]
    counter = {}
    for p in existing:
        slug_, n = p.name.rsplit("-", 1)
        counter[slug_] = max(counter.get(slug_, 0), int(n))
    summary = [f"TODAY'S UPLOADS — {day}", "",
               "Upload in this order, spaced through the evening. Copy TITLE and DESCRIPTION from each info.txt.",
               "Submit every URL on its campaign page right after posting (Reach campaigns: within 30 minutes).", ""]
    if existing:
        summary = ["", f"=== added later ({len(existing)} clips already packed above) ===", ""]
    pending_rules = sorted({r["creator"] for r, _ in chosen
                            if "PENDING" in ((cfg.creator(r["creator"]) or Creator("", "", "", "")).campaign_rules or "")})
    if pending_rules:
        summary += ["!! RULES NOT YET CONFIRMED for: " + ", ".join(pending_rules)
                    + ". Open each campaign's reference files on Content Rewards and check the caption rules "
                      "(required mentions/hashtags) before posting these.", ""]
    last_campaign = None
    for r, res in chosen:
        creator = cfg.creator(r["creator"])
        if r["creator"] != last_campaign:
            summary.append(f"--- {r['creator']} (${creator.cpm:.2f} per 1K views) ---")
            last_campaign = r["creator"]
        counter[creator.slug] = counter.get(creator.slug, 0) + 1
        name = f"{creator.slug}-{counter[creator.slug]:02d}"
        folder = root / name
        folder.mkdir(exist_ok=True)
        shutil.copy2(r["path"], folder / "vid.mp4")
        src = Path(r["path"]).with_name("src.mp4")
        opts = creator.source_options(r["url"]) if creator else {}
        qa.thumbnail(Path(r["path"]), r["hook"], folder / "thumbnail.jpg", frame=res["_thumb"],
                     accent=cfg["brand"]["accent"],
                     clean={"src": src, "t": res["thumb_t"], "layout": creator.layout if creator else "auto",
                            "facecam": creator.facecam if creator else None, **opts} if src.exists() else None)
        tags = hashtags(r["caption"] or "", creator)
        desc = (r["caption"] or "").strip()
        for t in (creator.required_tags if creator else []):
            if t not in desc:
                desc += f" {t}"
        submit = creator.permission if creator else ""
        warn = ""
        if creator and "PENDING" in (creator.campaign_rules or ""):
            warn = ("!! CAMPAIGN RULES NOT CONFIRMED: open this campaign's reference files on Content Rewards and check "
                    "the required mention/hashtag before posting. Add anything missing to the description.\n\n")
        (folder / "info.txt").write_text(
            warn
            + f"TITLE:\n{r['title']}\n\n"
            f"DESCRIPTION (paste as is):\n{desc}\n\n"
            f"HASHTAGS:\n{' '.join(tags)}\n\n"
            f"ON-SCREEN HOOK: {r['hook']}\n"
            f"LENGTH: {res['duration']}s   SCORE: {r['score']}   QA: {res['qa']}"
            + (f"   NOTES: {'; '.join(res['reasons'])}" if res["reasons"] else "") + "\n"
            f"CREATOR / CAMPAIGN: {r['creator']} — {submit}\n"
            f"SOURCE: {r['url']} @ {r['start']:.0f}s-{r['end']:.0f}s\n\n"
            "UPLOAD:\n"
            "- YouTube Studio > Create > Upload > vid.mp4. Title + description from above. Not for kids. Public.\n"
            "- Copy the Short's URL and submit it on the campaign page (link above) straight away.\n"
            "- Paste the URL into the review page's Approved tab so views get tracked.\n")
        db.execute("UPDATE clips SET status='approved', reason='auto-selected for morning pack', decided_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        summary.append(f"{name}: {r['title']}  [score {r['score']}, QA {res['qa']}, {res['duration']}s]  — {r['why']}")
    db.commit()
    left = [t for t in ranked if t[1]["qa"] >= min_qa and t[0]["id"] not in {c[0]["id"] for c in chosen}]
    summary += ["", f"{len(chosen)} selected, {len(left)} good clips held for tomorrow, "
                f"{sum(1 for _, res in assessed if res['qa'] < min_qa)} rejected by QA."]
    sp = root / "SUMMARY.txt"
    prev = sp.read_text() if (existing and sp.exists()) else ""
    sp.write_text(prev + "\n".join(summary) + "\n")
    return root
