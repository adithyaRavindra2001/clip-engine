"""clip — command line entry point.

  clip daily                     discover fresh VODs from allowed creators, clip them, write prospects report
  clip run URL --creator NAME    clip one VOD / podcast (URL or local file)
  clip discover                  prospects report only
  clip morning                   auto-pick today's uploads -> todays-yt/<date>/<campaign>-NN/ (vid.mp4, info.txt, thumbnail.jpg)
  clip review                    open the local review page
  clip learn                     update the style guide from your decisions + real views
  clip stats                     quick performance summary
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import discover, learn, morning, pipeline, review, store
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
    # 1. Campaigns with fixed footage: cut NEW moments from the least-used source, best-paying campaign first.
    for c in [c for c in creators if c.sources]:
        stock = store.pending_count(db, c.name)
        if stock >= enough:
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
            if stock >= enough:
                print(f"{c.name}: {stock} drafts in stock, skipping today's cut")
                continue
            new = [v for v in vods if v.creator == c.name and not store.video_seen(db, v.url)][:args.max_videos]
            print(f"{c.name}: {len(new)} new VOD(s) to process.")
            for v in new:
                try:
                    total += pipeline.process(cfg, db, v.url, v.creator, twitch_hotspots=v.hotspots)
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
    print((root / "SUMMARY.txt").read_text())
    print(f"Pack: {root}")


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


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="clip", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=".", help="folder containing config.yaml and creators.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="clip one VOD or local file")
    r.add_argument("url")
    r.add_argument("--creator", required=True)
    r.add_argument("--force", action="store_true", help="reprocess even if already done")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("daily", help="discover + clip + prospects report (schedule this)")
    d.add_argument("--max-videos", type=int, default=1)
    d.add_argument("--no-prospects", action="store_true")
    d.set_defaults(fn=cmd_daily)

    sub.add_parser("discover", help="prospects report").set_defaults(fn=cmd_discover)

    m = sub.add_parser("morning", help="auto-select today's best clips into todays-yt/<date>/<campaign>-NN/ with info.txt + thumbnail")
    m.add_argument("--date", default=None)
    m.add_argument("--limit", type=int, default=None, help="clips PER CAMPAIGN (default: morning.per_campaign)")
    m.add_argument("--min-qa", type=int, default=None)
    m.add_argument("--creator", default=None, help="pack only this campaign (appends to today's folder)")
    m.set_defaults(fn=cmd_morning)

    v = sub.add_parser("review", help="open the review page")
    v.add_argument("--port", type=int, default=8765)
    v.set_defaults(fn=cmd_review)

    sub.add_parser("learn", help="update the style guide").set_defaults(fn=cmd_learn)
    sub.add_parser("stats", help="performance summary").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    cfg = Config(Path(args.root).resolve())
    db = store.connect(cfg.data_dir)
    args.fn(cfg, db, args)


if __name__ == "__main__":
    sys.exit(main())
