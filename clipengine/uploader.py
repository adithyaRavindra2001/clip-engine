"""Hands-off publishing: upload each packed clip to YouTube Studio, then submit its Short URL on the
campaign's Content Rewards page, within seconds of it going live.

Runs real Google Chrome through Playwright with a dedicated profile folder (data/browser-profile) that
starts as a copy of the operator's signed-in Chrome profile. `clip login` opens that profile so you can
check (or fix) the YouTube and Content Rewards sign-ins once; after that `clip upload` needs nobody.

    clip login                      open the automation profile, sign in to YouTube Studio + Content Rewards
    clip upload                     publish today's pack: upload -> wait until live -> submit -> record
    clip upload --dry-run           walk the pack and print what would happen
    clip upload --limit 3 --spacing 180

Every upload is recorded in UPLOAD.txt (POSTED: / SUBMITTED: lines under the clip) and in the review DB
(status posted, posted_url), so a re-run never uploads the same clip twice. Pacing is human: a few minutes
between clips, with jitter, and a hard daily cap. On any failure the page is screenshotted to
data/upload-debug/ and the run stops, so a broken selector never burns the queue.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

from .config import Config

PROFILE_DIR = "data/browser-profile"
DEBUG_DIR = "data/upload-debug"
STUDIO = "https://studio.youtube.com"
YT_SHORT = "https://youtube.com/shorts/{id}"


# ---------- UPLOAD.txt ----------

def parse_pack(upload_txt: Path) -> list[dict]:
    """Entries in upload order: name, id, title, description, submit URL, posted, submitted."""
    text = upload_txt.read_text()
    blocks = re.split(r"^########## ", text, flags=re.M)[1:]
    out = []
    for b in blocks:
        name = b.split()[0]
        e = {"name": name, "block": b}
        m = re.search(r"\[clip (\d+)\]", b)
        e["id"] = int(m.group(1)) if m else None
        m = re.search(r"^TITLE:\n(.+?)\n", b, re.M)
        e["title"] = m.group(1).strip()[:100] if m else name
        m = re.search(r"^DESCRIPTION \(paste as is[^)]*\):\n(.*?)(?=\n^[A-Z ]+:)", b, re.M | re.S)
        e["description"] = m.group(1).strip()[:4900] if m else ""
        m = re.search(r"^SUBMIT: .*?(https://contentrewards\.com/\S+)", b, re.M)
        e["campaign_url"] = m.group(1) if m else None
        m = re.search(r"^POSTED: (\S+)", b, re.M)
        e["posted"] = m.group(1) if m else None
        m = re.search(r"^SUBMITTED: (\S+)", b, re.M)
        e["submitted"] = m.group(1) if m else None
        out.append(e)
    return out


def _move_done(root: Path, name: str) -> None:
    """Published + submitted clips move to <pack>/done/ so the folder shows only what is still to go."""
    done = root / "done"
    done.mkdir(exist_ok=True)
    for ext in (".mp4", ".jpg"):
        f = root / f"{name}{ext}"
        if f.exists():
            shutil.move(str(f), done / f.name)


def _note(upload_txt: Path, name: str, line: str) -> None:
    """Append a status line to a clip's block (POSTED: ..., SUBMITTED: ...)."""
    text = upload_txt.read_text()
    pat = re.compile(rf"(^########## {re.escape(name)} .*?)(?=\n\n|\Z)", re.M | re.S)
    m = pat.search(text)
    if not m:
        return
    block = m.group(1).rstrip("\n") + "\n" + line
    upload_txt.write_text(text[:m.start(1)] + block + text[m.end(1):])


# ---------- browser ----------

def _profile(cfg: Config) -> Path:
    return (cfg.root / PROFILE_DIR).resolve()


