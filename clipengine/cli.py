"""clip — command line entry point.

  clip daily                     discover fresh VODs from allowed creators, clip them, write prospects report
  clip run URL --creator NAME    clip one VOD / podcast (URL or local file)
  clip discover                  prospects report only
  clip review                    open the local review page
  clip learn                     update the style guide from your decisions + real views
  clip stats                     quick performance summary
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import discover, learn, pipeline, review, store
from .config import Config


def cmd_run(cfg, db, args):
    n = pipeline.process(cfg, db, args.url, args.creator, force=args.force)
    print(f"\n{n} draft clips ready. Review them with: clip review")


def cmd_daily(cfg, db, args):
    total, budget = 0, args.max_videos
    # 1. Campaigns with fixed footage: cut NEW moments from the least-used source, best-paying campaign first.
    fixed = sorted([c for c in cfg.creators() if c.allowed and c.sources], key=lambda c: -c.cpm)
    for c in fixed:
        if budget <= 0:
            break
        src = min(c.sources, key=lambda u: store.clip_count(db, u))
        print(f"{c.name} (${c.cpm:.2f} CPM): cutting new moments from {src}")
        try:
            total += pipeline.process(cfg, db, src, c.name, force=True)
            budget -= 1
        except SystemExit as e:
            print(f"  [waiting] {e}")
            budget -= 1
        except Exception as e:
            print(f"  [error] {src}: {e}")
    # 2. Everyone else: discover fresh uploads.
    if budget > 0:
        print("Discovering fresh VODs from allowed creators…")
        vods = discover.fresh_vods(cfg)
        new = [v for v in vods if not store.video_seen(db, v.url)][:budget]
        print(f"{len(vods)} recent VODs found, {len(new)} new to process.")
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
