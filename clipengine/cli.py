"""clip — command line entry point.

  clip daily                     discover fresh VODs from allowed creators, clip them, write prospects report
  clip run URL --creator NAME    clip one VOD / podcast (URL or local file)
  clip discover                  prospects report only
  clip morning                   auto-pick today's uploads -> todays-yt/<date>/<campaign>-NN.mp4 + .jpg, one UPLOAD.txt
  clip upload                    publish today's pack: YouTube Studio upload + Content Rewards submission
  clip quota                     today's pack vs the 5-per-campaign target (--short: names still below it)
  clip health                    morning audit: packed/published/submitted per campaign, launchd jobs, log errors
  clip login                     open the automation Chrome profile to sign in once
  clip tally                     everything published so far (tally/uploads.csv)
  clip reconcile                 safety net: submit public videos missing on Content Rewards
  clip campaigns                 keep 5 campaigns live: budgets, discovery, Claude ranking, auto-join + onboard
  clip posted NAME URL           log a clip you uploaded by hand
  clip review                    open the local review page
  clip learn                     update the style guide from your decisions + real views
  clip stats                     quick performance summary
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import campaigns, discover, learn, morning, pipeline, quota, review, store, uploader
from .config import Config


def cmd_run(cfg, db, args):
    n = pipeline.process(cfg, db, args.url, args.creator, force=args.force)
    print(f"\n{n} draft clips ready. Review them with: clip review")


def cmd_daily(cfg, db, args):
    """Every allowed campaign, every day: `--max-videos` is per campaign. Campaigns that already hold plenty of
    unreviewed drafts (morning.per_campaign x morning.stock_factor) are skipped so the queue stays fresh."""
    total = 0
    m = cfg["morning"]
    enough = int(m["per_campaign"]) * int(m["stock_factor"])
    creators = sorted([c for c in cfg.creators() if c.allowed and "TEST ONLY" not in (c.permission or "")],
                      key=lambda c: -c.cpm)
    only = getattr(args, "only", None)
    if only:
        creators = [c for c in creators if c.name.lower() == only.lower()]
        if not creators:
            raise SystemExit(f"no allowed campaign named {only!r}")
    force = getattr(args, "force", False)
    # 1. Campaigns with fixed footage: cut NEW moments from the least-used source, best-paying campaign first.
    for c in [c for c in creators if c.sources]:
        stock = store.pending_count(db, c.name)
        if stock >= enough and not force:
            print(f"{c.name}: {stock} drafts in stock, skipping today's cut")
            continue
        for _ in range(args.max_videos):
            src = min(c.source_urls, key=lambda u: store.clip_count(db, u))
            print(f"{c.name} (${c.cpm:.2f} CPM): cutting new moments from {src}")
            try:
                total += pipeline.process(cfg, db, src, c.name, force=True)
            except SystemExit as e:
                print(f"  [waiting] {e}")
            except Exception as e:
                print(f"  [error] {src}: {e}")
    # 2. Everyone else: discover fresh uploads from their channels.
    rest = [c for c in creators if not c.sources]
    if rest:
        print("Discovering fresh VODs from allowed creators…")
        vods = discover.fresh_vods(cfg)
        for c in rest:
            stock = store.pending_count(db, c.name)
            if stock >= enough and not force:
                print(f"{c.name}: {stock} drafts in stock, skipping today's cut")
                continue
            mine = [v for v in vods if v.creator == c.name]
            new = [v for v in mine if not store.video_seen(db, v.url)][:args.max_videos]
            if not new and force and mine:
                # Top-up with nothing new on the channel: ask for NEW moments from the freshest VOD instead.
                new = mine[:1]
                print(f"{c.name}: no unseen VOD, re-cutting new moments from the newest one")
            print(f"{c.name}: {len(new)} new VOD(s) to process.")
            for v in new:
                try:
                    total += pipeline.process(cfg, db, v.url, v.creator, twitch_hotspots=v.hotspots, force=force)
                except SystemExit as e:
                    print(f"  [waiting] {e}")
                except Exception as e:
                    print(f"  [error] {v.url}: {e}")
    if not args.no_prospects:
        cmd_discover(cfg, db, args)
    print(f"\nDone: {total} new draft clips. Review them with: clip review")


def cmd_discover(cfg, db, args):
    report = discover.prospects_report(cfg)
    out = cfg.data_dir / "reports" / f"prospects-{datetime.now():%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(f"Prospects report: {out}")


def cmd_morning(cfg, db, args):
    m = cfg["morning"]
    root = morning.build(cfg, db, args.date, limit=args.limit or int(m["per_campaign"]),
                         min_qa=args.min_qa or int(m["min_qa"]), only=args.creator)
    print((root / "UPLOAD.txt").read_text())
    print(f"Pack: {root}")


def cmd_upload(cfg, db, args):
    uploader.run(cfg, db, args.date, limit=args.limit, spacing=args.spacing, dry_run=args.dry_run, only=args.only)


def cmd_quota(cfg, db, args):
    short = quota.shortfall(cfg, args.date)
    if args.short:
        for name in short:
            print(name)
        return
    target = int(cfg["morning"]["per_campaign"])
    have = quota.packed(cfg, args.date)
    for c in quota._active(cfg):
        n = have.get(c.slug, 0)
        print(f"  {c.name:<18} {n}/{target}" + ("" if n >= target else f"   needs {target - n} more"))
    print("quota met" if not short else f"short: {', '.join(f'{k} {v}' for k, v in short.items())}")


def cmd_health(cfg, db, args):
    ok, text = quota.health(cfg, args.date, notify=not args.no_notify)
    print(text)
    if not ok:
        sys.exit(1)


def cmd_login(cfg, db, args):
    uploader.login(cfg)


def cmd_campaigns(cfg, db, args):
    campaigns.run(cfg, db, min_active=args.min_active, auto_join=args.auto_join, dry_run=args.dry_run)


def cmd_reconcile(cfg, db, args):
    uploader.reconcile(cfg, db, dry_run=args.dry_run, force=args.force)


def cmd_tally(cfg, db, args):
    rows = uploader.tally_rows(cfg)
    if not rows:
        print("Nothing published yet (tally/uploads.csv is empty).")
        return
    per = {}
    for r in rows:
        per[r["campaign"]] = per.get(r["campaign"], 0) + 1
    print(f"{'date':<11}{'name':<16}{'how':<7}{'url':<40} title")
    for r in rows:
        print(f"{r['date']:<11}{r['name']:<16}{r['how']:<7}{r['youtube_url']:<40} {r['title'][:50]}")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(per.items())) + f"  — {len(rows)} total")


def cmd_posted(cfg, db, args):
    uploader.record_manual(cfg, db, args.name, args.url, args.date)


def cmd_review(cfg, db, args):
    review.serve(cfg, args.port)


def cmd_learn(cfg, db, args):
    print(learn.update_style_guide(cfg, db))


def cmd_stats(cfg, db, args):
    q = db.execute(
        "SELECT v.creator, COUNT(*) n, SUM(c.status IN ('approved','posted')) ok, SUM(c.status='rejected') bad, "
        "SUM(COALESCE(c.views,0)) views, SUM(COALESCE(c.earnings,0)) money "
        "FROM clips c JOIN videos v ON v.id = c.video_id GROUP BY v.creator ORDER BY money DESC").fetchall()
    if not q:
        print("No clips yet.")
        return
    print(f"{'creator':<22}{'drafts':>8}{'approved':>10}{'rejected':>10}{'views':>12}{'earned $':>10}")
    for r in q:
        print(f"{r['creator']:<22}{r['n']:>8}{r['ok'] or 0:>10}{r['bad'] or 0:>10}{r['views']:>12,}{r['money']:>10.2f}")


def _project_root() -> Path:
    """The clip-engine checkout this package lives in (so `clip` works from any directory), else the cwd."""
    here = Path(__file__).resolve().parent.parent
    if (here / "config.yaml").exists():
        return here
    cwd = Path.cwd().resolve()
    if (cwd / "config.yaml").exists():
        return cwd
    raise SystemExit(f"config.yaml not found in {here} or {cwd}; pass --root")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="clip", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None, help="folder containing config.yaml (default: the project the command is installed in)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="clip one VOD or local file")
    r.add_argument("url")
    r.add_argument("--creator", required=True)
    r.add_argument("--force", action="store_true", help="reprocess even if already done")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("daily", help="discover + clip + prospects report (schedule this)")
    d.add_argument("--max-videos", type=int, default=1)
    d.add_argument("--no-prospects", action="store_true")
    d.add_argument("--only", default=None, help="one campaign (creators.yaml name), used by the quota top-up")
    d.add_argument("--force", action="store_true", help="cut even with drafts in stock; re-cut the newest VOD if nothing is new")
    d.set_defaults(fn=cmd_daily)

    q = sub.add_parser("quota", help="today's pack vs the per-campaign target (morning.per_campaign)")
    q.add_argument("--date", default=None)
    q.add_argument("--short", action="store_true", help="print only the names of campaigns still below target, one per line")
    q.set_defaults(fn=cmd_quota)

    h = sub.add_parser("health", help="morning audit of yesterday's pack: packed/published/submitted per campaign + launchd jobs")
    h.add_argument("--date", default=None, help="pack date to audit (default: yesterday)")
    h.add_argument("--no-notify", action="store_true")
    h.set_defaults(fn=cmd_health)

    sub.add_parser("discover", help="prospects report").set_defaults(fn=cmd_discover)

    m = sub.add_parser("morning", help="auto-select today's best clips into todays-yt/<date>/ as <campaign>-NN.mp4 + .jpg with one UPLOAD.txt")
    m.add_argument("--date", default=None)
    m.add_argument("--limit", type=int, default=None, help="clips PER CAMPAIGN (default: morning.per_campaign)")
    m.add_argument("--min-qa", type=int, default=None)
    m.add_argument("--creator", default=None, help="pack only this campaign (appends to today's folder)")
    m.set_defaults(fn=cmd_morning)

    u = sub.add_parser("upload", help="publish today's pack through Chrome (YouTube Studio + Content Rewards)")
    u.add_argument("--date", default=None)
    u.add_argument("--limit", type=int, default=20, help="hard cap per run")
    u.add_argument("--spacing", type=int, default=240, help="seconds between clips (jittered)")
    u.add_argument("--only", default=None, help="only clips whose name starts with this, e.g. kai")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(fn=cmd_upload)

    sub.add_parser("login", help="open the automation Chrome profile to sign in to YouTube + Content Rewards").set_defaults(fn=cmd_login)

    sub.add_parser("tally", help="list everything published (tally/uploads.csv)").set_defaults(fn=cmd_tally)

    ca = sub.add_parser("campaigns", help="check active campaigns' budgets, rank new ones, optionally auto-join to keep N live")
    ca.add_argument("--min-active", type=int, default=5)
    ca.add_argument("--auto-join", action="store_true", help="join the best new campaigns until --min-active are live and onboard them")
    ca.add_argument("--dry-run", action="store_true", help="report only; never mark ended, never join")
    ca.set_defaults(fn=cmd_campaigns)

    rc = sub.add_parser("reconcile", help="submit any public YouTube video that is missing on Content Rewards (30-min window)")
    rc.add_argument("--dry-run", action="store_true")
    rc.add_argument("--force", action="store_true", help="try to submit even videos older than the 30-minute window")
    rc.set_defaults(fn=cmd_reconcile)

    po = sub.add_parser("posted", help="log a clip you uploaded by hand: clip posted fomo-01 https://youtube.com/shorts/...")
    po.add_argument("name"); po.add_argument("url"); po.add_argument("--date", default=None)
    po.set_defaults(fn=cmd_posted)

    v = sub.add_parser("review", help="open the review page")
    v.add_argument("--port", type=int, default=8765)
    v.set_defaults(fn=cmd_review)

    sub.add_parser("learn", help="update the style guide").set_defaults(fn=cmd_learn)
    sub.add_parser("stats", help="performance summary").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else _project_root()
    cfg = Config(root)
    db = store.connect(cfg.data_dir)
    args.fn(cfg, db, args)


if __name__ == "__main__":
    sys.exit(main())
