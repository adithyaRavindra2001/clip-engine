"""Campaign supply: keep at least N paying campaigns live at all times.

    clip campaigns                 status of the active campaigns (budget left, still accepting) + a ranked list of new ones
    clip campaigns --auto-join     also join the best new ones until `min_active` are live, and onboard them into creators.yaml

Daily flow (scheduled before the nightly cut):
  1. STATUS   open each active campaign page in the signed-in Chrome profile; read budget used / remaining and whether
              it still accepts clips. Exhausted or closed ones get `status: ended` in creators.yaml (never deleted).
  2. DISCOVER scrape contentrewards.com's campaign listing; keep YouTube-eligible campaigns with budget left that we
              have not joined or parked; drop gambling/casino and anything that excludes our channel.
  3. RANK     headless `claude -p` (Max plan, no API key) ranks the shortlist for expected $ for a one-person YouTube
              Shorts clipper in India, with red flags.
  4. JOIN     with --auto-join: click Join on the top picks until min_active is reached, read the now-visible brief and
              reference materials, and ask Claude to draft the creators.yaml entry (sources, rules, tags, edit style).
              Entries whose sources Claude could pin to YouTube URLs go in as `allowed`; anything uncertain goes in as
              `prospect` with a note, so the nightly cut never runs on guesswork.
  5. REPORT   data/reports/campaigns-<date>.md + a Mac notification.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

import yaml

from .config import Config
from . import uploader

LISTING = "https://contentrewards.com/c/discover"
BLOCKLIST = re.compile(r"casino|gambl|slots|bet(ting)?\b|stake\b|shuffle|crypto casino|onlyfans|adult", re.I)


# ---------- helpers ----------

def _text(page) -> str:
    t = page.inner_text("body")
    return re.sub(r"\n{2,}", "\n", t)


def _money(s: str) -> float:
    m = re.search(r"\$([\d,]+(?:\.\d+)?)\s*([KkMm])?", s)
    if not m:
        return 0.0
    v = float(m.group(1).replace(",", ""))
    return v * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)


def _claude(prompt: str, model: str = "claude-opus-5-5") -> str:
    r = subprocess.run(["claude", "-p", "--output-format", "text", "--model", model, prompt],
                       capture_output=True, text=True, timeout=600)
    return r.stdout.strip()


def _json_from(text: str) -> dict | list | None:
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


# ---------- 1. status of active campaigns ----------

def campaign_status(page, url: str) -> dict:
    """Budget used / total and whether the campaign still accepts clips, read off the campaign page."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    t = _text(page)
    out = {"url": url, "title": page.title().split("|")[0].strip(), "accepting": None, "used": None, "total": None}
    m = re.search(r"\$([\d,.]+)\s*/\s*\$([\d,.]+[KkMm]?)", t)
    if m:
        out["used"], out["total"] = _money("$" + m.group(1)), _money("$" + m.group(2))
    out["accepting"] = bool(re.search(r"accepting clips|submit clip", t, re.I)) and not re.search(
        r"campaign (has )?ended|no longer accepting|closed|budget (is )?exhausted|pool (has )?ended", t, re.I)
    out["left"] = (out["total"] - out["used"]) if out["total"] else None
    return out


def check_active(cfg: Config, page) -> list[dict]:
    rows = []
    for c in cfg.creators():
        if not c.allowed or "TEST ONLY" in (c.permission or ""):
            continue
        m = re.search(r"https://contentrewards\.com/c/campaigns/[\w-]+", c.permission or "")
        if not m:
            continue
        st = campaign_status(page, m.group(0))
        st["creator"] = c.name
        st["exhausted"] = (st["left"] is not None and st["total"] and st["left"] / st["total"] < 0.03) or st["accepting"] is False
        rows.append(st)
    return rows


def mark_ended(cfg: Config, names: list[str]) -> None:
    path = cfg.root / "creators.yaml"
    s = path.read_text()
    for n in names:
        s = re.sub(rf"(  - name: {re.escape(n)}\n(?:    .*\n)*?    status: )allowed", rf"\1ended   # pool exhausted / closed, {date.today()}", s, count=1)
    path.write_text(s)


