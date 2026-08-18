"""Mapping layer for Confidence Real Estate (confidencerealestate.com).

Confidence is a React app served by a JSON backend, so unlike jskre there is
no HTML to parse -- the collector hands us decoded payloads and this module
turns them into `Listing` records. Keeping the mapping here, free of any
network code, is what makes it testable against captured fixtures.

Two things are worth knowing about the data.

*It is better structured than jskre's.* Terrace and garden sizes, parking
counts, floor, year built and a Ready/Under-Construction status arrive as
fields rather than as prose we have to mine. The feature extractor still
works on text, so `spec_lines` renders those fields back into the exact
phrasing `features.extract` already recognises. That keeps one extraction
path for both sources -- and therefore one set of comparable premiums --
instead of two that could drift apart.

*Its geography is cleaner but spelled differently.* The site models Lebanon
as its 26 official cazas, which is genuinely useful, but writes them as
'Metn', 'Keserouan', 'Baalbck'. jskre writes 'El Metn', 'Kesrouane'. Comps
pool on these strings, so an unreconciled spelling silently splits a town's
comparables in two. Everything below is normalised toward jskre's vocabulary
because that is what the existing 2,533 rows already use.
"""

from __future__ import annotations

from .parse import Listing

SOURCE = "confidence"
BASE_URL = "https://confidencerealestate.com"
API_BASE = "https://api.mearealestate.com/api"
LIST_PATH = "/Web/v2/Properties/All"
DETAIL_PATH = "/Web/Properties"

# Request-side constants. Both are parameters rather than defaults, so we pin
# them explicitly instead of trusting whatever the site last used.
CURRENCY_USD = 2
AREA_UNIT_M2 = 1
BUSINESS_TYPE_SALE = 13
COUNTRY_LEBANON = 3

STATUS_UNDER_CONSTRUCTION = 15

# --------------------------------------------------------------- geography

# jskre records governorates in the older five-region scheme (Akkar folded
# into North, Baalbek-Hermel into Bekaa, Nabatieh into South). Confidence uses
# the 26 cazas without a governorate at all. Mapping into jskre's vocabulary
# keeps a single comp pool per region rather than two half-populated ones.
CAZA_TO_GOVERNORATE: dict[str, str] = {
    "Beirut": "Beirut",
    # Mount Lebanon
    "Aley": "Mount Lebanon",
    "Baabda": "Mount Lebanon",
    "Chouf": "Mount Lebanon",
    "Keserouan": "Mount Lebanon",
    "Metn": "Mount Lebanon",
    "Jbeil": "Mount Lebanon",
    # North (incl. Akkar)
    "Akkar": "North",
    "Batroun": "North",
    "Becharre": "North",
    "Koura": "North",
    "Minieh-denniye": "North",
    "Tripoli": "North",
    "Zgharta": "North",
    # Bekaa (incl. Baalbek-Hermel)
    "Baalbck": "Bekaa",
    "Hermel": "Bekaa",
    "Rachaya": "Bekaa",
    "West-bekaa": "Bekaa",
    "Zahle": "Bekaa",
    # South (incl. Nabatieh)
    "Bint-jbeil": "South",
    "Hasbaya": "South",
    "Jezzine": "South",
    "Marjeeyoun": "South",
    "Nabatieh": "South",
    "Saida": "South",
    "Tyre": "South",
}

# Where the two sites spell the same caza differently, jskre's spelling wins.
DISTRICT_ALIASES: dict[str, str] = {
    "Metn": "El Metn",
    "Keserouan": "Kesrouane",
    "Baalbck": "Baalbek",
    "Becharre": "Bcharre",
    "Minieh-denniye": "Minieh-Danniyeh",
    "Bint-jbeil": "Bint Jbeil",
    "West-bekaa": "West Bekaa",
    "Marjeeyoun": "Marjeyoun",
}

# Their seven types onto the vocabulary analyse/config already filter on.
TYPE_ALIASES: dict[str, str] = {
    "Apartment": "Apartment",
    "Villa": "Villa",
    "House": "Villa",
    "Chalet & cabin": "Chalet",
    "Buildings and multiple units": "Building",
    "Land": "Land",
    "Commercial": "Commercial",
}

# Amenity labels rendered into wording `features.extract` already matches, so
# structured amenities and jskre's prose land on the same feature flags.
AMENITY_PHRASES: dict[str, str] = {
    "Indoor Parking": "indoor parking",
    "Covered Parking": "covered parking",
    "Maid's Room": "maid's room",
    "Storage Room": "storage room",
    "Balcony": "balcony",
    "Swimming pool": "swimming pool",
    "Gym": "gym",
    "Closed Community": "gated community",
    "Air conditioning": "air conditioning",
    "Furnished apartments": "furnished",
    "High speed internet access": "high speed internet",
    "Water Well": "water well",
    "Electricity": "generator",
    "Laundry Facility": "laundry facility",
    "In-unit washer and dryer": "washer and dryer",
    "Pets allowed": "pets allowed",
}


