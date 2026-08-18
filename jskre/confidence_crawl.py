"""Browser-driven collection for Confidence Real Estate.

Confidence serves an empty HTML shell and draws its listings from a JSON
backend, so there is no markup to parse. This collector therefore does what a
visitor's browser does: it opens the real site in Chromium, lets the page
authenticate itself, and then asks *the page* to fetch further results from
its own origin.

That last point is the design decision worth understanding. We never hardcode
the site's client key -- we read it off the first request the page makes for
itself, and every subsequent call runs inside the page via ``page.evaluate``.
So the requests carry whatever credentials, origin and headers the site's own
code would have sent, and they stop working the moment the site stops serving
them, which is the correct behaviour.

Playwright is imported lazily: it is a heavy dependency and only this one
source needs it.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from . import confidence as conf
from .db import Database
from .scrape import CrawlResult

log = logging.getLogger(__name__)

CATEGORY = "confidence-for-sale"

# The site itself pages nine at a time. It is a request parameter rather than a
# server constant, so we ask for more and verify what we actually got.
DEFAULT_PAGE_SIZE = 50
SITE_PAGE_SIZE = 9

# Roughly a browsing human, matching the pace used for jskre.
DEFAULT_DELAY = 2.0

_FETCH_JS = """
async ([url, headers, body]) => {
  // The captured headers come from whatever API call the page happened to
  // make first. On the live site that is a GET, which carries the key but no
  // Content-Type -- and the backend answers a POST without one with HTTP 415.
  // So the JSON content type is asserted here rather than hoped for.
  const merged = {...headers};
  if (body) merged['Content-Type'] = 'application/json';
  const options = {method: body ? 'POST' : 'GET', headers: merged};
  if (body) options.body = JSON.stringify(body);
  const response = await fetch(url, options);
  let payload = null;
  try { payload = await response.json(); } catch (e) { payload = null; }
  return {status: response.status, payload};
}
"""


def _filter_body(page_number: int, page_size: int) -> dict:
    """The site's own search payload, pinned to Lebanese sale listings in USD."""
    return {
        "SelectedAreaUnitId": conf.AREA_UNIT_M2,
        "SelectedCurrencyId": conf.CURRENCY_USD,
        "AdTypesIds": [],
        "Featured": False,
        "Premium": False,
        "SearchTerm": None,
        "AgentId": 0,
        "SortById": 2,
        "Rooms": 0,
        "CountryId": conf.COUNTRY_LEBANON,
        "CityId": None,
        "AreaIds": None,
        "Amenities": [],
        "PriceFrom": 0,
        "PriceTo": 0,
        "AreaFrom": 0,
        "AreaTo": 0,
        "Area": None,
        "StatusId": None,
        "Floor": None,
        "BusinessTypeId": conf.BUSINESS_TYPE_SALE,
        "funishedId": None,
        "PaymentTypeId": None,
        # PageNumber is zero-indexed on this API.
        "Pager": {"PageNumber": page_number, "PageSize": page_size},
    }


class _Session:
    """A live browser session that can replay the site's own API calls."""

    def __init__(self, page, headers: dict, delay: float) -> None:
        self.page = page
        self.headers = headers
        self.delay = delay

    def _call(self, url: str, body: dict | None) -> dict | None:
        time.sleep(self.delay)
        result = self.page.evaluate(_FETCH_JS, [url, self.headers, body])
        if result["status"] != 200:
            log.warning("%s returned HTTP %s", url, result["status"])
            return None
        payload = result["payload"]
        if not payload or not payload.get("Succeeded", True):
            log.warning("%s reported failure: %s", url, (payload or {}).get("message"))
            return None
        return payload.get("content")

    def list_page(self, page_number: int, page_size: int) -> dict | None:
        return self._call(
            f"{conf.API_BASE}{conf.LIST_PATH}?Lang=en",
            _filter_body(page_number, page_size),
        )

    def detail(self, property_id: str) -> dict | None:
        return self._call(
            f"{conf.API_BASE}{conf.DETAIL_PATH}?Lang=en&Id={property_id}", None
        )


