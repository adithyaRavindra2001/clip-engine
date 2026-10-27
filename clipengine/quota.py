"""The daily quota: exactly morning.per_campaign clips per allowed campaign, every day.

  packed()     what today's pack already holds per campaign (root + done/, not held/)
  shortfall()  how many more each campaign still needs today
  health()     next-morning audit: packed vs published vs submitted, launchd jobs, last errors -> notification
"""
from __future__ import annotations

import re
import subprocess
from datetime import date, timedelta
from pathlib import Path

from .config import Config

NAME = re.compile(r"([a-z0-9]+)-(\d+)")


def _active(cfg: Config):
    return [c for c in cfg.creators() if c.allowed and "TEST ONLY" not in (c.permission or "")]


def pack_root(cfg: Config, day: str | None = None) -> Path:
    return cfg.root / "todays-yt" / (day or date.today().isoformat())


def packed(cfg: Config, day: str | None = None) -> dict[str, int]:
    """Clips per campaign slug in the day's pack that are (or were) going out: root + done/. held/ is excluded:
    those were withheld for breaking a rule and must be replaced."""
    root = pack_root(cfg, day)
    out: dict[str, int] = {}
    for sub in (root, root / "done"):
        for p in sub.glob("*.mp4"):
            m = NAME.fullmatch(p.stem)
            if m:
                out[m.group(1)] = out.get(m.group(1), 0) + 1
    return out


def shortfall(cfg: Config, day: str | None = None) -> dict[str, int]:
    """{creator name: clips still needed today} for every allowed campaign (0 entries omitted)."""
    target = int(cfg["morning"]["per_campaign"])
    have = packed(cfg, day)
    return {c.name: target - have.get(c.slug, 0) for c in _active(cfg) if have.get(c.slug, 0) < target}


def _jobs_loaded() -> dict[str, str]:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    except Exception:
        return {}
    jobs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith("com.clipengine."):
            jobs[parts[2].removeprefix("com.clipengine.")] = parts[1]  # last exit status
    return jobs


def health(cfg: Config, day: str | None = None, notify: bool = True) -> tuple[bool, str]:
    """Audit the pack cut on `day` (default: yesterday, since the cut is at 17:00 and uploads run overnight).
    OK means every campaign has exactly per_campaign clips, all published and all submitted."""
    from . import uploader
    day = day or (date.today() - timedelta(days=1)).isoformat()
    target = int(cfg["morning"]["per_campaign"])
    have = packed(cfg, day)
    rows = [r for r in uploader.tally_rows(cfg) if r["date"] == day]
    published = {}
    submitted = {}
    for r in rows:
        m = NAME.fullmatch(r["name"])
        if not m:
            continue
        published[m.group(1)] = published.get(m.group(1), 0) + 1
        if r.get("submitted_at"):
            submitted[m.group(1)] = submitted.get(m.group(1), 0) + 1
    problems = []
    lines = [f"clip-engine health for the {day} pack (target {target} per campaign)", ""]
    for c in _active(cfg):
        p, u, s = have.get(c.slug, 0), published.get(c.slug, 0), submitted.get(c.slug, 0)
        flag = "" if p == u == s == target else "  <-- PROBLEM"
        lines.append(f"  {c.name:<18} packed {p}  published {u}  submitted {s}{flag}")
        if flag:
            problems.append(f"{c.name} {p}/{u}/{s}")
    jobs = _jobs_loaded()
    lines.append("")
    for job in ("daily", "upload", "health", "review"):
        if job not in jobs:
            lines.append(f"  launchd job {job}: NOT LOADED (run scripts/install_schedule.sh)")
            problems.append(f"{job} job not loaded")
        elif jobs[job] not in ("0", "-"):
            lines.append(f"  launchd job {job}: last exit {jobs[job]}")
            problems.append(f"{job} job exit {jobs[job]}")
    logs = Path.home() / "Library" / "Logs" / "clipengine"
    for name in ("daily.log", "upload.log"):
        f = logs / name
        if f.exists():
            tail = f.read_text(errors="ignore").splitlines()[-200:]
            bad = [l for l in tail if re.search(r"operation not permitted|Traceback|\[stop\]|\[error\]|can't open", l)]
            if bad:
                lines.append(f"  {name}: {bad[-1].strip()[:140]}")
                problems.append(f"{name}: {bad[-1].strip()[:60]}")
    ok = not problems
    lines.append("")
    lines.append("ALL GOOD" if ok else "PROBLEMS: " + "; ".join(problems))
    text = "\n".join(lines)
    out = cfg.data_dir / "reports" / f"health-{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n")
    if notify:
        uploader._notify("clip-engine: " + ("all good" if ok else "ATTENTION"),
                         f"{day}: " + ("every campaign at quota, all submitted" if ok else "; ".join(problems)[:200]))
    return ok, text
