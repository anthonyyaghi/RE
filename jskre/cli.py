"""Command line interface.

    python -m jskre scrape --category apartment-for-sale
    python -m jskre details --limit 300
    python -m jskre analyze --top 50
    python -m jskre report
    python -m jskre changes --days 14
    python -m jskre status
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .analyze import Assumptions, CompsIndex, market_summary, rank_deals
from .config import load_config
from .db import Database
from .http import PoliteClient
from .report import write_deals_csv, write_html_report, write_market_csv
from .scrape import CATEGORIES, crawl_details, crawl_index


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jskre",
        description="Collect and analyse jskre.com listings for renovation-flip margin.",
    )
    parser.add_argument("--config", default="config.yml", help="path to config file")
    parser.add_argument("--db", default=None, help="override database path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="crawl listing index pages")
    p_scrape.add_argument(
        "--category", default=None, choices=sorted(CATEGORIES),
        help="which listing category to crawl (default: from config)",
    )
    p_scrape.add_argument("--max-pages", type=int, default=None)
    p_scrape.add_argument("--start-page", type=int, default=1)

    p_conf = sub.add_parser(
        "confidence", help="collect Confidence Real Estate listings (needs Playwright)"
    )
    p_conf.add_argument("--pages", type=int, default=None,
                        help="stop after this many pages (default: all)")
    p_conf.add_argument("--page-size", type=int, default=None,
                        help="listings per request; the server may cap this")
    p_conf.add_argument("--delay", type=float, default=None,
                        help="seconds between requests (default: 2)")
    p_conf.add_argument("--no-details", action="store_true",
                        help="skip the pass that fetches descriptions and photos")
    p_conf.add_argument("--headed", action="store_true", help="show the browser")
    p_conf.add_argument("--channel", default=None,
                        help="use an installed browser, e.g. chrome")
    p_conf.add_argument("--chromium", default=None, help="path to a Chromium binary")

    p_details = sub.add_parser("details", help="fetch detail pages for full text")
    p_details.add_argument("--limit", type=int, default=200)
    p_details.add_argument("--ref", action="append", help="specific reference(s)")

    p_analyze = sub.add_parser("analyze", help="rank flip candidates")
    p_analyze.add_argument("--top", type=int, default=25)
    p_analyze.add_argument("--town", default=None, help="filter to one town")
    p_analyze.add_argument(
        "--no-screens", action="store_true",
        help="show every listing, ignoring profit/ROI minimums",
    )
    p_analyze.add_argument("--csv", default=None, help="also write a CSV here")
    p_analyze.add_argument(
        "--condition", default=None,
        help="comma-separated condition labels to keep, e.g. "
             "'renovation_target,neutral' for the renovation thesis only",
    )

    p_report = sub.add_parser("report", help="write CSV + HTML report")
    p_report.add_argument("--outdir", default="out")
    p_report.add_argument("--top", type=int, default=100)
    p_report.add_argument(
        "--condition", default=None,
        help="comma-separated condition labels to keep (see `analyze --condition`)",
    )

    p_changes = sub.add_parser("changes", help="recent price cuts and delistings")
    p_changes.add_argument("--days", type=int, default=14)

    sub.add_parser("status", help="database summary")
    sub.add_parser(
        "repair", help="re-clean stored descriptions in place (no refetching)"
    )

    p_photos = sub.add_parser(
        "photos", help="photo-based condition assessment via the Claude API"
    )
    p_photos.add_argument(
        "action", choices=["estimate", "submit", "poll", "status"],
        help="estimate cost / submit a batch / poll+import results / show state",
    )
    p_photos.add_argument("--model", default=None, help="model id (default claude-opus-5)")
    p_photos.add_argument("--limit", type=int, default=None, help="cap listings per batch")

    p_export = sub.add_parser(
        "export", help="write portable snapshots of the data to exports/"
    )
    p_export.add_argument("--outdir", default="exports")

    p_restore = sub.add_parser(
        "restore", help="rebuild the database from exports/ (fresh clones)"
    )
    p_restore.add_argument("--outdir", default="exports")

    p_serve = sub.add_parser("serve", help="run the web UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument(
        "--open", action="store_true", help="open a browser window"
    )

    return parser


def _client(config: dict) -> PoliteClient:
    http_cfg = config.get("http", {}) or {}
    return PoliteClient(
        user_agent=http_cfg.get(
            "user_agent", "jskre-deal-finder/1.0 (personal market research)"
        ),
        delay=float(http_cfg.get("delay_seconds", 1.5)),
        jitter=float(http_cfg.get("jitter_seconds", 0.75)),
        timeout=float(http_cfg.get("timeout_seconds", 30)),
        max_retries=int(http_cfg.get("max_retries", 4)),
        respect_robots=bool(http_cfg.get("respect_robots", True)),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)
    db_path = args.db or config.get("database", {}).get("path", "data/jskre.db")
    assumptions = Assumptions.from_dict(config.get("assumptions"))
    types = config.get("analysis", {}).get("property_types") or None

    if args.command == "serve":
        from .server import serve

        if args.open:
            import threading
            import webbrowser

            url = f"http://{args.host}:{args.port}"
            threading.Timer(0.7, lambda: webbrowser.open(url)).start()
        serve(args.host, args.port, db_path, args.config)
        return 0

    with Database(db_path) as db:
        if args.command == "scrape":
            category = args.category or config.get("scrape", {}).get(
                "category", "for-sale"
            )
            result = crawl_index(
                db, _client(config), category,
                max_pages=args.max_pages, start_page=args.start_page,
                delist_after_missed_crawls=int(
                    config.get("scrape", {}).get("delist_after_missed_crawls", 2)
                ),
            )
            print(result.summary())
            if not result.complete:
                print(
                    "Partial crawl: delisting detection skipped "
                    "(needs a full sweep from page 1)."
                )
            return 0

        if args.command == "confidence":
            from .confidence_crawl import crawl as crawl_confidence, DEFAULT_DELAY, DEFAULT_PAGE_SIZE

            settings = config.get("confidence", {})
            result = crawl_confidence(
                db,
                pages=args.pages,
                page_size=args.page_size or int(settings.get("page_size", DEFAULT_PAGE_SIZE)),
                delay=args.delay if args.delay is not None
                else float(settings.get("delay_seconds", DEFAULT_DELAY)),
                details=not args.no_details,
                headed=args.headed,
                channel=args.channel,
                chromium=args.chromium,
            )
            print(result.summary())
            if not result.complete:
                print(
                    "Partial crawl: delisting detection skipped "
                    "(needs a sweep that reaches the last page)."
                )
            return 0

        if args.command == "details":
            counts = crawl_details(
                db, _client(config), limit=args.limit, refs=args.ref
            )
            print(
                f"detail pass: {counts['fetched']} fetched, "
                f"{counts['missing']} missing/gone, {counts['failed']} unparsed"
            )
            return 0

        if args.command == "analyze":
            rows = db.active_listings(types)
            if not rows:
                print("No listings in the database yet — run `scrape` first.")
                return 1
            comps = CompsIndex(rows, assumptions)
            if args.town:
                needle = args.town.casefold()
                rows = [r for r in rows if (r["town"] or "").casefold() == needle]
                if not rows:
                    print(f"No listings found in town {args.town!r}.")
                    return 1
            deals = rank_deals(
                rows, assumptions, comps=comps,
                apply_screens=not args.no_screens,
                conditions=_conditions(args.condition),
            )
            _print_deals(deals, args.top)
            if args.csv:
                path = write_deals_csv(deals, args.csv)
                print(f"\nWrote {len(deals)} rows to {path}")
            return 0

        if args.command == "report":
            rows = db.active_listings(types)
            if not rows:
                print("No listings in the database yet — run `scrape` first.")
                return 1
            comps = CompsIndex(rows, assumptions)
            deals = rank_deals(
                rows, assumptions, comps=comps,
                conditions=_conditions(args.condition),
            )
            market = market_summary(rows, assumptions)
            outdir = Path(args.outdir)
            deals_csv = write_deals_csv(deals, outdir / "flip_candidates.csv")
            market_csv = write_market_csv(market, outdir / "market_by_town.csv")
            html_path = write_html_report(
                deals, market, assumptions, db.stats(),
                outdir / "report.html", top_n=args.top,
            )
            print(f"{len(deals)} candidates passed screening.")
            print(f"  {deals_csv}\n  {market_csv}\n  {html_path}")
            return 0

        if args.command == "photos":
            from . import photos

            if args.action == "estimate":
                info = photos.estimate(db, limit=args.limit)
                print(f"pending listings with photos : {info['pending_listings']:,}")
                print(f"images to assess             : {info['images']:,}")
                print(f"estimated input tokens       : {info['est_input_tokens']:,}")
                print("estimated batch cost:")
                for model, usd in info["est_batch_cost_usd"].items():
                    print(f"  {model:20} ${usd:,.2f}")
                return 0
            if args.action == "submit":
                batch_id = photos.submit(
                    db, model=args.model or photos.DEFAULT_MODEL, limit=args.limit
                )
                print(f"batch id: {batch_id}" if batch_id else "nothing to submit")
                return 0
            if args.action == "poll":
                summary = photos.refresh_and_import(db)
                print(
                    f"imported {summary['imported']}, errored {summary['errored']}, "
                    f"still processing {summary['still_processing']} batch(es)"
                )
                return 0
            if args.action == "status":
                for row in photos.status(db):
                    print(row)
                return 0

        if args.command == "export":
            from .export import export_all

            for path in export_all(db, args.outdir):
                print(path)
            return 0

        if args.command == "restore":
            from .export import restore_all

            counts = restore_all(db, args.outdir)
            if not counts:
                print(f"No snapshots found in {args.outdir}/")
                return 1
            for table, n in counts.items():
                print(f"{table:20} {n:,} rows")
            return 0

        if args.command == "repair":
            counts = db.repair_descriptions()
            print(
                f"examined {counts['examined']:,} descriptions, "
                f"cleaned {counts['cleaned']:,}, "
                f"{counts['now_truncated']:,} now correctly flagged as truncated"
            )
            return 0

        if args.command == "changes":
            _print_changes(db, args.days)
            return 0

        if args.command == "status":
            stats = db.stats()
            width = max(len(k) for k in stats)
            for key, value in stats.items():
                print(f"{key.replace('_', ' '):<{width}}  {value:,}")
            row = db.conn.execute(
                "SELECT started_at, category, seen, new_listings, price_cuts, "
                "delisted, status FROM crawl_runs ORDER BY id DESC LIMIT 5"
            ).fetchall()
            if row:
                print("\nrecent crawls:")
                for r in row:
                    print(
                        f"  {r['started_at']}  {r['category'] or '-':<20} "
                        f"seen={r['seen'] or 0:<5} new={r['new_listings'] or 0:<4} "
                        f"cuts={r['price_cuts'] or 0:<4} gone={r['delisted'] or 0:<4} "
                        f"{r['status']}"
                    )
            return 0

    return 1


def _conditions(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _print_deals(deals: list, top: int) -> None:
    if not deals:
        print(
            "No candidates passed the screens. Try `--no-screens`, or relax\n"
            "min_profit_usd / min_roi in config.yml."
        )
        return
    print(f"\n{len(deals)} candidates passed screening; top {min(top, len(deals))}:\n")
    header = (
        f"{'REF':<8} {'TOWN':<16} {'m2':>5} {'ASKING':>10} {'$/m2':>7} "
        f"{'RESALE':>7} {'ALL-IN':>10} {'PROFIT':>10} {'ROI':>7} {'CONF':<7} COND"
    )
    print(header)
    print("-" * len(header))
    for d in deals[:top]:
        print(
            f"{d.ref:<8} {(d.town or '-')[:16]:<16} {d.area_m2:>5.0f} "
            f"{d.asking_price:>10,} {d.asking_ppm2:>7,.0f} {d.resale_ppm2:>7,.0f} "
            f"{d.all_in_cost:>10,.0f} {d.profit_usd:>10,.0f} {d.roi_pct:>6.1f}% "
            f"{d.confidence:<7} {d.condition_label}"
        )
        if d.flags:
            print(f"{'':<8} └ {d.flags}")


def _print_changes(db: Database, days: int) -> None:
    cuts = db.conn.execute(
        """
        SELECT p.ref, p.title, p.town, p.first_price_usd, p.price_usd,
               p.price_changes, p.url
        FROM properties p
        WHERE p.is_active = 1 AND p.price_changes > 0
          AND p.first_price_usd IS NOT NULL AND p.price_usd < p.first_price_usd
          AND p.last_seen >= datetime('now', ?)
        ORDER BY (p.first_price_usd - p.price_usd) DESC
        LIMIT 40
        """,
        (f"-{days} days",),
    ).fetchall()

    print(f"\nPrice reductions seen in the last {days} days: {len(cuts)}")
    if cuts:
        print(f"\n{'REF':<8} {'TOWN':<16} {'WAS':>10} {'NOW':>10} {'CUT':>10} {'%':>6}")
        print("-" * 64)
        for r in cuts:
            drop = r["first_price_usd"] - r["price_usd"]
            pct = drop / r["first_price_usd"] * 100
            print(
                f"{r['ref']:<8} {(r['town'] or '-')[:16]:<16} "
                f"{r['first_price_usd']:>10,} {r['price_usd']:>10,} "
                f"{drop:>10,} {pct:>5.1f}%"
            )

    gone = db.conn.execute(
        """
        SELECT ref, title, town, price_usd, delisted_at, first_seen
        FROM properties
        WHERE is_active = 0 AND delisted_at >= datetime('now', ?)
        ORDER BY delisted_at DESC LIMIT 25
        """,
        (f"-{days} days",),
    ).fetchall()
    print(f"\nDelisted in the last {days} days: {len(gone)}")
    print("(a delisting is often a sale — useful for calibrating real exit prices)")
    for r in gone:
        price = f"${r['price_usd']:,}" if r["price_usd"] else "—"
        print(f"  {r['ref']:<8} {(r['town'] or '-')[:18]:<18} {price:>10}  {r['delisted_at'][:10]}")


if __name__ == "__main__":
    sys.exit(main())