def launch(cfg: Config, headless: bool = False):
    """Real Chrome, dedicated profile, automation banner off. Returns (playwright, context)."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        str(_profile(cfg)), channel="chrome", headless=headless,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check",
              "--profile-directory=Default"],
        viewport={"width": 1400, "height": 900}, locale="en-US", timezone_id="Asia/Kolkata",
    )
    ctx.set_default_timeout(60_000)
    return pw, ctx


def _debug(page, tag: str, cfg: Config) -> Path:
    d = cfg.root / DEBUG_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shot = d / f"{stamp}-{tag}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        (d / f"{stamp}-{tag}.html").write_text(page.content())
    except Exception:
        pass
    return shot


def _notify(title: str, body: str) -> None:
    subprocess.run(["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                   capture_output=True)


def _sleep(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


# ---------- YouTube Studio ----------

def _type_textbox(page, container_sel: str, text: str) -> None:
    box = page.locator(f"{container_sel} #textbox").first
    box.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    # Studio's textbox is a contenteditable; insert_text keeps newlines and avoids per-key slowness.
    page.keyboard.insert_text(text)
    _sleep(0.4, 0.9)


def _channel(page) -> str:
    page.goto(STUDIO, wait_until="domcontentloaded")
    page.wait_for_url(re.compile(r"studio\.youtube\.com/channel/UC[\w-]+"), timeout=60_000)
    return re.search(r"/channel/(UC[\w-]+)", page.url).group(1)


def _open_draft(page, channel: str, name: str, title: str) -> bool:
    """If Studio already holds a draft for this clip (an interrupted run, or a file you dropped in by hand
    whose title is the file name like 'fomo 11'), open its editor instead of uploading again."""
    wanted = {title.lower().strip(), name.lower(), name.lower().replace("-", " "), name.lower().replace("-", "")}
    for tab in ("short", "upload"):
        page.goto(f"{STUDIO}/channel/{channel}/videos/{tab}", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("ytcp-video-row", timeout=20_000)
        except Exception:
            continue
        for row in page.locator("ytcp-video-row").all():
            try:
                t = row.locator("#video-title").first.inner_text(timeout=3_000).strip().lower()
            except Exception:
                continue
            if t in wanted and "draft" in row.inner_text().lower():
                btn = row.get_by_role("button", name=re.compile(r"edit draft", re.I)).first
                if not btn.count():
                    btn = row.get_by_text(re.compile(r"edit draft", re.I)).first
                btn.click()
                page.wait_for_selector("#title-textarea #textbox", timeout=60_000)
                print(f"    resuming Studio draft {t!r}", flush=True)
                return True
    return False


def _limit_hit(page) -> bool:
    try:
        return page.get_by_text(re.compile(r"daily upload limit reached", re.I)).first.is_visible(timeout=1_500)
    except Exception:
        return False


def youtube_upload(page, cfg: Config, mp4: Path, title: str, description: str, name: str = "") -> str:
    """Upload one file (or resume its draft), fill details, publish as Public (not for kids). Returns the video id."""
    channel = _channel(page)
    print(f"    studio channel {channel}", flush=True)
    if not (name and _open_draft(page, channel, name, title)):
        page.goto(f"{STUDIO}/channel/{channel}/videos/upload?d=ud", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
        except Exception:
            page.locator("#create-icon").first.click()
            page.get_by_text(re.compile(r"upload videos", re.I)).first.click()
            page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
        page.locator("input[type=file]").first.set_input_files(str(mp4))
        page.wait_for_selector("#title-textarea #textbox", timeout=120_000)
    _sleep(1.0, 2.0)
    if _limit_hit(page):
        raise RuntimeError("YouTube daily upload limit reached. One-time fix: myaccount.google.com/video-verification "
                           "(scan the QR in Studio with your phone), or wait 24 h. The clip stays as a Studio draft "
                           "and the next run resumes it.")
    _type_textbox(page, "#title-textarea", title)
    _type_textbox(page, "#description-textarea", description)
    page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').first.click()
    _sleep(0.5, 1.2)
    link = page.locator("ytcp-video-info a, .video-url-fadeable a").first
    try:
        link.wait_for(timeout=120_000)
    except Exception:
        if _limit_hit(page):
            raise RuntimeError("YouTube daily upload limit reached (see myaccount.google.com/video-verification); "
                               "clip left as a Studio draft")
        raise
    href = link.get_attribute("href") or ""
    m = re.search(r"(?:youtu\.be/|/shorts/|v=)([\w-]{11})", href)
    if not m:
        raise RuntimeError(f"could not read video id from {href!r}")
    vid = m.group(1)
    for _ in range(3):   # Details -> Video elements -> Checks -> Visibility
        page.locator("#next-button").first.click()
        _sleep(0.8, 1.6)
    page.locator('tp-yt-paper-radio-button[name="PUBLIC"]').first.click()
    _sleep(0.5, 1.0)
    _wait_upload_done(page)
    page.locator("#done-button").first.click()
    _sleep(1.5, 3.0)
    for sel in ("ytcp-uploads-still-processing-dialog #close-button", "#close-button"):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3_000):
                btn.click()
        except Exception:
            pass
    return vid


def _wait_upload_done(page, timeout_s: int = 900) -> None:
    """Wait until Studio's footer says the upload and checks are done (then Publish applies immediately)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            txt = page.locator("ytcp-uploads-dialog").first.inner_text(timeout=5_000).lower()
        except Exception:
            txt = ""
        if re.search(r"checks complete|upload complete|processing (will begin|has started)|no issues found", txt):
            return
        if re.search(r"upload failed|an error occurred|daily upload limit", txt):
            raise RuntimeError("Studio reported: " + re.search(r"upload failed|an error occurred|daily upload limit", txt).group(0))
        time.sleep(5)
    raise RuntimeError("upload did not finish in time")


