"""Tests for the store: upsert semantics, change history, delisting."""

import pytest

from jskre.db import Database
from jskre.parse import Listing


@pytest.fixture
def db(tmp_path) -> Database:
    with Database(tmp_path / "test.db") as database:
        yield database


def listing(ref="L1", price=200_000, **kwargs) -> Listing:
    defaults = dict(
        ref=ref,
        url=f"/properties/x-{ref.lower()}",
        title="Apartment in Jbeil",
        property_type="Apartment",
        price_usd=price,
        area_m2=150.0,
        bedrooms=3.0,
        bathrooms=2.0,
        town="Jbeil",
        district="Jbeil",
        governorate="Mount Lebanon",
        location_raw="Jbeil, Jbeil, Mount Lebanon",
        description="A nice apartment",
    )
    defaults.update(kwargs)
    return Listing(**defaults)


def test_first_insert_is_new(db):
    assert db.upsert(listing(), category="for-sale") == "new"
    assert db.stats()["active"] == 1
    assert len(db.price_history("L1")) == 1


def test_reinsert_unchanged_is_unchanged(db):
    db.upsert(listing(), category="for-sale")
    assert db.upsert(listing(), category="for-sale") == "unchanged"
    assert len(db.price_history("L1")) == 1


def test_price_drop_is_recorded_as_cut(db):
    db.upsert(listing(price=200_000))
    assert db.upsert(listing(price=175_000)) == "price_cut"

    history = db.price_history("L1")
    assert [row["price_usd"] for row in history] == [200_000, 175_000]

    row = db.conn.execute("SELECT * FROM properties WHERE ref='L1'").fetchone()
    assert row["price_usd"] == 175_000
    assert row["first_price_usd"] == 200_000   # original asking is preserved
    assert row["price_changes"] == 1


def test_price_rise_is_distinguished(db):
    db.upsert(listing(price=200_000))
    assert db.upsert(listing(price=220_000)) == "price_rise"


def test_field_change_reports_updated(db):
    db.upsert(listing())
    assert db.upsert(listing(title="Renovated apartment in Jbeil")) == "updated"
    row = db.conn.execute("SELECT title FROM properties WHERE ref='L1'").fetchone()
    assert row["title"] == "Renovated apartment in Jbeil"


def test_truncated_description_never_overwrites_full_one(db):
    full = "Full description with every detail spelled out over several lines."
    db.upsert(listing(description=full), from_detail=True)

    # A later index-page pass carries only a truncated blurb.
    db.upsert(listing(description="Full description with...", description_truncated=True))

    row = db.conn.execute("SELECT description FROM properties WHERE ref='L1'").fetchone()
    assert row["description"] == full


def test_detail_pass_records_fetch_time(db):
    db.upsert(listing())
    row = db.conn.execute(
        "SELECT detail_fetched_at FROM properties WHERE ref='L1'"
    ).fetchone()
    assert row["detail_fetched_at"] is None

    db.upsert(listing(description="Long text from detail page"), from_detail=True)
    row = db.conn.execute(
        "SELECT detail_fetched_at FROM properties WHERE ref='L1'"
    ).fetchone()
    assert row["detail_fetched_at"] is not None


def test_missing_listing_is_marked_delisted(db):
    db.upsert(listing("L1"), category="for-sale")
    db.upsert(listing("L2"), category="for-sale")

    gone = db.mark_delisted({"L1"}, category="for-sale")

    assert gone == 1
    stats = db.stats()
    assert stats["active"] == 1 and stats["delisted"] == 1
    row = db.conn.execute("SELECT * FROM properties WHERE ref='L2'").fetchone()
    assert row["is_active"] == 0 and row["delisted_at"] is not None


def test_delisting_is_scoped_to_its_category(db):
    db.upsert(listing("L1"), category="apartment-for-sale")
    db.upsert(listing("L2"), category="villa-for-sale")

    # A crawl of apartments must not retire the villa.
    gone = db.mark_delisted({"L1"}, category="apartment-for-sale")
    assert gone == 0
    assert db.stats()["active"] == 2


def test_relisting_reactivates_and_clears_delisted_at(db):
    db.upsert(listing("L1"), category="for-sale")
    db.mark_delisted(set(), category="for-sale")
    assert db.stats()["delisted"] == 1

    db.upsert(listing("L1"), category="for-sale")
    row = db.conn.execute("SELECT * FROM properties WHERE ref='L1'").fetchone()
    assert row["is_active"] == 1 and row["delisted_at"] is None


def test_active_listings_requires_price_and_area(db):
    db.upsert(listing("L1"))
    db.upsert(listing("L2", price=None))
    db.upsert(listing("L3", area_m2=None))
    assert {row["ref"] for row in db.active_listings()} == {"L1"}


def test_active_listings_filters_by_property_type(db):
    db.upsert(listing("L1", property_type="Apartment"))
    db.upsert(listing("L2", property_type="Land"))
    refs = {row["ref"] for row in db.active_listings(["Apartment"])}
    assert refs == {"L1"}


def test_refs_needing_detail_tracks_backlog(db):
    db.upsert(listing("L1"))
    db.upsert(listing("L2", description="Full text"), from_detail=True)
    assert db.refs_needing_detail() == ["L1"]


def test_crawl_run_records_counters(db):
    run_id = db.start_run("for-sale")
    db.finish_run(run_id, status="ok", seen=42, new_listings=7, price_cuts=2)

    row = db.conn.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
    assert row["seen"] == 42 and row["new_listings"] == 7
    assert row["status"] == "ok" and row["finished_at"] is not None


def test_price_per_m2_is_stored(db):
    db.upsert(listing(price=300_000, area_m2=150.0))
    row = db.conn.execute("SELECT price_per_m2 FROM properties WHERE ref='L1'").fetchone()
    assert row["price_per_m2"] == pytest.approx(2000.0)
