"""Tests for the Confidence Real Estate mapping, against real captured payloads.

The fixtures are trimmed copies of what the live site actually returned, so
these tests fail if their field names or shapes change -- which is the point.
"""

import json
from pathlib import Path

import pytest

from jskre import confidence as conf
from jskre.confidence_crawl import CATEGORY, _filter_body
from jskre.condition import assess
from jskre.db import Database
from jskre.features import extract

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def card() -> dict:
    payload = json.loads((FIXTURES / "confidence_list.json").read_text())
    return payload["Data"][0]


@pytest.fixture
def detail() -> dict:
    return json.loads((FIXTURES / "confidence_detail.json").read_text())


# ------------------------------------------------------------------ geography


def test_every_caza_maps_to_a_governorate():
    """Lebanon has 26 cazas; a gap would silently drop listings from comps."""
    assert len(conf.CAZA_TO_GOVERNORATE) == 26
    assert set(conf.CAZA_TO_GOVERNORATE.values()) == {
        "Beirut", "Mount Lebanon", "North", "Bekaa", "South"
    }


def test_district_spellings_are_reconciled_with_jskre():
    # jskre writes 'El Metn' and 'Kesrouane'; unreconciled, a town's comps
    # would split across two spellings of the same district.
    assert conf.normalise_district("Metn") == "El Metn"
    assert conf.normalise_district("Keserouan") == "Kesrouane"
    # Trailing whitespace is theirs, not ours.
    assert conf.normalise_district("Zgharta ") == "Zgharta"
    # An unknown district passes through rather than becoming None.
    assert conf.normalise_district("Batroun") == "Batroun"


def test_governorate_lookup_tolerates_their_whitespace():
    assert conf.governorate_for("Zgharta ") == "North"
    assert conf.governorate_for("Baabda") == "Mount Lebanon"
    assert conf.governorate_for("Nowhere") is None


# ---------------------------------------------------------------- list cards


def test_card_maps_onto_a_listing(card):
    listing = conf.listing_from_card(card)

    assert listing.ref == f"confidence:{card['Id']}"
    assert listing.source == "confidence"
    assert listing.url.endswith(f"/property/{card['Id']}")
    assert listing.price_usd == int(card["Price"])
    assert listing.area_m2 == card["Area"]
    assert listing.town == card["AreaName"]
    assert listing.governorate == "Mount Lebanon"
    assert listing.property_type == "Apartment"
    assert listing.published_at == card["PublishDate"]
    assert listing.source_reference == card["Reference"]


def test_card_description_is_absent_not_truncated(card):
    """List payloads carry no description at all.

    Flagging one as truncated would be a lie the detail pass then has to
    undo -- and `upsert` refuses to overwrite a full description with a
    truncated one, so the lie would stick.
    """
    listing = conf.listing_from_card(card)
    assert listing.description is None
    assert listing.description_truncated is False


def test_refs_are_namespaced_so_they_cannot_collide_with_jskre():
    assert conf.ref_for(47451) == "confidence:47451"


# -------------------------------------------------------------- detail pages


def test_detail_carries_description_and_gallery(detail):
    listing = conf.listing_from_detail(detail)

    assert listing.description and "205 sqm" in listing.description
    assert listing.image_urls and listing.photo_count == len(listing.image_urls)
    assert listing.town == detail["AreaLocation"]
    assert listing.district == "Baabda"


def test_structured_amenities_reach_the_feature_extractor(detail):
    """Confidence publishes as fields what jskre buries in prose.

    Both must land on the same feature flags, or the fitted premiums stop
    being comparable across sources.
    """
    listing = conf.listing_from_detail(detail)
    features = extract(listing.description)

    # The fixture's amenities are Maid's Room, Balcony and Storage Room.
    assert features.maid_room is True
    assert features.storage is True


def test_terrace_and_parking_fields_render_into_recognised_prose():
    record = {
        "Id": 1, "Name": "x", "Price": 100000, "Area": 100,
        "TerraceSize": 30.0, "GardenSize": 12.0,
        "NumberOfParkingSpaces": 2, "Floor": 3,
        "Description": "A flat.", "Amenities": [],
    }
    features = extract(conf.listing_from_detail(record).description)

    assert features.terrace_m2 == 30.0
    assert features.garden_m2 == 12.0
    assert features.parking_spaces == 2
    assert features.floor == 3


def test_under_construction_status_marks_new_build():
    """Their status field replaces the regex jskre needs for developer stock,
    which the screens exclude because it sells at a premium."""
    record = {
        "Id": 2, "Name": "x", "Price": 100000, "Area": 100,
        "StatusId": conf.STATUS_UNDER_CONSTRUCTION,
        "Description": "A flat.", "Amenities": [],
    }
    listing = conf.listing_from_detail(record)
    assert extract(listing.description).new_build is True
    # And it must not read as a renovation signal, which was a real bug on
    # jskre: 'under construction' is a premium, not a fixer-upper.
    assert assess(listing.description).label != "shell"


def test_ready_status_adds_no_spec_line():
    record = {"Id": 3, "StatusId": 16, "Amenities": []}
    assert conf.spec_lines(record) == []


def test_year_built_is_captured_when_present():
    record = {"Id": 4, "Name": "x", "Price": 1, "Area": 1,
              "YearBuild": 1998, "Amenities": []}
    assert conf.listing_from_detail(record).year_built == 1998
    # Absent is None, not zero -- their Age field uses 0 for 'unknown'.
    assert conf.listing_from_detail({"Id": 5, "Amenities": []}).year_built is None


# ------------------------------------------------------------------- storage


def test_both_sources_coexist_in_one_database(tmp_path, card):
    from jskre.parse import Listing

    with Database(tmp_path / "mixed.db") as db:
        db.upsert(Listing(ref="L1", url="/properties/x-l1", price_usd=200_000,
                          area_m2=150.0, town="Jbeil"), category="for-sale")
        db.upsert(conf.listing_from_card(card), category=CATEGORY)

        rows = {r["ref"]: r["source"] for r in
                db.conn.execute("SELECT ref, source FROM properties")}
        assert rows["L1"] == "jskre"
        assert rows[f"confidence:{card['Id']}"] == "confidence"
        assert len(db.active_listings()) == 2


# ----------------------------------------------------------------- requests


def test_filter_body_pins_lebanese_sale_listings_in_usd():
    body = _filter_body(page_number=0, page_size=50)

    assert body["BusinessTypeId"] == conf.BUSINESS_TYPE_SALE   # not rentals
    assert body["CountryId"] == conf.COUNTRY_LEBANON           # not the UAE
    assert body["SelectedCurrencyId"] == conf.CURRENCY_USD
    assert body["SelectedAreaUnitId"] == conf.AREA_UNIT_M2


def test_pager_is_zero_indexed():
    """Their first page is 0. Starting at 1 would silently skip nine listings."""
    assert _filter_body(0, 9)["Pager"] == {"PageNumber": 0, "PageSize": 9}
