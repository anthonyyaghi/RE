"""Tests for the photo-assessment pipeline (everything except the live API)."""

import gzip
import json

import pytest

from jskre import photos
from jskre.db import Database
from jskre.export import export_all
from jskre.parse import Listing


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "p.db") as database:
        yield database


def add_listing(db, ref, urls, active=True, title="Nice flat"):
    db.upsert(Listing(
        ref=ref, url=f"/properties/x-{ref.lower()}", title=title,
        property_type="Apartment", price_usd=100_000, area_m2=100.0,
        town="Jbeil", district="Jbeil", governorate="Mount Lebanon",
        image_urls=urls,
    ))
    if not active:
        db.conn.execute("UPDATE properties SET is_active=0 WHERE ref=?", (ref,))
        db.conn.commit()


URLS = [f"https://s3.amazonaws.com/x/photo{i}.jpg" for i in range(6)]


# ------------------------------------------------------------------ selection


def test_pending_requires_active_and_urls_and_no_assessment(db):
    add_listing(db, "L1", URLS)
    add_listing(db, "L2", [])                 # no photos
    add_listing(db, "L3", URLS, active=False) # delisted
    add_listing(db, "L4", URLS)
    photos.import_result(db, "L4", {"condition": "dated"}, "m", None)

    refs = [p["ref"] for p in photos.pending_listings(db)]
    assert refs == ["L1"]


def test_select_photos_caps_and_dedupes():
    urls = [URLS[0], URLS[0], URLS[1], URLS[2], URLS[3], URLS[4]]
    picked = photos.select_photos(urls)
    assert len(picked) == photos.MAX_PHOTOS_PER_LISTING
    assert len(set(picked)) == len(picked)
    assert picked[0] == URLS[0]  # listing order preserved


# ------------------------------------------------------------------- requests


def test_build_params_shape():
    params = photos.build_params("L9", "Sea view flat", URLS)

    assert params["model"] == photos.DEFAULT_MODEL
    content = params["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == photos.MAX_PHOTOS_PER_LISTING
    assert all(b["source"]["type"] == "url" for b in images)
    # The text block references the listing so results are traceable.
    assert "L9" in content[-1]["text"]
    # Structured output is enforced, strict schema.
    schema = params["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    # The shared system prompt carries a cache breakpoint for the batch.
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_schema_enums_match_what_import_stores():
    conds = photos.OUTPUT_SCHEMA["properties"]["condition"]["enum"]
    assert "dated" in conds and "renovated" in conds and "shell" in conds


# --------------------------------------------------------------------- import


def test_import_result_is_idempotent_upsert(db):
    add_listing(db, "L1", URLS)
    data = {
        "condition": "dated", "kitchen_era": "original",
        "bathroom_era": "not_shown", "reno_scope": "medium",
        "confidence": "medium", "interior_photo_count": 3,
        "evidence": ["terrazzo flooring", "aluminum windows"],
    }
    photos.import_result(db, "L1", data, "claude-opus-5", "batch_1", image_count=4)
    # Re-import (e.g. re-running poll) replaces rather than duplicates.
    data["condition"] = "average"
    photos.import_result(db, "L1", data, "claude-opus-5", "batch_2", image_count=4)

    rows = db.conn.execute("SELECT * FROM photo_assessments").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["condition"] == "average"
    assert row["batch_id"] == "batch_2"
    assert json.loads(row["evidence"]) == ["terrazzo flooring", "aluminum windows"]


def test_assessed_listing_leaves_pending(db):
    add_listing(db, "L1", URLS)
    assert len(photos.pending_listings(db)) == 1
    photos.import_result(db, "L1", {"condition": "renovated"}, "m", None)
    assert photos.pending_listings(db) == []


# ------------------------------------------------------------------- estimate


def test_estimate_scales_with_pending(db):
    add_listing(db, "L1", URLS)
    add_listing(db, "L2", URLS[:2])

    info = photos.estimate(db)
    assert info["pending_listings"] == 2
    assert info["images"] == photos.MAX_PHOTOS_PER_LISTING + 2
    for model, usd in info["est_batch_cost_usd"].items():
        assert usd > 0
    # Opus costs more than Haiku, or the price table is wrong.
    assert (info["est_batch_cost_usd"]["claude-opus-5"]
            > info["est_batch_cost_usd"]["claude-haiku-4-5"])

    photos.import_result(db, "L1", {"condition": "dated"}, "m", None)
    assert photos.estimate(db)["pending_listings"] == 1


# --------------------------------------------------------------------- export


def test_export_writes_stable_snapshots(db, tmp_path):
    add_listing(db, "L1", URLS)
    photos.import_result(db, "L1", {"condition": "dated", "evidence": ["x"]}, "m", "b1")

    outdir = tmp_path / "exports"
    paths = export_all(db, outdir)
    names = {p.name for p in paths}
    assert names == {"listings.jsonl.gz", "price_history.jsonl.gz",
                     "photo_assessments.jsonl.gz"}

    with gzip.open(outdir / "listings.jsonl.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert rows[0]["ref"] == "L1"
    assert rows[0]["price_usd"] == 100_000

    # Byte-stable: exporting unchanged data produces identical files, so git
    # sees no diff when nothing changed.
    before = (outdir / "listings.jsonl.gz").read_bytes()
    export_all(db, outdir)
    assert (outdir / "listings.jsonl.gz").read_bytes() == before