def _clean(value: object) -> str | None:
    """Their strings carry stray whitespace ('Zgharta ', ' Ready')."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalise_district(name: object) -> str | None:
    cleaned = _clean(name)
    if cleaned is None:
        return None
    return DISTRICT_ALIASES.get(cleaned, cleaned)


def governorate_for(city: object) -> str | None:
    cleaned = _clean(city)
    return CAZA_TO_GOVERNORATE.get(cleaned) if cleaned else None


def normalise_type(name: object) -> str | None:
    cleaned = _clean(name)
    if cleaned is None:
        return None
    return TYPE_ALIASES.get(cleaned, cleaned)


def ref_for(property_id: object) -> str:
    """Namespace the id: ids are unique per site, the primary key is global."""
    return f"{SOURCE}:{property_id}"


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def spec_lines(record: dict) -> list[str]:
    """Render structured fields into the phrasing the feature extractor reads.

    This is deliberately not a second extraction path. Confidence publishes as
    fields what jskre buries in prose, and rendering them back into that prose
    means one extractor, one set of fitted premiums, and no risk of the two
    sources being scored by different rules.
    """
    lines: list[str] = []

    terrace = _number(record.get("TerraceSize"))
    if terrace:
        lines.append(f"{terrace:g} m² terrace")
    garden = _number(record.get("GardenSize"))
    if garden:
        lines.append(f"{garden:g} m² garden")

    parking = _number(record.get("NumberOfParkingSpaces"))
    if parking:
        lines.append(f"{parking:g} parking spaces")

    floor = record.get("Floor")
    if floor is not None and str(floor).strip() != "":
        lines.append(f"Floor: {floor}")

    # Their status field replaces the regex jskre needs to spot developer
    # stock, which the screens exclude because it sells at a premium rather
    # than offering renovation margin.
    if record.get("StatusId") == STATUS_UNDER_CONSTRUCTION:
        lines.append("under construction")

    for amenity in record.get("Amenities") or []:
        label = _clean(amenity.get("Value") if isinstance(amenity, dict) else amenity)
        if label:
            lines.append(AMENITY_PHRASES.get(label, label.lower()))

    return lines


def _description_with_specs(description: object, record: dict) -> str | None:
    text = _clean(description)
    specs = spec_lines(record)
    if not specs:
        return text
    block = "\n".join(f"- {line}" for line in specs)
    return f"{text}\n{block}" if text else block


def listing_from_card(item: dict) -> Listing:
    """Map one entry of the paginated list response.

    Cards carry no description, so a detail pass is still required before the
    feature extractor has anything to work with.
    """
    property_id = item.get("Id")
    image = _clean(item.get("ImgUrl"))
    return Listing(
        ref=ref_for(property_id),
        url=f"{BASE_URL}/property/{property_id}",
        source=SOURCE,
        title=_clean(item.get("Name")),
        property_type=normalise_type(item.get("Type")),
        price_usd=int(item["Price"]) if _number(item.get("Price")) else None,
        area_m2=_number(item.get("Area")),
        bedrooms=_number(item.get("NumberOfBedrooms")),
        bathrooms=_number(item.get("NumberOfBathrooms")),
        town=_clean(item.get("AreaName")),
        district=normalise_district(item.get("City")),
        governorate=governorate_for(item.get("City")),
        location_raw=", ".join(
            part
            for part in (
                _clean(item.get("AreaName")),
                _clean(item.get("City")),
                _clean(item.get("Country")),
            )
            if part
        )
        or None,
        # A card's description is genuinely absent rather than truncated, so it
        # must not be flagged as truncated -- that flag means "a fuller version
        # exists on the detail page", which is exactly what the detail pass
        # will supply.
        description=None,
        image_urls=[image] if image else [],
        source_reference=_clean(item.get("Reference")),
        published_at=_clean(item.get("PublishDate")),
    )


def listing_from_detail(content: dict) -> Listing:
    """Map the detail payload, which carries the description and gallery."""
    property_id = content.get("Id")
    images = [
        url
        for url in (
            _clean(media.get("FilePath"))
            for media in (content.get("MediaFiles") or [])
            if isinstance(media, dict)
        )
        if url
    ]
    year_built = content.get("YearBuild")
    return Listing(
        ref=ref_for(property_id),
        url=f"{BASE_URL}/property/{property_id}",
        source=SOURCE,
        title=_clean(content.get("Name")),
        property_type=normalise_type(content.get("Type")),
        price_usd=int(content["Price"]) if _number(content.get("Price")) else None,
        area_m2=_number(content.get("Area")),
        bedrooms=_number(content.get("NumberOfBedrooms")),
        bathrooms=_number(content.get("NumberOfBathrooms")),
        town=_clean(content.get("AreaLocation")),
        district=normalise_district(content.get("City")),
        governorate=governorate_for(content.get("City")),
        location_raw=", ".join(
            part
            for part in (
                _clean(content.get("AreaLocation")),
                _clean(content.get("City")),
                _clean(content.get("Country")),
            )
            if part
        )
        or None,
        description=_description_with_specs(content.get("Description"), content),
        photo_count=len(images) or None,
        image_urls=images,
        source_reference=_clean(content.get("Reference")),
        published_at=_clean(content.get("PublishDate")),
        year_built=int(year_built) if _number(year_built) else None,
    )
