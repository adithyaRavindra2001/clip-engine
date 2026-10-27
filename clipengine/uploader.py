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


def youtube_upload(page, cfg: Config, mp4: Path, title: str, description: str) -> str:
    """Upload one file, fill details, publish as Public (not for kids). Returns the video id."""
    page.goto(f"{STUDIO}/channel/UC/videos/upload?d=ud", wait_until="domcontentloaded")
    page.wait_for_selector("ytcp-uploads-dialog, input[type=file]", timeout=90_000)
    page.locator("input[type=file]").first.set_input_files(str(mp4))
    # Details step
    page.wait_for_selector("#title-textarea #textbox", timeout=120_000)
    _sleep(1.0, 2.0)
    _type_textbox(page, "#title-textarea", title)
    _type_textbox(page, "#description-textarea", description)
    page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').first.click()
    _sleep(0.5, 1.2)
    # The video link appears in the dialog as soon as the id is assigned.
    link = page.locator("ytcp-video-info a, .video-url-fadeable a").first
    link.wait_for(timeout=120_000)
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
    # Let the bytes finish before closing the dialog, so "Public" applies the moment processing ends.
    _wait_upload_done(page)
    page.locator("#done-button").first.click()
    _sleep(1.5, 3.0)
    # Studio sometimes shows a "Video published" / close dialog
    for sel in ("ytcp-uploads-still-processing-dialog #close-button", "#close-button"):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3_000):
                btn.click()
        except Exception:
            pass
    return vid


def _wait_upload_done(page, timeout_s: int = 900) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        txt = ""
        try:
            txt = page.locator("ytcp-video-upload-progress .progress-label, ytcp-video-upload-progress span").first.inner_text(timeout=5_000)
        except Exception:
            pass
        low = txt.lower()
        if "complete" in low or "checks complete" in low or "processing" in low and "upload" not in low:
            return
        if "error" in low or "failed" in low:
            raise RuntimeError(f"upload failed: {txt}")
        time.sleep(5)
    raise RuntimeError("upload did not finish in time")


def wait_until_live(page, vid: str, timeout_s: int = 900) -> str:
    """Poll the Short's public URL until it plays (processing done, visibility applied)."""
    url = YT_SHORT.format(id=vid)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            page.goto(url, wait_until="domcontentloaded")
            body = page.content()
            if "Video unavailable" not in body and "is private" not in body and "isn't available" not in body \
                    and ("shorts" in page.url or "watch?v=" in page.url):
                return url
        except Exception:
            pass
        time.sleep(20)
    raise RuntimeError(f"{url} not live after {timeout_s}s")


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
    confirm = page.get_by_role("button", name=re.compile(r"^(submit|confirm|submit (clip|content|post|link))$", re.I)).last
    confirm.wait_for(timeout=30_000)
    confirm.click()
    _sleep(2.0, 3.5)
    body = page.content().lower()
    if "error" in body and "submitted" not in body:
        raise RuntimeError("submission form reported an error")


# ---------- orchestration ----------

def run(cfg: Config, db, day: str | None = None, limit: int = 20, spacing: int = 240,
        dry_run: bool = False, only: str | None = None) -> int:
    day = day or date.today().isoformat()
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
                    print(f"  {name}: uploading {e['title']!r}")
                    vid = youtube_upload(page, cfg, mp4, e["title"], e["description"])
                    url = YT_SHORT.format(id=vid)
                    _note(upload_txt, name, f"POSTED: {url}")
                    if e["id"]:
                        db.execute("UPDATE clips SET status='posted', posted_url=?, decided_at=CURRENT_TIMESTAMP WHERE id=?", (url, e["id"]))
                        db.commit()
                    print(f"    uploaded -> {url}; waiting until it is live")
                    wait_until_live(page, vid)
                if e["campaign_url"]:
                    print(f"    submitting on {e['campaign_url']}")
                    content_rewards_submit(page, cfg, e["campaign_url"], url)
                    _note(upload_txt, name, f"SUBMITTED: {datetime.now():%Y-%m-%d %H:%M}")
                    print("    submitted")
                else:
                    _note(upload_txt, name, "SUBMITTED: no-campaign-url")
                with (cfg.data_dir / "uploads.log").open("a") as log:
                    log.write(json.dumps({"date": day, "name": name, "url": url, "campaign": e["campaign_url"],
                                          "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
                done += 1
            except Exception as ex:
                shot = _debug(page, name, cfg)
                print(f"  [stop] {name}: {ex}\n         screenshot: {shot}")
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
