"""Local web UI: a JSON API plus the static frontend.

Deliberately built on `http.server` rather than a framework. This is a personal
tool that runs on your own machine against a local SQLite file, so the value of
adding a web stack is low and the value of `pip install -r requirements.txt`
staying at two lines is high.

The API is stateless with respect to analysis: assumption values arrive as query
parameters on every request and nothing is stored server-side between calls.
That is what makes the live controls in the UI work -- you can drag a renovation
cost slider and the whole inventory re-ranks (~85ms warm) with no session state
to invalidate.

Binds to 127.0.0.1 by default. There is no authentication, so do not put this on
a public interface.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import traceback
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .analyze import (
    Assumptions,
    CompsIndex,
    market_summary,
    rank_deals,
)
from .condition import assess
from .features import extract, summary as feature_summary
from .config import load_config
from .db import Database
from .http import PoliteClient
from .scrape import CATEGORIES, crawl_details, crawl_index

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"
ASSUMPTION_FIELDS = {f.name: f.type for f in dataclass_fields(Assumptions)}

# Assumption fields that are text, not numbers.
STRING_ASSUMPTIONS = {"max_benchmark_scope"}


# --------------------------------------------------------------------- jobs


class Job:
    """A background scrape or detail pass, with progress and cancellation."""

    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, kind: str, params: dict) -> None:
        with Job._counter_lock:
            Job._counter += 1
            self.id = Job._counter
        self.kind = kind
        self.params = params
        self.status = "running"
        self.progress: dict = {}
        self.error: str | None = None
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.finished_at: str | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def request_stop(self) -> None:
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def update(self, snapshot: dict) -> None:
        with self._lock:
            self.progress = snapshot

    def finish(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.error = error
            self.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "params": self.params,
                "status": self.status,
                "progress": dict(self.progress),
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "stop_requested": self._stop.is_set(),
            }


class JobRunner:
    """Runs at most one crawl at a time, so we never hammer the site."""

    def __init__(self, db_path: str, config: dict) -> None:
        self.db_path = db_path
        self.config = config
        self.jobs: list[Job] = []
        self._lock = threading.Lock()

    def active(self) -> Job | None:
        with self._lock:
            for job in self.jobs:
                if job.status == "running":
                    return job
        return None

    def list_jobs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return [job.as_dict() for job in reversed(self.jobs[-limit:])]

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            for job in self.jobs:
                if job.id == job_id:
                    return job
        return None

    def start(self, kind: str, params: dict) -> tuple[Job | None, str | None]:
        existing = self.active()
        if existing is not None:
            return None, f"A {existing.kind} job is already running (#{existing.id})."

        job = Job(kind, params)
        with self._lock:
            self.jobs.append(job)

        thread = threading.Thread(
            target=self._run, args=(job,), name=f"jskre-job-{job.id}", daemon=True
        )
        thread.start()
        return job, None

    def _client(self) -> PoliteClient:
        http_cfg = self.config.get("http", {}) or {}
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

    def _run(self, job: Job) -> None:
        # Each job opens its own connection: sqlite3 objects are not safe to
        # share across threads.
        try:
            with Database(self.db_path) as db:
                if job.kind == "scrape":
                    result = crawl_index(
                        db,
                        self._client(),
                        category=job.params.get("category", "for-sale"),
                        max_pages=job.params.get("max_pages"),
                        delist_after_missed_crawls=int(
                            self.config.get("scrape", {}).get(
                                "delist_after_missed_crawls", 2
                            )
                        ),
                        progress=job.update,
                        should_stop=job.should_stop,
                    )
                    job.update(result.snapshot())
                else:
                    counts = crawl_details(
                        db,
                        self._client(),
                        limit=job.params.get("limit"),
                        progress=job.update,
                        should_stop=job.should_stop,
                    )
                    job.update(counts)
            job.finish("stopped" if job.should_stop() else "done")
        except Exception as exc:  # a crashed thread must not vanish silently
            log.exception("Job %s failed", job.id)
            job.finish("failed", f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------- analysis


def assumptions_from_query(base: Assumptions, query: dict[str, list[str]]) -> Assumptions:
    """Overlay assumption overrides from query params onto the saved defaults."""
    overrides: dict[str, object] = {}
    for name in ASSUMPTION_FIELDS:
        if name not in query:
            continue
        raw = query[name][0]
        if raw == "" or raw is None:
            continue
        if name in STRING_ASSUMPTIONS:
            overrides[name] = raw
            continue
        if raw.lower() in ("null", "none"):
            overrides[name] = None
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        # Keep integer-typed fields integral so downstream formatting is clean.
        if name in ("holding_months", "min_comps"):
            value = int(value)
        overrides[name] = value

    merged = {f.name: getattr(base, f.name) for f in dataclass_fields(Assumptions)}
    merged.update(overrides)
    return Assumptions(**merged)


def _row_to_listing(row) -> dict:
    condition = assess(row["title"], row["description"])
    return {
        "ref": row["ref"],
        "url": row["url"],
        "title": row["title"],
        "property_type": row["property_type"],
        "price_usd": row["price_usd"],
        "area_m2": row["area_m2"],
        "bedrooms": row["bedrooms"],
        "bathrooms": row["bathrooms"],
        "town": row["town"],
        "district": row["district"],
        "governorate": row["governorate"],
        "price_per_m2": row["price_per_m2"],
        "photo_count": row["photo_count"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "first_price_usd": row["first_price_usd"],
        "price_changes": row["price_changes"],
        "is_active": bool(row["is_active"]),
        "delisted_at": row["delisted_at"],
        "condition_label": condition.label,
        "condition_score": condition.score,
        "condition_signals": list(condition.signals),
    }


# ------------------------------------------------------------------ handler


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "jskre"
    protocol_version = "HTTP/1.1"

    # Injected by make_server.
    db_path: str = "data/jskre.db"
    config_path: str = "config.yml"
    config: dict = {}
    runner: JobRunner | None = None

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This binds to localhost and serves a local database; no caching so
        # the UI always reflects the current file.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # --------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._route_get(path, query)
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            log.exception("GET %s failed", path)
            self._error(500, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            self._route_post(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            log.exception("POST %s failed", path)
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/bootstrap":
            return self._api_bootstrap()
        if path == "/api/candidates":
            return self._api_candidates(query)
        if path == "/api/listings":
            return self._api_listings(query)
        if path.startswith("/api/listings/"):
            return self._api_listing_detail(path.rsplit("/", 1)[-1], query)
        if path == "/api/market":
            return self._api_market(query)
        if path == "/api/changes":
            return self._api_changes(query)
        if path == "/api/jobs":
            return self._json({"jobs": self.runner.list_jobs()})
        return self._error(404, f"No such endpoint: {path}")

    def _route_post(self, path: str) -> None:
        body = self._read_json_body()
        if path == "/api/assumptions":
            return self._api_save_assumptions(body)
        if path == "/api/jobs":
            return self._api_start_job(body)
        if path.endswith("/stop") and path.startswith("/api/jobs/"):
            return self._api_stop_job(path.split("/")[3])
        return self._error(404, f"No such endpoint: {path}")

    # ----------------------------------------------------------- api: state

    def _assumptions(self) -> Assumptions:
        return Assumptions.from_dict(self.config.get("assumptions"))

    def _types(self) -> list[str] | None:
        return self.config.get("analysis", {}).get("property_types") or None

    def _api_bootstrap(self) -> None:
        with Database(self.db_path) as db:
            stats = db.stats()
            runs = [
                dict(row)
                for row in db.conn.execute(
                    "SELECT started_at, finished_at, category, pages_fetched, seen, "
                    "new_listings, price_cuts, price_rises, delisted, status "
                    "FROM crawl_runs ORDER BY id DESC LIMIT 8"
                ).fetchall()
            ]
            towns = [
                row["town"]
                for row in db.conn.execute(
                    "SELECT town, COUNT(*) n FROM properties WHERE is_active = 1 "
                    "AND town IS NOT NULL GROUP BY town HAVING n >= 1 ORDER BY town"
                ).fetchall()
            ]
            types = [
                row["property_type"]
                for row in db.conn.execute(
                    "SELECT DISTINCT property_type FROM properties "
                    "WHERE property_type IS NOT NULL ORDER BY property_type"
                ).fetchall()
            ]

        a = self._assumptions()
        self._json(
            {
                "stats": stats,
                "runs": runs,
                "towns": towns,
                "property_types": types,
                "categories": sorted(CATEGORIES),
                "assumptions": {
                    f.name: getattr(a, f.name) for f in dataclass_fields(Assumptions)
                },
                "analysis_property_types": self._types(),
                "jobs": self.runner.list_jobs(),
                "config_path": str(self.config_path),
                "db_path": str(self.db_path),
            }
        )

    # ------------------------------------------------------ api: candidates

    def _api_candidates(self, query: dict[str, list[str]]) -> None:
        a = assumptions_from_query(self._assumptions(), query)
        conditions = [c for c in query.get("condition", [""])[0].split(",") if c]
        town = (query.get("town", [""])[0] or "").strip()
        apply_screens = query.get("screens", ["1"])[0] != "0"
        limit = min(int(query.get("limit", ["200"])[0] or 200), 2000)
        sort = query.get("sort", ["profit_usd"])[0]

        with Database(self.db_path) as db:
            rows = db.active_listings(self._types())

        # Comps are always built from the whole market, then candidates are
        # filtered -- narrowing first would starve the benchmark of comparables.
        comps = CompsIndex(rows, a)
        subject_rows = rows
        if town:
            needle = town.casefold()
            subject_rows = [r for r in rows if (r["town"] or "").casefold() == needle]
        for param, keep in (
            ("min_price", lambda price, bound: price >= bound),
            ("max_price", lambda price, bound: price <= bound),
        ):
            raw = (query.get(param, [""])[0] or "").strip()
            if not raw:
                continue
            try:
                bound = float(raw)
            except ValueError:
                continue
            subject_rows = [
                r for r in subject_rows
                if r["price_usd"] and keep(r["price_usd"], bound)
            ]

        deals = rank_deals(
            subject_rows,
            a,
            comps=comps,
            apply_screens=apply_screens,
            conditions=conditions or None,
        )

        if deals and sort in {f.name for f in dataclass_fields(type(deals[0]))}:
            # Cheapest-first for the "what does it cost" columns, best-first for
            # everything else.
            ascending = sort in ("asking_price", "asking_ppm2", "all_in_cost")
            deals.sort(
                key=lambda d: (getattr(d, sort) is None, getattr(d, sort) or 0),
                reverse=not ascending,
            )

        breakdown: dict[str, int] = {}
        for deal in deals:
            breakdown[deal.condition_label] = breakdown.get(deal.condition_label, 0) + 1

        self._json(
            {
                "total": len(deals),
                "breakdown": breakdown,
                "analysed": len(subject_rows),
                "market_size": len(rows),
                "candidates": [d.as_dict() for d in deals[:limit]],
                "assumptions": {
                    f.name: getattr(a, f.name) for f in dataclass_fields(Assumptions)
                },
            }
        )

    # -------------------------------------------------------- api: listings

    def _api_listings(self, query: dict[str, list[str]]) -> None:
        where = ["1=1"]
        params: list[object] = []

        if query.get("active", ["1"])[0] == "1":
            where.append("is_active = 1")
        text = (query.get("q", [""])[0] or "").strip()
        if text:
            where.append(
                "(title LIKE ? OR description LIKE ? OR ref LIKE ? OR town LIKE ?)"
            )
            params.extend([f"%{text}%"] * 4)
        for field_name, column in (("town", "town"), ("type", "property_type")):
            value = (query.get(field_name, [""])[0] or "").strip()
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        for field_name, column, op in (
            ("min_price", "price_usd", ">="),
            ("max_price", "price_usd", "<="),
            ("min_area", "area_m2", ">="),
            ("max_area", "area_m2", "<="),
        ):
            raw = (query.get(field_name, [""])[0] or "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue  # ignore unparseable filters rather than 500
            where.append(f"{column} {op} ?")
            params.append(value)

        sort_column = query.get("sort", ["last_seen"])[0]
        allowed = {
            "last_seen", "first_seen", "price_usd", "area_m2", "price_per_m2",
            "town", "ref", "price_changes",
        }
        if sort_column not in allowed:
            sort_column = "last_seen"
        direction = "ASC" if query.get("dir", ["desc"])[0] == "asc" else "DESC"

        page = max(1, int(query.get("page", ["1"])[0] or 1))
        per_page = min(200, max(1, int(query.get("per_page", ["50"])[0] or 50)))
        offset = (page - 1) * per_page

        clause = " AND ".join(where)
        with Database(self.db_path) as db:
            total = db.conn.execute(
                f"SELECT COUNT(*) FROM properties WHERE {clause}", params
            ).fetchone()[0]
            rows = db.conn.execute(
                f"SELECT * FROM properties WHERE {clause} "
                f"ORDER BY {sort_column} IS NULL, {sort_column} {direction} "
                f"LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()

        self._json(
            {
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
                "listings": [_row_to_listing(row) for row in rows],
            }
        )

    def _api_listing_detail(self, ref: str, query: dict[str, list[str]]) -> None:
        a = assumptions_from_query(self._assumptions(), query)
        with Database(self.db_path) as db:
            row = db.conn.execute(
                "SELECT * FROM properties WHERE ref = ?", (ref.upper(),)
            ).fetchone()
            if row is None:
                return self._error(404, f"No listing with reference {ref!r}")

            history = [dict(h) for h in db.price_history(row["ref"])]
            market_rows = db.active_listings(self._types())

        payload = _row_to_listing(row)
        payload["description"] = row["description"]
        payload["description_truncated"] = bool(row["description_truncated"])
        payload["detail_fetched_at"] = row["detail_fetched_at"]
        payload["location_raw"] = row["location_raw"]
        try:
            payload["image_urls"] = json.loads(row["image_urls"] or "[]")
        except (json.JSONDecodeError, TypeError):
            payload["image_urls"] = []
        payload["price_history"] = history
        payload["features"] = feature_summary(extract(row["title"], row["description"]))

        # Deal maths for this one listing, plus the exact comparables the
        # resale benchmark was computed from.
        deal = None
        comps_used: list[dict] = []
        if row["price_usd"] and row["area_m2"]:
            from .features import effective_indoor_m2

            comps = CompsIndex(market_rows, a)
            deals = rank_deals([row], a, comps=comps, apply_screens=False)
            deal = deals[0].as_dict() if deals else None
            if deal:
                # Mirror analyse_listing's subject exactly: same features,
                # same effective indoor area, so the pool shown IS the pool
                # priced.
                feats = extract(row["title"], row["description"])
                eff_area = effective_indoor_m2(row["area_m2"], feats) or row["area_m2"]
                comps_used = comps.comp_details(
                    row["town"], row["district"], row["governorate"],
                    eff_area, row["property_type"], subject_features=feats,
                )
        payload["deal"] = deal
        payload["comps_used"] = comps_used

        # Only fall back to same-town peers when there is no benchmark to show.
        if not comps_used:
            peers = [
                _row_to_listing(r)
                for r in market_rows
                if r["town"] == row["town"] and r["ref"] != row["ref"]
            ]
            peers.sort(key=lambda p: p["price_per_m2"] or 0)
            payload["peers"] = peers[:60]
        else:
            payload["peers"] = []
        self._json(payload)

    # ---------------------------------------------------- api: market/changes

    def _api_market(self, query: dict[str, list[str]]) -> None:
        a = assumptions_from_query(self._assumptions(), query)
        with Database(self.db_path) as db:
            rows = db.active_listings(self._types())
        self._json({"market": market_summary(rows, a), "market_size": len(rows)})

    def _api_changes(self, query: dict[str, list[str]]) -> None:
        days = max(1, min(365, int(query.get("days", ["30"])[0] or 30)))
        with Database(self.db_path) as db:
            cuts = [
                dict(row)
                for row in db.conn.execute(
                    """
                    SELECT ref, title, town, url, first_price_usd, price_usd,
                           price_changes, area_m2, last_seen
                    FROM properties
                    WHERE is_active = 1 AND price_changes > 0
                      AND first_price_usd IS NOT NULL AND price_usd < first_price_usd
                      AND last_seen >= datetime('now', ?)
                    ORDER BY (first_price_usd - price_usd) DESC LIMIT 200
                    """,
                    (f"-{days} days",),
                ).fetchall()
            ]
            gone = [
                dict(row)
                for row in db.conn.execute(
                    """
                    SELECT ref, title, town, url, price_usd, area_m2,
                           delisted_at, first_seen
                    FROM properties
                    WHERE is_active = 0 AND delisted_at >= datetime('now', ?)
                    ORDER BY delisted_at DESC LIMIT 200
                    """,
                    (f"-{days} days",),
                ).fetchall()
            ]
        for row in cuts:
            row["drop_usd"] = row["first_price_usd"] - row["price_usd"]
            row["drop_pct"] = round(row["drop_usd"] / row["first_price_usd"] * 100, 1)
        self._json({"days": days, "price_cuts": cuts, "delisted": gone})

    # -------------------------------------------------------- api: mutations

    def _api_save_assumptions(self, body: dict) -> None:
        """Persist assumption values into config.yml.

        Rewrites only the `assumptions` block, leaving the rest of the file --
        including its comments -- untouched, which matters because those
        comments are where the "these are placeholders" warnings live.
        """
        incoming = body.get("assumptions")
        if not isinstance(incoming, dict):
            return self._error(400, "Expected an 'assumptions' object.")

        current = Assumptions.from_dict(self.config.get("assumptions"))
        merged = {f.name: getattr(current, f.name) for f in dataclass_fields(Assumptions)}
        for key, value in incoming.items():
            if key not in ASSUMPTION_FIELDS:
                continue
            if key in STRING_ASSUMPTIONS:
                merged[key] = str(value)
            elif value is None:
                merged[key] = None
            else:
                try:
                    merged[key] = float(value)
                except (TypeError, ValueError):
                    return self._error(400, f"{key} must be a number or null.")
        try:
            validated = Assumptions(**merged)
        except TypeError as exc:
            return self._error(400, str(exc))

        path = Path(self.config_path)
        try:
            import yaml
        except ImportError:
            return self._error(
                500, "PyYAML is not installed, so config.yml cannot be written."
            )

        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        block = yaml.safe_dump(
            {
                "assumptions": {
                    f.name: getattr(validated, f.name)
                    for f in dataclass_fields(Assumptions)
                }
            },
            sort_keys=False,
            default_flow_style=False,
        )
        updated = _replace_yaml_block(raw, "assumptions", block)
        path.write_text(updated, encoding="utf-8")

        # Reload so subsequent requests see the new defaults.
        ApiHandler.config = load_config(self.config_path)
        if self.runner is not None:
            self.runner.config = ApiHandler.config
        self._json({"saved": True, "assumptions": merged, "path": str(path)})

    def _api_start_job(self, body: dict) -> None:
        kind = body.get("kind")
        if kind not in ("scrape", "details"):
            return self._error(400, "kind must be 'scrape' or 'details'.")

        params: dict = {}
        if kind == "scrape":
            category = body.get("category") or "for-sale"
            if category not in CATEGORIES:
                return self._error(400, f"Unknown category {category!r}.")
            params["category"] = category
            max_pages = body.get("max_pages")
            if max_pages not in (None, "", 0):
                try:
                    params["max_pages"] = max(1, int(max_pages))
                except (TypeError, ValueError):
                    return self._error(400, "max_pages must be a whole number.")
        else:
            try:
                params["limit"] = max(1, int(body.get("limit") or 200))
            except (TypeError, ValueError):
                return self._error(400, "limit must be a whole number.")

        job, error = self.runner.start(kind, params)
        if job is None:
            return self._error(409, error or "Could not start job.")
        self._json({"job": job.as_dict()}, status=202)

    def _api_stop_job(self, raw_id: str) -> None:
        try:
            job_id = int(raw_id)
        except ValueError:
            return self._error(400, "Bad job id.")
        job = self.runner.get(job_id)
        if job is None:
            return self._error(404, f"No job #{job_id}.")
        job.request_stop()
        self._json({"job": job.as_dict()})

    # -------------------------------------------------------------- static

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            return self._error(403, "Forbidden")
        if not target.is_file():
            # Unknown paths fall back to the app shell so deep links work.
            target = WEB_ROOT / "index.html"
            if not target.is_file():
                return self._error(404, "Frontend not built.")
        content_type, _ = mimetypes.guess_type(str(target))
        self._send(200, target.read_bytes(), content_type or "application/octet-stream")


def _replace_yaml_block(raw: str, key: str, replacement: str) -> str:
    """Swap a top-level YAML block, preserving everything around it."""
    lines = raw.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            start = i
            break
    if start is None:
        separator = "" if raw.endswith("\n") or not raw else "\n"
        return raw + separator + "\n" + replacement

    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i]
        # A new top-level key ends the block; blank and indented lines do not.
        if stripped.strip() and not stripped[0].isspace() and not stripped.startswith("#"):
            end = i
            break
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: str = "data/jskre.db",
    config_path: str = "config.yml",
) -> ThreadingHTTPServer:
    config = load_config(config_path)
    ApiHandler.db_path = db_path
    ApiHandler.config_path = config_path
    ApiHandler.config = config
    ApiHandler.runner = JobRunner(db_path, config)
    return ThreadingHTTPServer((host, port), ApiHandler)


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: str = "data/jskre.db",
    config_path: str = "config.yml",
) -> None:
    httpd = make_server(host, port, db_path, config_path)
    print(f"jskre web UI on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