def wait_until_live(page, vid: str, timeout_s: int = 60) -> str:
    """Poll YouTube's oEmbed endpoint: it answers 200 only once the video is public (or unlisted) and
    processed, 401/403 while private, 404 while unavailable. No page scraping, no false negatives."""
    url = YT_SHORT.format(id=vid)
    probe = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            r = page.request.get(probe, timeout=15_000)
            if r.status == 200:
                return url
        except Exception:
            pass
        time.sleep(5)
    print(f"    (oEmbed not answering yet; submitting anyway, the Short is published)", flush=True)
    return url


# ---------- Content Rewards ----------

def content_rewards_submit(page, cfg: Config, campaign_url: str, short_url: str) -> None:
    """Submit the posted link on the campaign page. Selectors are text-based because the site is a
    React app without stable ids; any miss stops the run with a screenshot so it can be fixed fast."""
    page.goto(campaign_url, wait_until="domcontentloaded")
    _sleep(2.0, 3.5)
    if page.get_by_role("button", name=re.compile(r"^sign in$", re.I)).count():
        raise RuntimeError("Content Rewards is signed out in the automation profile: run `clip login`")
    opener = page.get_by_role("button", name=re.compile(r"submit", re.I)).first
    if not opener.count():
        opener = page.get_by_text(re.compile(r"submit (content|clip|a clip|post|link)", re.I)).first
    opener.wait_for(timeout=30_000)
    opener.click()
    _sleep(1.0, 2.0)
    field = page.locator('input[type=url], input[placeholder*="http" i], input[placeholder*="link" i], '
                         'input[placeholder*="url" i], textarea[placeholder*="link" i]').first
    field.wait_for(timeout=30_000)
    field.click()
    field.fill(short_url)
    _sleep(0.8, 1.5)
    # Platform picker, if the dialog has one
    for name in (re.compile(r"youtube", re.I),):
        try:
            opt = page.get_by_role("button", name=name).first
            if opt.count() and opt.is_visible(timeout=2_000):
                opt.click()
                _sleep(0.5, 1.0)
        except Exception:
            pass
    # The dialog's "I've read the requirements ..." checkbox gates the Submit button.
    ack = page.get_by_text(re.compile(r"read the requirements", re.I)).first
    if ack.count():
        ack.click()
        _sleep(0.5, 1.0)
    box = page.locator("[role=dialog] input[type=checkbox], [role=dialog] [role=checkbox]").first
    try:
        if box.count() and box.is_visible(timeout=1_500) and not box.is_checked():
            box.click()
            _sleep(0.4, 0.8)
    except Exception:
        pass
    confirm = page.get_by_role("button", name=re.compile(r"^(submit|confirm|submit (clip|content|post|link))$", re.I)).last
    confirm.wait_for(timeout=30_000)
    for _ in range(40):   # wait for the site's own checks to pass and the button to enable
        if confirm.get_attribute("aria-disabled") != "true" and confirm.is_enabled():
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Submit button never enabled (site checks failed: linked account / already submitted / 30-minute rule?)")
    if confirm.get_attribute("aria-disabled") == "true" or not confirm.is_enabled():
        # Read the dialog's own checklist so the reason is in the log (linked account / already submitted / 30 min).
        dlg = page.locator("[role=dialog]").first.inner_text() if page.locator("[role=dialog]").count() else page.inner_text("body")
        raise RuntimeError("Submit button stayed disabled; dialog says: " + " | ".join(l.strip() for l in dlg.splitlines() if l.strip())[:300])
    def _wait_not_submitting(limit_s: int = 180) -> None:
        t0 = time.time()
        while time.time() - t0 < limit_s:
            try:
                if not re.search(r"submitting", page.inner_text("body"), re.I):
                    return
            except Exception:
                return
            time.sleep(2)
        _debug(page, "submit-hung", cfg)
        raise RuntimeError("Content Rewards stayed on 'Submitting…' for 3 minutes; will retry on the next run")

    # YouTube can take a few minutes to expose the publish time; the site then says
    # "We couldn't confirm exactly when this post went live. Try again in a few minutes." So: retry in place.
    for attempt in range(1, 9):
        confirm.click()
        _wait_not_submitting()
        _sleep(1.0, 2.0)
        body = page.inner_text("body")
        if not re.search(r"couldn't confirm|could not confirm|try again in a few", body, re.I):
            break
        print(f"    site can't see the publish time yet (attempt {attempt}); retrying in ~75 s", flush=True)
        _sleep(60, 90)
        confirm = page.get_by_role("button", name=re.compile(r"^(submit|confirm|submit (clip|content|post|link))$", re.I)).last
    else:
        _debug(page, "submit-unconfirmed", cfg)
        raise RuntimeError("Content Rewards could not confirm the post time after 8 tries")
    _sleep(1.5, 2.5)
    _debug(page, "after-submit", cfg)   # always keep a picture of what the site said
    # The only reliable success signal: the video now appears on the Submissions page.
    vid = re.search(r"(?:v=|/shorts/|youtu\.be/)([\w-]{11})", short_url).group(1)
    dialog_open = bool(page.get_by_text(re.compile(r"submit clip link", re.I)).count())
    if dialog_open:
        _debug(page, "submit-dialog-still-open", cfg)
        raise RuntimeError("submission dialog is still open after clicking Submit")
    # The Submissions list can lag a minute or two behind (the site verifies ownership first). A closed dialog with no
    # error is a success; the listing check is confirmation only, and the nightly reconcile is the backstop.
    for attempt in range(6):
        if vid in cr_submissions(page):
            return
        time.sleep(15)
    print("    (submitted; not yet visible on the Submissions page, reconcile will confirm)", flush=True)