def _launch(playwright, headed: bool, channel: str | None, chromium: str | None):
    options: dict = {"headless": not headed}
    if channel:
        options["channel"] = channel
    if chromium:
        options["executable_path"] = chromium
    return playwright.chromium.launch(**options)


def _capture_headers(page) -> dict:
    """Take the site's own API headers from the first request it makes.

    Reading them off a live request rather than hardcoding them means we send
    exactly what the site sends, and inherit any future change for free.
    """
    captured: dict = {}

    def on_request(request) -> None:
        if captured or "/api/" not in request.url:
            return
        headers = request.headers
        wanted = {
            key: value
            for key, value in headers.items()
            if key.lower() in ("apikey", "content-type", "authorization")
        }
        if "apikey" in {k.lower() for k in wanted}:
            captured.update(wanted)

    page.on("request", on_request)
    return captured


def crawl(
    db: Database,
    pages: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    delay: float = DEFAULT_DELAY,
    details: bool = True,
    headed: bool = False,
    channel: str | None = None,
    chromium: str | None = None,
    progress: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> CrawlResult:
    """Collect Confidence's Lebanese sale listings into the database."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment guidance
        raise SystemExit(
            "Confidence collection needs Playwright:\n"
            "  pip install playwright && playwright install chromium\n"
            "If Chromium will not start, use --channel chrome instead."
        )

    result = CrawlResult(category=CATEGORY)
    run_id = db.start_run(CATEGORY)

    with sync_playwright() as playwright:
        browser = _launch(playwright, headed, channel, chromium)
        context = browser.new_page()
        headers = _capture_headers(context)

        log.info("opening %s", conf.BASE_URL)
        context.goto(f"{conf.BASE_URL}/all-properties", wait_until="networkidle",
                     timeout=90_000)
        context.wait_for_timeout(3_000)

        if not headers:
            browser.close()
            db.finish_run(run_id, status="failed",
                          notes="page made no identifiable API request")
            raise SystemExit(
                "Could not observe the site's own API request, so there are no "
                "headers to reuse. Re-run with --headed to see what the page did."
            )

        session = _Session(context, headers, delay)
        page_number, effective_size = 0, page_size

        while True:
            if should_stop is not None and should_stop():
                result.stopped = True
                break

            content = session.list_page(page_number, effective_size)
            if content is None:
                break

            paging = content.get("PagingInfo") or {}
            granted = paging.get("PageSize") or effective_size
            if page_number == 0 and granted != effective_size:
                # The server caps page size; believe it rather than silently
                # skipping every listing past the cap.
                log.warning("asked for %s per page, server granted %s",
                            effective_size, granted)
                effective_size = granted

            items = content.get("Data") or []
            for item in items:
                listing = conf.listing_from_card(item)
                outcome = db.upsert(listing, category=CATEGORY)
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

            result.pages_fetched += 1
            result.last_page = page_number
            if progress is not None:
                progress(result.snapshot())

            total_pages = paging.get("TotalPages")
            log.info("page %s/%s -- %s listings so far",
                     page_number + 1, total_pages, result.seen)

            page_number += 1
            if not items or not paging.get("HasNext"):
                result.complete = True
                break
            if pages is not None and result.pages_fetched >= pages:
                break

        # Only a sweep that reached the end can speak to what has disappeared.
        if result.complete and not result.stopped:
            result.delisted = db.mark_delisted(
                result.seen_refs, CATEGORY, source=conf.SOURCE
            )

        if details and not result.stopped:
            backlog = db.refs_needing_detail(source=conf.SOURCE)
            log.info("detail pass: %s listings", len(backlog))
            for i, ref in enumerate(backlog, start=1):
                if should_stop is not None and should_stop():
                    result.stopped = True
                    break
                content = session.detail(ref.split(":", 1)[-1])
                if content:
                    db.upsert(conf.listing_from_detail(content), from_detail=True)
                if i % 25 == 0:
                    log.info("detail pass: %s/%s", i, len(backlog))

        browser.close()

    db.finish_run(
        run_id,
        status="stopped" if result.stopped else "ok",
        pages_fetched=result.pages_fetched,
        seen=result.seen,
        new_listings=result.new_listings,
        price_cuts=result.price_cuts,
        price_rises=result.price_rises,
        delisted=result.delisted,
    )
    return result
