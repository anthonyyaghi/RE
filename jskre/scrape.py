"""Crawl orchestration.

Two passes, deliberately separate because they have very different costs:

*index pass* -- walks ``/listings/<category>?page=N``. Each page carries ten
full listing cards (price, area, beds, baths, town, reference, description), so
~450 requests capture the entire for-sale inventory. This is the pass you run
on a schedule.

*detail pass* -- fetches individual ``/properties/...`` pages, one request per
listing, purely to replace a truncated card blurb with the full description and
collect photo URLs. Run it once for the backlog, then only for new listings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .db import Database
from .http import PoliteClient, RobotsDisallowed
from .parse import (
    parse_detail_page,
    parse_index_page,
    parse_max_page,
    parse_result_count,
)

log = logging.getLogger(__name__)

# Categories worth tracking for a renovate-and-resell strategy. 'for-sale' is
# the superset; the others let you crawl a narrower slice more often.
CATEGORIES = {
    "for-sale": "/listings/for-sale",
    "apartment-for-sale": "/listings/apartment-for-sale",
    "villa-for-sale": "/listings/villa-for-sale",
    "chalet-for-sale": "/listings/chalet-for-sale",
    "building-for-sale": "/listings/building-for-sale",
}


@dataclass
class CrawlResult:
    category: str
    pages_fetched: int = 0
    seen: int = 0
    new_listings: int = 0
    price_cuts: int = 0
    price_rises: int = 0
    updated: int = 0
    delisted: int = 0
    complete: bool = False
    seen_refs: set[str] = field(default_factory=set)

    def summary(self) -> str:
        return (
            f"{self.category}: {self.seen} listings across {self.pages_fetched} pages "
            f"({self.new_listings} new, {self.price_cuts} price cuts, "
            f"{self.price_rises} rises, {self.updated} updated, "
            f"{self.delisted} delisted)"
        )


def crawl_index(
    db: Database,
    client: PoliteClient,
    category: str = "for-sale",
    max_pages: int | None = None,
    start_page: int = 1,
    delist_after_missed_crawls: int = 2,
) -> CrawlResult:
    """Walk index pages for a category, upserting every card found."""
    if category not in CATEGORIES:
        raise ValueError(
            f"Unknown category {category!r}; choose from {sorted(CATEGORIES)}"
        )

    path = CATEGORIES[category]
    result = CrawlResult(category=category)
    run_id = db.start_run(category)

    try:
        first_html = client.get(f"{path}?page={start_page}")
        if first_html is None:
            db.finish_run(run_id, status="failed", notes="could not fetch first page")
            return result

        total = parse_result_count(first_html)
        last_page = parse_max_page(first_html) or 1
        if max_pages:
            last_page = min(last_page, start_page + max_pages - 1)
        log.info(
            "%s: %s results, crawling pages %s-%s",
            category,
            f"{total:,}" if total else "?",
            start_page,
            last_page,
        )

        page = start_page
        html_text: str | None = first_html
        empty_streak = 0

        while page <= last_page:
            if html_text is None:
                html_text = client.get(f"{path}?page={page}")
            if html_text is None:
                log.warning("%s page %s unavailable; skipping", category, page)
                page += 1
                html_text = None
                continue

            result.pages_fetched += 1
            listings = parse_index_page(html_text)

            if not listings:
                empty_streak += 1
                # Two consecutive empty pages means we have run off the end of
                # the result set (the site keeps serving 200s past the last page).
                if empty_streak >= 2:
                    log.info("%s: no listings on two pages in a row; stopping", category)
                    break
            else:
                empty_streak = 0

            for listing in listings:
                outcome = db.upsert(listing, category=category)
                result.seen += 1
                result.seen_refs.add(listing.ref)
                if outcome == "new":
                    result.new_listings += 1
                elif outcome == "price_cut":
                    result.price_cuts += 1
                elif outcome == "price_rise":
                    result.price_rises += 1
                elif outcome == "updated":
                    result.updated += 1

            if result.pages_fetched % 25 == 0:
                log.info(
                    "%s: %s pages, %s listings so far",
                    category,
                    result.pages_fetched,
                    result.seen,
                )

            page += 1
            html_text = None

        # Only retire unseen listings after a full sweep from page 1.
        result.complete = start_page == 1 and max_pages is None
        if result.complete:
            result.delisted = db.mark_delisted(
                result.seen_refs, category, threshold=delist_after_missed_crawls
            )

        db.finish_run(
            run_id,
            status="ok",
            pages_fetched=result.pages_fetched,
            seen=result.seen,
            new_listings=result.new_listings,
            price_cuts=result.price_cuts,
            price_rises=result.price_rises,
            delisted=result.delisted,
        )
    except RobotsDisallowed as exc:
        log.error("Crawl stopped: %s", exc)
        db.finish_run(run_id, status="robots_blocked", notes=str(exc))
    except KeyboardInterrupt:
        db.finish_run(run_id, status="interrupted", seen=result.seen)
        raise

    return result


def crawl_details(
    db: Database,
    client: PoliteClient,
    limit: int | None = None,
    refs: list[str] | None = None,
) -> dict[str, int]:
    """Fetch detail pages to fill in full descriptions and photo URLs."""
    targets = refs if refs is not None else db.refs_needing_detail(limit)
    counts = {"fetched": 0, "updated": 0, "missing": 0, "failed": 0}

    for i, ref in enumerate(targets, start=1):
        url = db.url_for(ref)
        if not url:
            counts["missing"] += 1
            continue
        try:
            html_text = client.get(url)
        except RobotsDisallowed as exc:
            log.error("Detail crawl stopped: %s", exc)
            break
        if html_text is None:
            counts["missing"] += 1
            continue

        listing = parse_detail_page(html_text, url)
        if listing is None:
            counts["failed"] += 1
            continue

        db.upsert(listing, from_detail=True)
        counts["fetched"] += 1
        if i % 50 == 0:
            log.info("detail pass: %s/%s", i, len(targets))

    return counts
