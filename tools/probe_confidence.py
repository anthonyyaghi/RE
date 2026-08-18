#!/usr/bin/env python3
"""Reconnaissance probe for confidencerealestate.com.

Confidence is a React single-page app: the HTML it serves is an empty shell,
and the listings are drawn by JavaScript after the page calls its own backend.
There is therefore no markup to parse the way jskre.com's server-rendered
pages are parsed.

So this script does what a visitor's browser does -- it opens the site in real
Chromium, lets the page load itself, and records the JSON the site hands to
that browser. Nothing here constructs an API request by hand; it only listens
to traffic the page initiates on its own.

The output is a directory of captured responses whose shape tells us what a
real collector would need: the field names, the pagination parameters, and how
much inventory exists. Run it once, eyeball the summary, and send the
directory back.

    pip install playwright
    playwright install chromium
    python tools/probe_confidence.py

Options worth knowing:

    --headed        watch it work in a real window (useful the first time)
    --detail N      also open N listings to see the per-property payload
    --out DIR       where to write captures (default: data/confidence-probe)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - guidance for a fresh machine
    sys.exit(
        "playwright is not installed.\n"
        "  pip install playwright && playwright install chromium"
    )

BASE = "https://confidencerealestate.com"
LISTINGS_URL = f"{BASE}/all-properties"

# Identify ourselves rather than impersonating a stock browser, and keep the
# pace to something a person could plausibly produce.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36 (+re-research; contact via github.com/anthonyyaghi/RE)"
)
PAUSE_MS = 2_000

# Header values worth knowing the *presence* of but not worth writing to disk.
SENSITIVE_HEADERS = {"apikey", "authorization", "cookie", "set-cookie"}


def _redact(headers: dict) -> dict:
    """Keep header names (they tell us what the API requires), drop secrets."""
    return {
        k: ("<present, redacted>" if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


def _slug(url: str, index: int) -> str:
    tail = re.sub(r"[^A-Za-z0-9]+", "-", url.split("?")[0].split("//")[-1]).strip("-")
    return f"{index:03d}-{tail[-70:]}.json"


def _describe(value, depth: int = 0) -> str:
    """A compact shape summary, so the console output is readable at a glance."""
    pad = "  " * depth
    if isinstance(value, dict):
        # Deep enough to reach the fields of an individual listing, which are
        # the whole point of the exercise.
        if depth >= 4:
            return f"{{{len(value)} keys}}"
        lines = []
        for key, sub in list(value.items())[:25]:
            lines.append(f"{pad}  {key}: {_describe(sub, depth + 1)}")
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[] (empty)"
        return f"[{len(value)} items] first -> {_describe(value[0], depth + 1)}"
    if isinstance(value, str):
        shown = value if len(value) <= 60 else value[:57] + "..."
        return f'"{shown}"'
    return f"{value!r}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="data/confidence-probe", help="capture directory")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--detail", type=int, default=1,
                    help="how many individual listings to open (default 1)")
    ap.add_argument("--scrolls", type=int, default=3,
                    help="scroll-to-bottom attempts, to trigger paging (default 3)")
    ap.add_argument("--proxy", default=None, help="proxy URL, if your network needs one")
    ap.add_argument("--chromium", default=None,
                    help="path to a Chromium binary, if the bundled one is missing")
    ap.add_argument("--url", default=LISTINGS_URL,
                    help="page to open (default: the all-properties index)")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []

    with sync_playwright() as pw:
        launch: dict = {"headless": not args.headed}
        if args.proxy:
            launch["proxy"] = {"server": args.proxy}
        if args.chromium:
            launch["executable_path"] = args.chromium
        browser = pw.chromium.launch(**launch)
        context = browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 1000}
        )
        page = context.new_page()

        def on_response(response) -> None:
            """Record every JSON payload the page fetches for itself."""
            url = response.url
            if "/api/" not in url and "api." not in url:
                return
            try:
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                body = response.json()
            except Exception:
                return

            request = response.request
            post = request.post_data
            try:
                post = json.loads(post) if post else None
            except (TypeError, ValueError):
                pass

            record = {
                "url": url,
                "method": request.method,
                "status": response.status,
                "request_headers": _redact(request.headers),
                "request_body": post,
                "response": body,
            }
            captured.append(record)
            path = outdir / _slug(url, len(captured))
            path.write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"  captured {request.method} {url.split('?')[0]} -> {path.name}")

        page.on("response", on_response)

        print(f"Opening {args.url}")
        page.goto(args.url, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(PAUSE_MS)

        # Scrolling triggers whatever paging strategy the page uses, which is
        # how we learn the pagination parameters.
        for i in range(args.scrolls):
            page.mouse.wheel(0, 20_000)
            page.wait_for_timeout(PAUSE_MS)
            print(f"  scroll {i + 1}/{args.scrolls}")

        (outdir / "listings-rendered.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(outdir / "listings.png"), full_page=False)

        links = page.eval_on_selector_all(
            "a[href*='/property/']", "els => els.map(e => e.href)"
        )
        unique = sorted(set(links))
        print(f"\n{len(unique)} property links visible on the first screen")
        (outdir / "property-links.json").write_text(
            json.dumps(unique, indent=2), encoding="utf-8"
        )

        for href in unique[: max(0, args.detail)]:
            print(f"\nOpening listing {href}")
            time.sleep(PAUSE_MS / 1000)
            page.goto(href, wait_until="networkidle", timeout=90_000)
            page.wait_for_timeout(PAUSE_MS)
            (outdir / "detail-rendered.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(outdir / "detail.png"), full_page=False)

        browser.close()

    # ------------------------------------------------------------- summary
    print(f"\n{'=' * 70}\n{len(captured)} JSON responses captured into {outdir}\n")
    for record in captured:
        print(f"--- {record['method']} {record['url']}")
        if record["request_body"]:
            print(f"    request body: {json.dumps(record['request_body'])}")
        print(f"    response shape: {_describe(record['response'])}\n")

    if not captured:
        print(
            "No JSON was captured. The page may have failed to load -- rerun with\n"
            "--headed to watch what happens, and check whether the site is reachable."
        )
        return 1

    print(f"Send me the contents of {outdir} (it is gitignored, so it will not")
    print("be committed by accident) and I can write the collector against it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
