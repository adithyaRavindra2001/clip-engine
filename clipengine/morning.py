"""The morning pack: pick today's best clips automatically and lay them out for one-click uploading.

    todays-yt/<date>/fomo-01.mp4    the clip   (name = campaign slug + number within that campaign)
                     fomo-01.jpg    thumbnail: sharpest face frame + hook text
                     UPLOAD.txt     ONE file with every clip in upload order: TITLE / DESCRIPTION / HASHTAGS /
                                    where to submit / why it was picked

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

GENERIC_TAGS: list[str] = []   # YouTube: #shorts/#fyp/#viral add nothing; 3-5 topical tags + campaign tags only


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
        if t.lower() in ("#shorts", "#fyp", "#viral", "#foryou", "#trending"):
            continue
        if t.lower() not in seen:
            seen.add(t.lower()); out.append(t)
    return out[:5]


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
    # Files are <campaign slug>-NN.mp4; numbering continues per campaign when appending to today's pack.
    existing = [p for sub in (root, root / "done", root / "held") for p in sub.glob("*.mp4")
                if re.fullmatch(r"[a-z0-9]+-\d+", p.stem)]
    counter = {}
    for p in existing:
        slug_, n = p.stem.rsplit("-", 1)
        counter[slug_] = max(counter.get(slug_, 0), int(n))
    summary = [f"TODAY'S UPLOADS — {day}", "",
               "Upload in this order, spaced through the evening. For each clip: YouTube Studio > Create > Upload >",
               "<name>.mp4, paste TITLE and DESCRIPTION (hashtags are already in the description), thumbnail <name>.jpg,",
               "Not for kids, Public. Then copy the Short's URL, submit it on the campaign page straight away",
               "(Reach campaigns: within 30 minutes) and paste it into the review page's Approved tab so views get tracked.", ""]
    if existing:
        summary = ["", "", f"=== added later ({len(existing)} clips already packed above) ===", ""]
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
        shutil.copy2(r["path"], root / f"{name}.mp4")
        src = Path(r["path"]).with_name("src.mp4")
        opts = creator.source_options(r["url"]) if creator else {}
        qa.thumbnail(Path(r["path"]), r["hook"], root / f"{name}.jpg", frame=res["_thumb"],
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
                    "the required mention/hashtag before posting. Add anything missing to the description.\n")
        # Hashtags go into the description too: YouTube reads them from there, one paste does it all.
        extra = " ".join(t for t in tags if t not in desc)
        full_desc = (desc + ("\n\n" + extra if extra else "")).strip()
        db.execute("UPDATE clips SET status='approved', reason='auto-selected for morning pack', decided_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        summary += [
            f"########## {name}  ({name}.mp4, thumbnail {name}.jpg)  [score {r['score']}, QA {res['qa']}, {res['duration']}s]  [clip {r['id']}]",
            warn.rstrip("\n") if warn else None,
            f"TITLE:\n{r['title']}",
            f"DESCRIPTION (paste as is, hashtags included):\n{full_desc}",
            f"HASHTAGS: {' '.join(tags)}",
            f"HOOK ON SCREEN: {r['hook']}",
            f"WHY: {r['why']}",
            f"SUBMIT: {submit}",
            f"SOURCE: {r['url']} @ {r['start']:.0f}s-{r['end']:.0f}s"
            + (f"\nNOTES: {'; '.join(res['reasons'])}" if res["reasons"] else ""),
            "",
        ]
        summary = [x for x in summary if x is not None]
    db.commit()
    left = [t for t in ranked if t[1]["qa"] >= min_qa and t[0]["id"] not in {c[0]["id"] for c in chosen}]
    summary += ["", f"{len(chosen)} selected, {len(left)} good clips held for tomorrow, "
                f"{sum(1 for _, res in assessed if res['qa'] < min_qa)} rejected by QA."]
    sp = root / "UPLOAD.txt"
    prev = sp.read_text() if (existing and sp.exists()) else ""
    sp.write_text(prev + "\n".join(summary) + "\n")
    return root