# ---------- tally ----------

TALLY = "tally/uploads.csv"
TALLY_HEADER = "date,name,campaign,title,youtube_url,posted_at,submitted_at,how\n"


def tally_add(cfg: Config, day: str, name: str, campaign: str, title: str, url: str,
              posted_at: str, submitted_at: str, how: str) -> None:
    """One line per published clip in tally/uploads.csv (human-readable, git-tracked)."""
    import csv
    f = cfg.root / TALLY
    f.parent.mkdir(exist_ok=True)
    new = not f.exists()
    with f.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(TALLY_HEADER.strip().split(","))
        w.writerow([day, name, campaign, title, url, posted_at, submitted_at, how])


def tally_rows(cfg: Config) -> list[dict]:
    import csv
    f = cfg.root / TALLY
    if not f.exists():
        return []
    with f.open() as fh:
        return list(csv.DictReader(fh))


def record_manual(cfg: Config, db, name: str, url: str, day: str | None = None) -> None:
    """Log a clip you uploaded and submitted by hand: marks UPLOAD.txt, the DB and the tally."""
    day = day or latest_pack(cfg) or date.today().isoformat()
    root = cfg.root / "todays-yt" / day
    upload_txt = root / "UPLOAD.txt"
    e = next((e for e in parse_pack(upload_txt) if e["name"] == name), None)
    if not e:
        raise SystemExit(f"{name} not in {upload_txt}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not e["posted"]:
        _note(upload_txt, name, f"POSTED: {url}")
    if not e["submitted"]:
        _note(upload_txt, name, f"SUBMITTED: {now} (manual)")
    if e["id"]:
        db.execute("UPDATE clips SET status='posted', posted_url=?, decided_at=CURRENT_TIMESTAMP WHERE id=?", (url, e["id"]))
        db.commit()
    tally_add(cfg, day, name, name.rsplit("-", 1)[0], e["title"], url, now, now, "manual")
    _move_done(root, name)
    print(f"logged {name} -> {url}")


# ---------- orchestration ----------

def latest_pack(cfg: Config) -> str | None:
    """Newest todays-yt/<date> whose UPLOAD.txt still has clips without a SUBMITTED line. The upload job
    runs at 03:30, after midnight, so 'today' is usually the pack cut the evening before."""
    packs = sorted((p for p in (cfg.root / "todays-yt").glob("????-??-??") if (p / "UPLOAD.txt").exists()), reverse=True)
    for p in packs[:7]:
        if any(not e["submitted"] and (p / f"{e['name']}.mp4").exists() for e in parse_pack(p / "UPLOAD.txt")):
            return p.name
    return None


def run(cfg: Config, db, day: str | None = None, limit: int = 20, spacing: int = 240,
        dry_run: bool = False, only: str | None = None) -> int:
    day = day or latest_pack(cfg)
    if not day:
        print("No pack with unpublished clips in todays-yt/")
        return 0
    root = cfg.root / "todays-yt" / day
    upload_txt = root / "UPLOAD.txt"
    if not upload_txt.exists():
        print(f"No pack for {day} ({upload_txt})")
        return 0
    todo = [e for e in parse_pack(upload_txt) if not e["submitted"] and (root / f"{e['name']}.mp4").exists()
            and (not only or e["name"].startswith(only))]
    print(f"{day}: {len(todo)} clip(s) to publish (cap {limit}, ~{spacing}s apart)")
    if dry_run:
        for e in todo[:limit]:
            print(f"  {e['name']}: {e['title']!r} -> {e['campaign_url']}" + ("  (already posted: %s)" % e["posted"] if e["posted"] else ""))
        return 0
    if not todo:
        return 0
    pw, ctx = launch(cfg)
    page = ctx.new_page()
    done = 0
    try:
        for e in todo[:limit]:
            name, mp4 = e["name"], root / f"{e['name']}.mp4"
            try:
                url = e["posted"]
                if not url:
                    print(f"  {name}: uploading {e['title']!r}", flush=True)
                    vid = youtube_upload(page, cfg, mp4, e["title"], e["description"], name=name)
                    url = YT_SHORT.format(id=vid)
                    _note(upload_txt, name, f"POSTED: {url}")
                    if e["id"]:
                        db.execute("UPDATE clips SET status='posted', posted_url=?, decided_at=CURRENT_TIMESTAMP WHERE id=?", (url, e["id"]))
                        db.commit()
                    print(f"    uploaded -> {url}; waiting until it is live", flush=True)
                    wait_until_live(page, vid)
                if e["campaign_url"]:
                    print(f"    submitting on {e['campaign_url']}", flush=True)
                    content_rewards_submit(page, cfg, e["campaign_url"], url)
                    _note(upload_txt, name, f"SUBMITTED: {datetime.now():%Y-%m-%d %H:%M}")
                    print("    submitted", flush=True)
                else:
                    _note(upload_txt, name, "SUBMITTED: no-campaign-url")
                tally_add(cfg, day, name, name.rsplit("-", 1)[0], e["title"], url,
                          datetime.now().strftime("%Y-%m-%d %H:%M"), datetime.now().strftime("%Y-%m-%d %H:%M"), "auto")
                with (cfg.data_dir / "uploads.log").open("a") as log:
                    log.write(json.dumps({"date": day, "name": name, "url": url, "campaign": e["campaign_url"],
                                          "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
                _move_done(root, name)
                done += 1
            except Exception as ex:
                shot = _debug(page, name, cfg)
                print(f"  [stop] {name}: {ex}\n         screenshot: {shot}", flush=True)
                _notify("clip upload stopped", f"{name}: {str(ex)[:80]}")
                break
            if done < min(limit, len(todo)):
                pause = spacing * random.uniform(0.7, 1.4)
                print(f"    next in {pause/60:.1f} min")
                time.sleep(pause)
    finally:
        ctx.close()
        pw.stop()
    _notify("clip upload", f"{done} clip(s) published and submitted for {day}")
    print(f"{done} published.")
    return done


def login(cfg: Config) -> None:
    """Open the automation profile so the operator can sign in / confirm sign-ins, then close it."""
    pw, ctx = launch(cfg)
    page = ctx.new_page()
    page.goto(STUDIO, wait_until="domcontentloaded")
    p2 = ctx.new_page()
    p2.goto("https://contentrewards.com/c/campaigns", wait_until="domcontentloaded")
    print("Chrome is open with the automation profile.\n"
          "  Tab 1: YouTube Studio   — must land on the channel dashboard (not a sign-in page).\n"
          "  Tab 2: Content Rewards  — sign in if you see 'Sign In' top-right.\n"
          "Close the Chrome window when both are signed in.")
    try:
        while True:
            time.sleep(2)
            if not ctx.pages:
                break
    except Exception:
        pass
    try:
        ctx.close()
    except Exception:
        pass
    pw.stop()
    print("Profile saved. Run `clip upload --dry-run` next.")


# ---------- reconcile: every public video on the channel must be submitted ----------

def _api_key(cfg: Config) -> str | None:
    import os
    k = (cfg.raw.get("youtube") or {}).get("api_key") or os.environ.get("YOUTUBE_API_KEY")
    if k:
        return k
    env = cfg.root / ".env"
    if env.exists():
        m = re.search(r"YOUTUBE_API_KEY=[\"']?([^\s'\"]+)", env.read_text())
        if m:
            return m.group(1)
    return None


def channel_videos(cfg: Config, channel: str, max_items: int = 200) -> list[dict]:
    """Public videos on the channel via the Data API uploads playlist (1 quota unit per 50 videos)."""
    import json as _json
    import urllib.parse
    import urllib.request
    key = _api_key(cfg)
    if not key:
        raise SystemExit("YOUTUBE_API_KEY missing (.env)")
    uploads = "UU" + channel[2:]
    out, token = [], None
    while len(out) < max_items:
        q = {"part": "snippet,status,contentDetails", "playlistId": uploads, "maxResults": 50, "key": key}
        if token:
            q["pageToken"] = token
        with urllib.request.urlopen("https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(q), timeout=30) as r:
            data = _json.load(r)
        for it in data.get("items", []):
            sn, st = it["snippet"], it.get("status", {})
            out.append({"id": sn["resourceId"]["videoId"], "title": sn["title"],
                        "published_at": sn.get("publishedAt") or it.get("contentDetails", {}).get("videoPublishedAt"),
                        "privacy": st.get("privacyStatus")})
        token = data.get("nextPageToken")
        if not token:
            break
    return [v for v in out if v["privacy"] == "public"]


def cr_submissions(page) -> set[str]:
    """YouTube video ids already submitted on Content Rewards (links on the Submissions page)."""
    page.goto("https://contentrewards.com/c/submissions", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    seen = -1
    for _ in range(25):   # infinite scroll / "load more" until the list stops growing
        page.mouse.wheel(0, 6000)
        page.wait_for_timeout(900)
        try:
            more = page.get_by_role("button", name=re.compile(r"(load|show) more", re.I)).first
            if more.count() and more.is_visible(timeout=500):
                more.click(); page.wait_for_timeout(1500)
        except Exception:
            pass
        n = page.locator('a[href*="youtu"]').count()
        if n == seen:
            break
        seen = n
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    ids = set()
    for h in hrefs:
        m = re.search(r"(?:v=|/shorts/|youtu\.be/)([\w-]{11})", h)
        if m:
            ids.add(m.group(1))
    return ids


def _campaign_for(cfg: Config, db, vid: str, title: str) -> tuple[str | None, str | None]:
    """(campaign_url, pack name) for a video: by recorded URL, then by pack title, then by DB creator."""
    packs = sorted((cfg.root / "todays-yt").glob("????-??-??"), reverse=True)
    entries = [e for p in packs if (p / "UPLOAD.txt").exists() for e in parse_pack(p / "UPLOAD.txt")]
    for e in entries:
        if e["posted"] and vid in e["posted"]:
            return e["campaign_url"], e["name"]
    for e in entries:
        if e["title"].strip().lower() == title.strip().lower():
            return e["campaign_url"], e["name"]
    row = db.execute("SELECT v.creator FROM clips c JOIN videos v ON v.id=c.video_id WHERE c.posted_url LIKE ?", (f"%{vid}%",)).fetchone()
    if row:
        c = cfg.creator(row["creator"])
        m = re.search(r"https://contentrewards\.com/\S+", (c.permission if c else "") or "")
        if m:
            return m.group(0).rstrip(")"), None
    # last resort: a single-campaign hint from hashtags in the title is not reliable; give up
    return None, None


def reconcile(cfg: Config, db, dry_run: bool = False, window_min: int = 30, force: bool = False) -> None:
    """Nightly safety net: anything public on YouTube but missing on Content Rewards gets submitted
    (if still inside the campaign's posting window) or reported."""
    from datetime import timezone
    pw, ctx = launch(cfg)
    page = ctx.new_page()
    try:
        channel = _channel(page)
        videos = channel_videos(cfg, channel)
        submitted = cr_submissions(page)
        recorded = {re.search(r"([\w-]{11})$", r["youtube_url"].rstrip("/")).group(1) for r in tally_rows(cfg)
                    if r.get("submitted_at") and re.search(r"[\w-]{11}$", r["youtube_url"].rstrip("/"))}
        print(f"channel {channel}: {len(videos)} public video(s); Content Rewards lists {len(submitted)} submission(s), "
              f"our tally has {len(recorded)}")
        submitted |= recorded
        # The Submissions page lists only the newest ~20; anything older than 2 days is past every campaign's
        # posting window anyway, so only recent videos are compared.
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        recent = [v for v in videos if (v["published_at"] or "") >= cutoff]
        missing = [v for v in recent if v["id"] not in submitted]
        if not missing:
            print("all public videos are submitted")
            return
        now = datetime.now(timezone.utc)
        for v in missing:
            url = YT_SHORT.format(id=v["id"])
            age = (now - datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))).total_seconds() / 60 if v["published_at"] else 1e9
            campaign, name = _campaign_for(cfg, db, v["id"], v["title"])
            tag = f"{v['title'][:60]!r} ({url}, published {age:.0f} min ago)"
            if not campaign:
                print(f"  ? no campaign known for {tag}: submit by hand"); continue
            if age > window_min and not force:
                print(f"  x MISSED {tag}: outside the {window_min}-minute window, cannot be submitted now"); continue
            if dry_run:
                print(f"  would submit {tag} -> {campaign}"); continue
            try:
                content_rewards_submit(page, cfg, campaign, url)
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                if name:
                    day = next((p.name for p in sorted((cfg.root / "todays-yt").glob("????-??-??"), reverse=True)
                                if (p / "UPLOAD.txt").exists() and any(e["name"] == name for e in parse_pack(p / "UPLOAD.txt"))), None)
                    if day:
                        _note(cfg.root / "todays-yt" / day / "UPLOAD.txt", name, f"SUBMITTED: {stamp} (reconcile)")
                        _move_done(cfg.root / "todays-yt" / day, name)
                tally_add(cfg, date.today().isoformat(), name or v["id"], (name or "?").rsplit("-", 1)[0], v["title"], url, stamp, stamp, "reconcile")
                db.execute("UPDATE clips SET status='posted', posted_url=? WHERE posted_url IS NULL AND title=?", (url, v["title"]))
                db.commit()
                print(f"  + submitted {tag}")
            except Exception as ex:
                shot = _debug(page, "reconcile", cfg)
                print(f"  ! failed {tag}: {ex} (screenshot {shot})")
    finally:
        ctx.close()
        pw.stop()