# ---------- 2. discover ----------

CARD = re.compile(r"^(?P<brand>.+?) · (?P<age>\S+) (?P<title>.+?) \$(?P<used>[\d.,]+[KkMm]?) /\$(?P<total>[\d.,]+[KkMm]?) "
                  r"(?P<clippers>[\d.,]+[KkMm]?) \$(?P<cpm>[\d.]+)(?:–\$(?P<cpm_hi>[\d.]+))?/1K")


def _num(s: str) -> float:
    return _money("$" + s)


def discover(page, known_urls: set[str], limit: int = 150, min_left: float = 500, min_cpm: float = 0.75) -> list[dict]:
    """Every card on the Discover page, parsed, minus joined/parked ones and obvious non-starters."""
    page.goto(LISTING, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    seen_n = -1
    for _ in range(20):
        page.mouse.wheel(0, 6000)
        page.wait_for_timeout(700)
        n = page.locator('a[href*="/c/campaigns/"]').count()
        if n == seen_n:
            break
        seen_n = n
    cards = page.eval_on_selector_all(
        'a[href*="/c/campaigns/"]',
        "els => els.map(e => ({href: e.href, text: (e.innerText || '').replace(/\\s+/g, ' ').trim()}))")
    seen, out = set(), []
    for c in cards:
        url = c["href"].split("?")[0].rstrip("/")
        if url in seen or url in known_urls or not re.search(r"/c/campaigns/[\w-]{8,}$", url):
            continue
        seen.add(url)
        m = CARD.search(c["text"])
        if not m:
            continue
        d = m.groupdict()
        used, total = _num(d["used"]), _num(d["total"])
        cand = {"url": url, "brand": d["brand"], "title": d["title"], "age": d["age"], "used": used, "total": total,
                "left": total - used, "clippers": int(_num(d["clippers"])), "cpm": float(d["cpm"]),
                "card": c["text"][:300], "blocked": bool(BLOCKLIST.search(c["text"]))}
        if cand["blocked"] or cand["left"] < min_left or cand["cpm"] < min_cpm:
            continue
        out.append(cand)
    # best first: money left weighted by rate, discounted by competition, boosted when fresh (first-mover:
    # budget intact, few clippers) and penalised when the burn rate says it will be empty within ~3 days.
    for c in out:
        days = max(_age_days(c["age"]), 0.25)
        burn = c["used"] / days                      # $ per day
        days_left = c["left"] / burn if burn > 0 else 99
        c["days_left"] = round(days_left, 1)
        c["score"] = (c["cpm"] * min(c["left"], 20000) / (1 + (c["clippers"] or 0) / 200)
                      * (1.3 if days <= 2 else 1.0 if days <= 7 else 0.8)
                      * (0.5 if days_left < 3 else 1.0))
    out.sort(key=lambda c: -c["score"])
    return out[:limit]


def _age_days(age: str) -> float:
    """'3d' / '5h' / '2w' / '1mo' as shown on the Discover cards -> days."""
    m = re.match(r"(\d+(?:\.\d+)?)\s*(mo|m|w|d|h)", age or "")
    if not m:
        return 7.0
    n, u = float(m.group(1)), m.group(2)
    return n * {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "mo": 30}[u]


def enrich(page, cands: list[dict], max_pages: int = 15) -> list[dict]:
    """Open the top candidate pages for budget, platforms, CPM, clipper count, and the public brief text."""
    for c in cands[:max_pages]:
        try:
            page.goto(c["url"], wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            t = _text(page)
            c["title"] = page.title().split("|")[0].strip()
            m = re.search(r"\$([\d,.]+)\s*/\s*\$([\d,.]+[KkMm]?)", t)
            if m:
                c["used"], c["total"] = _money("$" + m.group(1)), _money("$" + m.group(2))
                c["left"] = c["total"] - c["used"]
            m = re.search(r"\$([\d.]+)\s*(?:/|per)\s*1K", t, re.I)
            if m:
                c["cpm"] = float(m.group(1))
            c["youtube"] = bool(re.search(r"\bYouTube\b", t))
            c["tiktok_only"] = bool(re.search(r"\bTikTok\b|\bInstagram\b", t)) and not c["youtube"]
            c["blocked"] = bool(BLOCKLIST.search(t))
            m = re.search(r"(\d[\d,]*)\s*(?:clippers|creators|members)", t, re.I)
            c["clippers"] = int(m.group(1).replace(",", "")) if m else None
            c["accepting"] = bool(re.search(r"accepting clips|join campaign", t, re.I))
            c["page_text"] = t[:4000]
        except Exception as ex:
            c["error"] = str(ex)[:120]
    return cands


# ---------- 3. rank with Claude ----------

RANK_PROMPT = """You pick clipping campaigns for a one-person YouTube Shorts clipper based in India, paid through Whop
Content Rewards. Channel: one general Shorts channel, English, audience mostly US/UK/CA/AU + India, a few thousand
views per clip on a good day, 5 clips per campaign per day. The clipper can only post on YouTube Shorts.

Rank the candidate campaigns below by expected money over the next two weeks. Consider: CPM, budget LEFT (not total),
whether YouTube Shorts is accepted, competition (clippers count), minimum views before payout, whether footage is
easy to get (a podcast or stream on YouTube beats "footage in a Drive folder"), and red flags: gambling/casino
promotion (never), TikTok/Instagram-only, creator-country restrictions, "post from a dedicated/new account" demands,
mandatory watermarks we don't have, required follower counts, audience rules we cannot meet.

Answer ONLY a JSON object:
{"ranked": [{"url": "...", "title": "...", "score": 0-100, "expected_usd_2w": number, "why": "one line",
             "red_flags": ["..."], "join": true/false}]}
"join" is true only when it is clearly YouTube-eligible, not blocked, and worth the clipper's 5 slots per day.

CANDIDATES:
"""

ONBOARD_PROMPT = """Turn this Content Rewards campaign brief into a creators.yaml entry for the clip-engine pipeline.
The pipeline can: pull YouTube videos by URL (fixed `sources`) or discover a channel's recent long uploads by `handle`;
render 9:16 Shorts with captions, a hook, punch-in zoom, optional watermark PNG (only if we have the file); append
`required_tags` to every description; pass `campaign_rules` verbatim to the clip picker; `edit_style` is "streamer"
(meme sounds allowed) or "podcast".

Return ONLY JSON:
{"name": "ShortName", "slug": "short", "platform": "youtube", "handle": "@channel or empty",
 "sources": ["https://www.youtube.com/watch?v=..." ...] or [],
 "cpm": number, "edit_style": "podcast|streamer", "required_tags": ["#tag", "@mention"],
 "campaign_rules": "the rules the picker must obey, compressed but complete",
 "music": true/false, "needs_watermark_file": true/false,
 "confidence": 0-100, "notes": "anything a human must do (download a watermark, link an account, etc.)"}
Put the exact YouTube source URLs if the brief or its links name them; if sources are only a Drive folder or a
channel with no specific videos, set sources [] and give the handle. Confidence < 70 if sources or required tags are unclear.

BRIEF:
"""


def rank(cands: list[dict]) -> list[dict]:
    short = [c for c in cands if c.get("page_text") and not c.get("blocked") and not c.get("tiktok_only") and c.get("youtube", True)]
    if not short:
        return []
    payload = "\n\n".join(
        f"URL: {c['url']}\nTITLE: {c.get('title', '')}\nCPM: {c.get('cpm')}  BUDGET LEFT: {c.get('left')} of {c.get('total')}  "
        f"CLIPPERS: {c.get('clippers')}  YOUTUBE: {c.get('youtube')}\nPAGE:\n{c.get('page_text', c.get('card', ''))[:2500]}"
        for c in short)
    out = _json_from(_claude(RANK_PROMPT + payload))
    ranked = (out or {}).get("ranked", []) if isinstance(out, dict) else []
    by_url = {c["url"]: c for c in short}
    for r in ranked:
        r.update({k: v for k, v in by_url.get(r.get("url"), {}).items() if k in ("cpm", "left", "total", "clippers")})
    return sorted(ranked, key=lambda r: -float(r.get("score") or 0))


# ---------- 4. join + onboard ----------

def join(page, url: str) -> bool:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if page.get_by_role("button", name=re.compile(r"^sign in$", re.I)).count():
        raise RuntimeError("Content Rewards is signed out in the automation profile (run `clip login`)")
    if re.search(r"submit clip", _text(page), re.I):
        return True   # already joined
    btn = page.get_by_role("button", name=re.compile(r"join campaign", re.I)).first
    if not btn.count():
        return False
    btn.scroll_into_view_if_needed()
    btn.click()
    page.wait_for_timeout(3000)
    # some campaigns show a requirements/acknowledge dialog
    for name in (re.compile(r"^(agree|accept|confirm|join|continue)$", re.I),):
        try:
            b = page.get_by_role("button", name=name).last
            if b.count() and b.is_visible(timeout=1500):
                b.click()
                page.wait_for_timeout(2500)
        except Exception:
            pass
    return bool(re.search(r"submit clip", _text(page), re.I))


def brief_text(page, url: str) -> str:
    """Everything visible after joining, plus the text of linked rules pages (reachclipping.com etc.)."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    for label in ("Show more", "Read more"):
        try:
            for b in page.get_by_text(re.compile(rf"^{label}$", re.I)).all()[:4]:
                if b.is_visible():
                    b.click(); page.wait_for_timeout(400)
        except Exception:
            pass
    t = _text(page)
    links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    extra = []
    for l in sorted({l for l in links if re.search(r"reachclipping|notion\.site|docs\.google\.com/document|\.pdf$|rules|brief", l, re.I)})[:3]:
        try:
            page.goto(l, wait_until="domcontentloaded"); page.wait_for_timeout(3000)
            extra.append(f"\n\n--- linked rules page {l} ---\n" + _text(page)[:6000])
        except Exception:
            pass
    yt = sorted({l for l in links if re.search(r"youtube\.com|youtu\.be", l)})
    return t[:8000] + "".join(extra) + ("\n\nYOUTUBE LINKS ON PAGE: " + " ".join(yt) if yt else "")


def onboard(cfg: Config, url: str, brief: str, cpm_hint: float | None) -> dict:
    out = _json_from(_claude(ONBOARD_PROMPT + brief)) or {}
    if not isinstance(out, dict) or not out.get("name"):
        return {"ok": False, "reason": "Claude returned no entry"}
    allowed = (out.get("confidence", 0) >= 70 and (out.get("sources") or out.get("handle"))
               and not out.get("needs_watermark_file"))
    entry = {
        "name": re.sub(r"[^A-Za-z0-9]", "", out["name"])[:30] or "Campaign",
        "slug": re.sub(r"[^a-z0-9]", "", (out.get("slug") or out["name"]).lower())[:12],
        "status": "allowed" if allowed else "prospect",
        "platform": "youtube",
        "handle": out.get("handle") or "",
        "permission": f"Whop Content Rewards campaign '{out.get('name')}', auto-joined {date.today()} — {url}",
        "campaign_rules": (out.get("campaign_rules") or "").strip() + ("" if allowed else f"\nPENDING: {out.get('notes', '')}"),
        "required_tags": [t for t in (out.get("required_tags") or []) if isinstance(t, str)][:8],
        "cpm": float(out.get("cpm") or cpm_hint or 0),
        "edit_style": out.get("edit_style") if out.get("edit_style") in ("streamer", "podcast") else "podcast",
        "music": bool(out.get("music", True)),
    }
    if out.get("sources"):
        entry["sources"] = [s for s in out["sources"] if isinstance(s, str) and "youtu" in s][:10]
    path = cfg.root / "creators.yaml"
    doc = yaml.safe_load(path.read_text()) or {}
    doc.setdefault("creators", []).append(entry)
    path.write_text(path.read_text().rstrip("\n") + "\n\n  # auto-onboarded " + str(date.today()) + " (clip campaigns)\n"
                    + "\n".join("  " + l if l.strip() else l for l in yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=110).splitlines()) + "\n")
    return {"ok": True, "entry": entry, "notes": out.get("notes", ""), "confidence": out.get("confidence")}


# ---------- 5. orchestration ----------

def run(cfg: Config, db, min_active: int = 5, auto_join: bool = False, dry_run: bool = False) -> Path:
    pw, ctx = uploader.launch(cfg)
    page = ctx.new_page()
    lines = [f"# Campaigns — {datetime.now():%Y-%m-%d %H:%M}", ""]
    try:
        active = check_active(cfg, page)
        lines.append("## Active")
        for a in active:
            left = f"${a['left']:,.0f} of ${a['total']:,.0f} left" if a["left"] is not None else "budget unknown"
            lines.append(f"- {a['creator']}: {left}; {'accepting' if a['accepting'] else 'NOT accepting'}"
                         + ("  **-> EXHAUSTED, marking ended**" if a["exhausted"] else ""))
        ended = [a["creator"] for a in active if a["exhausted"]]
        if ended and not dry_run:
            mark_ended(cfg, ended)
        live = [a for a in active if not a["exhausted"]]
        need = max(0, min_active - len(live))
        lines += ["", f"{len(live)} live, target {min_active} -> need {need} more", ""]

        known = {re.search(r"https://contentrewards\.com/c/campaigns/[\w-]+", c.permission or "").group(0)
                 for c in cfg.creators() if re.search(r"https://contentrewards\.com/c/campaigns/[\w-]+", c.permission or "")}
        cands = enrich(page, discover(page, known))
        ranked = rank(cands)
        lines.append("## New candidates (ranked by Claude)")
        for r in ranked[:12]:
            lines.append(f"- [{r.get('score')}] {r.get('title')} — ${r.get('cpm')}/1K, ~${r.get('left') or 0:,.0f} left, "
                         f"{r.get('clippers') or '?'} clippers — {r.get('why')}"
                         + (f"  RED FLAGS: {', '.join(r['red_flags'])}" if r.get("red_flags") else "")
                         + f"  {r['url']}")
        if not ranked:
            lines.append("- nothing eligible found")

        joined = []
        if auto_join and need and not dry_run:
            lines += ["", "## Joined + onboarded"]
            for r in [r for r in ranked if r.get("join")][:need]:
                try:
                    ok = join(page, r["url"])
                except RuntimeError as ex:
                    lines.append(f"- stopped: {ex}"); break
                if not ok:
                    lines.append(f"- {r.get('title')}: could not join (see data/upload-debug)"); uploader._debug(page, "join", cfg); continue
                res = onboard(cfg, r["url"], brief_text(page, r["url"]), r.get("cpm"))
                if res.get("ok"):
                    e = res["entry"]
                    lines.append(f"- {r.get('title')} -> creators.yaml as **{e['name']}** ({e['status']}, confidence {res.get('confidence')})"
                                 + (f"; notes: {res['notes']}" if res.get("notes") else ""))
                    joined.append(e["name"])
                else:
                    lines.append(f"- {r.get('title')}: joined but onboarding failed: {res.get('reason')}")
    finally:
        ctx.close(); pw.stop()
    out = cfg.data_dir / "reports" / f"campaigns-{date.today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    uploader._notify("clip campaigns", f"{len(live)} live" + (f", joined {len(joined)}" if joined else "") + (f", ended: {', '.join(ended)}" if ended else ""))
    print("\n".join(lines))
    return out
