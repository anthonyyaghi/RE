"""SQLite store with change history.

The schema is built around the fact that a listing is a *mutable* thing: its
price gets cut, its description gets rewritten, and eventually it disappears
(usually because it sold). Those transitions are exactly the signals a flipper
cares about, so we keep them rather than overwriting:

* ``properties``    -- current state, one row per agency reference
* ``price_history`` -- one row per observed price change
* ``crawl_runs``    -- provenance: what each crawl saw and changed

"Days on market" is measured from ``first_seen``, which is when *we* first saw
the listing, not when the agency published it. It is therefore a lower bound,
and only becomes meaningful once you have been running the scraper for a while.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .parse import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    ref                   TEXT PRIMARY KEY,
    url                   TEXT NOT NULL,
    source                TEXT NOT NULL DEFAULT 'jskre',
    title                 TEXT,
    property_type         TEXT,
    listing_category      TEXT,          -- e.g. 'apartment-for-sale'
    price_usd             INTEGER,
    area_m2               REAL,
    bedrooms              REAL,
    bathrooms             REAL,
    town                  TEXT,
    district              TEXT,
    governorate           TEXT,
    location_raw          TEXT,
    description           TEXT,
    description_truncated INTEGER DEFAULT 0,
    photo_count           INTEGER,
    image_urls            TEXT,          -- JSON array
    price_per_m2          REAL,
    first_seen            TEXT NOT NULL,
    last_seen             TEXT NOT NULL,
    first_price_usd       INTEGER,
    price_changes         INTEGER DEFAULT 0,
    is_active             INTEGER DEFAULT 1,
    delisted_at           TEXT,
    detail_fetched_at     TEXT,
    -- Consecutive complete crawls that did not see this listing. A full crawl
    -- takes minutes, during which new listings shift items across page
    -- boundaries, so a single miss is not evidence of a sale.
    missed_crawls         INTEGER DEFAULT 0
);

-- One row per *observed price change*. Deliberately no UNIQUE(ref,
-- observed_at): timestamps are second-resolution, so two changes seen in the
-- same second would collide and be silently lost. Duplicate suppression is
-- done in _record_price by comparing against the last recorded price instead.
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref         TEXT NOT NULL REFERENCES properties(ref) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    price_usd   INTEGER
);

-- User curation: a hand-picked shortlist. `note` is the user's own words --
-- "call agent", "visited, kitchen worse than photos" -- and removing a
-- bookmark deletes its note with it.
CREATE TABLE IF NOT EXISTS bookmarks (
    ref        TEXT PRIMARY KEY REFERENCES properties(ref),
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    category      TEXT,
    pages_fetched INTEGER DEFAULT 0,
    seen          INTEGER DEFAULT 0,
    new_listings  INTEGER DEFAULT 0,
    price_cuts    INTEGER DEFAULT 0,
    price_rises   INTEGER DEFAULT 0,
    delisted      INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'running',
    notes         TEXT
);

"""

