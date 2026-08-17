"""Parser tests against trimmed real pages.

The fixtures are real jskre.com HTML with most of the boilerplate removed. If
the site is redesigned these tests are what will tell you, so refresh the
fixtures rather than loosening the assertions.
"""

from pathlib import Path

import pytest

from jskre import parse

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (FIXTURES / "index_page.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (FIXTURES / "detail_page.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------ pure bits


@pytest.mark.parametrize(
    "url,expected",
    [
        ("/properties/brand-new-apartment-for-sale-in-douar-l22322", "L22322"),
        ("/properties/x-l1", "L1"),
        ("/properties/no-ref-here", None),
        ("/properties/trailing-slash-l99/", "L99"),
    ],
)
def test_ref_from_url(url, expected):
    assert parse.ref_from_url(url) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Sin El Fil, El Metn, Mount Lebanon", ("Sin El Fil", "El Metn", "Mount Lebanon")),
        ("Douar, Mount Lebanon", ("Douar", None, "Mount Lebanon")),
        ("Cyprus", ("Cyprus", None, None)),
        ("", (None, None, None)),
        (None, (None, None, None)),
    ],
)
def test_split_location(raw, expected):
    assert parse.split_location(raw) == expected


@pytest.mark.parametrize(
    "raw,expected_text,expected_truncated",
    [
        # The site's card puts contact buttons right after the blurb.
        ("Nice flat\n- 120 m²\nWhatsApp us\nCall us\n<a", "Nice flat\n- 120 m²", False),
        # Truncation marker sits BEFORE the chrome, so it only shows up after
        # the chrome is stripped.
        ("It consists of 1 recepti......\nWhatsApp us\nCall us\n<a",
         "It consists of 1 recepti...", True),
        ("Complete text with no chrome.", "Complete text with no chrome.", False),
        ("Read more", None, False),
        ("", None, False),
        (None, None, False),
        # A residual tag fragment on the final line is dropped.
        ("Good flat\n<a href=\"/x\"", "Good flat", False),
    ],
)
def test_clean_blurb(raw, expected_text, expected_truncated):
    text, truncated = parse.clean_blurb(raw)
    assert text == expected_text
    assert truncated is expected_truncated


def test_card_descriptions_carry_no_card_chrome(index_html):
    for listing in parse.parse_index_page(index_html):
        assert listing.description
        lowered = listing.description.lower()
        assert "whatsapp us" not in lowered
        assert "call us" not in lowered
        assert "<a" not in lowered


def test_js_string_decodes_escapes():
    assert parse._js_string(r'"line1\nline2 \"quoted\" a\/b"') == 'line1\nline2 "quoted" a/b'


# ----------------------------------------------------------------- index page


def test_index_metadata(index_html):
    assert parse.parse_result_count(index_html) == 2533
    assert parse.parse_max_page(index_html) == 254


def test_index_yields_one_listing_per_card(index_html):
    listings = parse.parse_index_page(index_html)
    assert len(listings) == 3
    assert len({listing.ref for listing in listings}) == 3


def test_index_card_fields(index_html):
    listings = {listing.ref: listing for listing in parse.parse_index_page(index_html)}

    douar = listings["L22322"]
    assert douar.title == "Brand New Apartment For Sale in Douar"
    assert douar.price_usd == 120_000
    assert douar.area_m2 == 110.0
    assert douar.bedrooms == 2.0
    assert douar.bathrooms == 2.0
    assert douar.property_type == "Apartment"
    assert (douar.town, douar.district, douar.governorate) == (
        "Douar", "El Metn", "Mount Lebanon",
    )
    assert douar.price_per_m2 == pytest.approx(1090.9, abs=0.1)
    assert douar.description and "Brand New Apartment" in douar.description

    bsalim = listings["L22320"]
    assert bsalim.price_usd == 470_000
    assert bsalim.area_m2 == 222.0
    assert bsalim.town == "Bsalim"


def test_index_card_reports_photo_count(index_html):
    listings = parse.parse_index_page(index_html)
    assert any(listing.photo_count for listing in listings)


def test_index_page_with_no_listings_is_empty():
    assert parse.parse_index_page("<html><body>nothing here</body></html>") == []


# ---------------------------------------------------------------- detail page


def test_detail_page_fields(detail_html):
    url = "/properties/decorated-apartment-in-prime-location-for-sale-in-sin-el-fil-l22296"
    listing = parse.parse_detail_page(detail_html, url)

    assert listing is not None
    assert listing.ref == "L22296"
    assert listing.price_usd == 400_000
    assert listing.area_m2 == 210.0
    assert listing.bedrooms == 3.0
    assert listing.bathrooms == 3.0
    assert listing.town == "Sin El Fil"
    assert listing.district == "El Metn"
    assert listing.governorate == "Mount Lebanon"
    assert listing.property_type == "Apartment"
    assert listing.price_per_m2 == pytest.approx(1904.8, abs=0.1)


def test_detail_page_extracts_full_description(detail_html):
    listing = parse.parse_detail_page(detail_html, "/properties/x-l22296")
    assert listing.description is not None
    # The description lives in a JS string literal, newlines and all.
    assert "210 m²" in listing.description
    assert "\n" in listing.description
    assert listing.description_truncated is False


def test_detail_page_collects_image_urls(detail_html):
    listing = parse.parse_detail_page(detail_html, "/properties/x-l22296")
    assert len(listing.image_urls) == 4
    assert all(url.startswith("https://") for url in listing.image_urls)
    # URLs come from a JSON blob with escaped slashes; make sure we unescaped.
    assert all("\\/" not in url for url in listing.image_urls)


def test_detail_page_without_ref_returns_none():
    assert parse.parse_detail_page("<html></html>", "/properties/nothing") is None