# Indexes are created *after* migration, since an older database may not yet
# have the columns they reference.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_props_town     ON properties(town);
CREATE INDEX IF NOT EXISTS idx_props_district ON properties(district);
CREATE INDEX IF NOT EXISTS idx_props_active   ON properties(is_active);
CREATE INDEX IF NOT EXISTS idx_props_ppm2     ON properties(price_per_m2);
CREATE INDEX IF NOT EXISTS idx_price_hist_ref ON price_history(ref);
"""

# Columns that, when they change, we treat as a genuine listing update.
TRACKED_FIELDS = (
    "url",
    "title",
    "property_type",
    "price_usd",
    "area_m2",
    "bedrooms",
    "bathrooms",
    "town",
    "district",
    "governorate",
    "location_raw",
    "description",
    "photo_count",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path = "data/jskre.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(SCHEMA_INDEXES)
        self.conn.commit()

    # All columns the current code expects, with their DDL. Used to bring an
    # older database file up to date.
    EXPECTED_COLUMNS: tuple[tuple[str, str], ...] = (
        # Databases predating multi-source support hold jskre rows only, so the
        # default backfills them correctly.
        ("source", "TEXT NOT NULL DEFAULT 'jskre'"),
        ("title", "TEXT"),
        ("property_type", "TEXT"),
        ("listing_category", "TEXT"),
        ("price_usd", "INTEGER"),
        ("area_m2", "REAL"),
        ("bedrooms", "REAL"),
        ("bathrooms", "REAL"),
        ("town", "TEXT"),
        ("district", "TEXT"),
        ("governorate", "TEXT"),
        ("location_raw", "TEXT"),
        ("description", "TEXT"),
        ("description_truncated", "INTEGER DEFAULT 0"),
        ("photo_count", "INTEGER"),
        ("image_urls", "TEXT"),
        ("price_per_m2", "REAL"),
        ("first_price_usd", "INTEGER"),
        ("price_changes", "INTEGER DEFAULT 0"),
        ("is_active", "INTEGER DEFAULT 1"),
        ("delisted_at", "TEXT"),
        ("detail_fetched_at", "TEXT"),
        ("missed_crawls", "INTEGER DEFAULT 0"),
    )

    def _migrate(self) -> None:
        """Add columns introduced after a database file was first created.

        CREATE TABLE IF NOT EXISTS silently skips schema changes on an existing
        file, so new columns have to be added explicitly.
        """
        have = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(properties)").fetchall()
        }
        for column, ddl in self.EXPECTED_COLUMNS:
            if column not in have:
                self.conn.execute(f"ALTER TABLE properties ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ crawl runs

    def start_run(self, category: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawl_runs (started_at, category) VALUES (?, ?)",
            (utcnow(), category),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str = "ok", **counters: object) -> None:
        fields = ", ".join(f"{k} = ?" for k in counters)
        sql = "UPDATE crawl_runs SET finished_at = ?, status = ?"
        params: list[object] = [utcnow(), status]
        if fields:
            sql += ", " + fields
            params.extend(counters.values())
        sql += " WHERE id = ?"
        params.append(run_id)
        self.conn.execute(sql, params)
        self.conn.commit()

    # ------------------------------------------------------------- upserting

    def upsert(
        self,
        listing: Listing,
        category: str | None = None,
        from_detail: bool = False,
    ) -> str:
        """Insert or update a listing.

        Returns one of 'new', 'price_cut', 'price_rise', 'updated', 'unchanged'.
        """
        now = utcnow()
        record = listing.as_dict()
        record["listing_category"] = category
        existing = self.conn.execute(
            "SELECT * FROM properties WHERE ref = ?", (listing.ref,)
        ).fetchone()

        if existing is None:
            cols = list(record) + [
                "first_seen",
                "last_seen",
                "first_price_usd",
                "is_active",
                "detail_fetched_at",
            ]
            vals = list(record.values()) + [
                now,
                now,
                listing.price_usd,
                1,
                now if from_detail else None,
            ]
            placeholders = ", ".join("?" for _ in cols)
            self.conn.execute(
                f"INSERT INTO properties ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            if listing.price_usd is not None:
                self._record_price(listing.ref, listing.price_usd, now)
            self.conn.commit()
            return "new"

        outcome = "unchanged"
        old_price = existing["price_usd"]
        new_price = listing.price_usd

        if new_price is not None and new_price != old_price:
            self._record_price(listing.ref, new_price, now)
            outcome = "price_cut" if (old_price or 0) > new_price else "price_rise"
        else:
            for field_name in TRACKED_FIELDS:
                new_value = record.get(field_name)
                # A list-page crawl carries a truncated description; never let
                # it clobber a full one fetched from the detail page.
                if field_name == "description" and listing.description_truncated:
                    continue
                if new_value is not None and new_value != existing[field_name]:
                    outcome = "updated"
                    break

        updates = {
            k: v
            for k, v in record.items()
            # Preserve richer detail-page data when the current pass is a
            # cheaper list-page pass that found nothing for a field.
            if v is not None and not (k == "description" and listing.description_truncated)
        }
        updates["last_seen"] = now
        updates["is_active"] = 1
        updates["delisted_at"] = None
        updates["missed_crawls"] = 0
        if from_detail:
            updates["detail_fetched_at"] = now
        if outcome in ("price_cut", "price_rise"):
            updates["price_changes"] = (existing["price_changes"] or 0) + 1

        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(
            f"UPDATE properties SET {assignments} WHERE ref = ?",
            list(updates.values()) + [listing.ref],
        )
        self.conn.commit()
        return outcome

    def _record_price(self, ref: str, price: int, observed_at: str) -> None:
        """Append a price observation unless it repeats the last known price."""
        last = self.conn.execute(
            "SELECT price_usd FROM price_history WHERE ref = ? "
            "ORDER BY id DESC LIMIT 1",
            (ref,),
        ).fetchone()
        if last is not None and last["price_usd"] == price:
            return
        self.conn.execute(
            "INSERT INTO price_history (ref, observed_at, price_usd) VALUES (?, ?, ?)",
            (ref, observed_at, price),
        )

    def mark_delisted(
        self,
        seen_refs: Iterable[str],
        category: str | None,
        threshold: int = 2,
        source: str | None = None,
    ) -> int:
        """Retire active listings a complete crawl failed to see.

        Only call this after a *complete* crawl of that category -- a partial
        crawl would wrongly retire everything it did not reach.

        A listing is retired only after `threshold` consecutive complete crawls
        have missed it. A full sweep takes minutes, and listings added while it
        runs shift everything else across page boundaries, so a single miss is
        a paging artefact far more often than it is a sale. Returns the number
        of listings actually retired, not merely missed.
        """
        seen = set(seen_refs)
        sql = "SELECT ref, missed_crawls FROM properties WHERE is_active = 1"
        params: list[object] = []
        if category:
            sql += " AND listing_category = ?"
            params.append(category)
        if source:
            # Without this a complete crawl of one site would retire every
            # listing belonging to the others, which it never even looked at.
            sql += " AND source = ?"
            params.append(source)
        rows = self.conn.execute(sql, params).fetchall()

        missed = [r for r in rows if r["ref"] not in seen]
        if not missed:
            return 0

        now = utcnow()
        retire: list[tuple[str, str]] = []
        bump: list[tuple[str]] = []
        for row in missed:
            if (row["missed_crawls"] or 0) + 1 >= max(1, threshold):
                retire.append((now, row["ref"]))
            else:
                bump.append((row["ref"],))

        if bump:
            self.conn.executemany(
                "UPDATE properties SET missed_crawls = missed_crawls + 1 WHERE ref = ?",
                bump,
            )
        if retire:
            self.conn.executemany(
                "UPDATE properties SET is_active = 0, delisted_at = ?, "
                "missed_crawls = missed_crawls + 1 WHERE ref = ?",
                retire,
            )
        self.conn.commit()
        return len(retire)

    # ------------------------------------------------------------- accessors

    def active_listings(self, property_types: list[str] | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM properties WHERE is_active = 1 "
            "AND price_usd IS NOT NULL AND area_m2 IS NOT NULL AND area_m2 > 0"
        )
        params: list[object] = []
        if property_types:
            sql += " AND property_type IN (%s)" % ", ".join("?" for _ in property_types)
            params.extend(property_types)
        return self.conn.execute(sql, params).fetchall()

    def refs_needing_detail(
        self, limit: int | None = None, source: str | None = None
    ) -> list[str]:
        sql = (
            "SELECT ref FROM properties WHERE is_active = 1 AND ("
            "detail_fetched_at IS NULL OR description_truncated = 1"
            ")"
        )
        params: list[object] = []
        if source:
            # Each site's detail pass can only fetch its own URLs.
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY last_seen DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["ref"] for r in self.conn.execute(sql, params).fetchall()]

    def url_for(self, ref: str) -> str | None:
        row = self.conn.execute(
            "SELECT url FROM properties WHERE ref = ?", (ref,)
        ).fetchone()
        return row["url"] if row else None

    def price_history(self, ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT observed_at, price_usd FROM price_history WHERE ref = ? "
            "ORDER BY observed_at",
            (ref,),
        ).fetchall()

    def repair_descriptions(self) -> dict[str, int]:
        """Re-clean stored descriptions in place, no refetching required.

        Earlier crawls stored the card's trailing "WhatsApp us / Call us"
        chrome as part of the description, which also defeated truncation
        detection. The fix is a pure string transform, so there is no reason to
        hit the site again for it.
        """
        from .parse import clean_blurb

        rows = self.conn.execute(
            "SELECT ref, description, description_truncated FROM properties "
            "WHERE description IS NOT NULL"
        ).fetchall()

        updates: list[tuple[str | None, int, str]] = []
        for row in rows:
            cleaned, truncated = clean_blurb(row["description"])
            if cleaned != row["description"] or int(bool(truncated)) != (
                row["description_truncated"] or 0
            ):
                updates.append((cleaned, int(bool(truncated)), row["ref"]))

        if updates:
            self.conn.executemany(
                "UPDATE properties SET description = ?, description_truncated = ? "
                "WHERE ref = ?",
                updates,
            )
            self.conn.commit()
        return {
            "examined": len(rows),
            "cleaned": len(updates),
            "now_truncated": sum(1 for u in updates if u[1]),
        }

    # ------------------------------------------------------------- bookmarks

    def set_bookmark(self, ref: str, bookmarked: bool, note: str | None = None) -> None:
        """Add/remove a listing from the saved shortlist.

        Passing note=None on an existing bookmark preserves its note; passing
        a string (including "") replaces it.
        """
        if bookmarked:
            self.conn.execute(
                "INSERT INTO bookmarks (ref, note, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(ref) DO UPDATE SET "
                "note = COALESCE(excluded.note, bookmarks.note)",
                (ref, note, utcnow()),
            )
        else:
            self.conn.execute("DELETE FROM bookmarks WHERE ref = ?", (ref,))
        self.conn.commit()

    def bookmark_map(self) -> dict[str, dict]:
        return {
            row["ref"]: dict(row)
            for row in self.conn.execute("SELECT * FROM bookmarks").fetchall()
        }

    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "total": one("SELECT COUNT(*) FROM properties"),
            "active": one("SELECT COUNT(*) FROM properties WHERE is_active = 1"),
            "delisted": one("SELECT COUNT(*) FROM properties WHERE is_active = 0"),
            "with_detail": one(
                "SELECT COUNT(*) FROM properties WHERE detail_fetched_at IS NOT NULL"
            ),
            "price_changes": one("SELECT COUNT(*) FROM price_history"),
            "runs": one("SELECT COUNT(*) FROM crawl_runs"),
        }
