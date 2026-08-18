"""Parsers for jskre.com HTML.

The site is a server-rendered Laravel app with Tailwind classes and no
machine-readable product schema, so we anchor on the few stable structural
signals rather than on CSS classes (which change whenever the theme does):

* ``href="/properties/<slug>-l<id>"``   -- the listing link and its reference
* ``alt="Bedrooms">``/``Bathrooms``/``Area`` -- icon labels next to each figure
* ``REF: L#####``                       -- the agency reference
* ``let description = "...";``          -- full description, injected by JS

Listing *cards* on index pages already carry price, area, beds, baths, town
and a truncated description, so a crawl of index pages alone yields a complete
dataset. Detail pages are only needed for the untruncated description.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field, asdict

# --------------------------------------------------------------------- regex

PROPERTY_HREF_RE = re.compile(r'href="(/properties/[^"#?]+)"')
REF_IN_SLUG_RE = re.compile(r"-l(\d+)$", re.I)
REF_RE = re.compile(r"\bREF:\s*(L\d+)", re.I)
PRICE_RE = re.compile(r"\$\s*([\d,]+)")
BEDS_RE = re.compile(r'alt="Bedrooms"\s*>\s*([\d.]+)', re.I)
BATHS_RE = re.compile(r'alt="Bathrooms"\s*>\s*([\d.]+)', re.I)
AREA_RE = re.compile(r'alt="Area"\s*>\s*([\d,.]+)\s*(?:</[^>]+>\s*)?m', re.I | re.S)
AREA_TEXT_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*m²", re.I)
PHOTOS_RE = re.compile(r"([\d]+)\s*Photos?", re.I)
RESULTS_RE = re.compile(r"([\d,]+)\s*(?:</[^>]+>\s*)?\s*Results", re.I)
MAX_PAGE_RE = re.compile(r"[?&]page=(\d+)")
DESCRIPTION_JS_RE = re.compile(r"""(?:let|var|const)\s+description\s*=\s*(".*?");""", re.S)
IMAGES_JS_RE = re.compile(r"""(?:let|var|const)\s+images\s*=\s*(\[.*?\]);""", re.S)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
WS_RE = re.compile(r"[ \t ]+")

# Property types the site uses in the spec row.
KNOWN_TYPES = (
    "Apartment",
    "Villa",
    "Chalet",
    "Duplex",
    "Triplex",
    "Penthouse",
    "Studio",
    "Office",
    "Shop",
    "Warehouse",
    "Building",
    "Land",
    "Restaurant",
    "Showroom",
    "Hotel",
    "Factory",
    "Clinic",
)
TYPE_RE = re.compile(r">\s*(" + "|".join(KNOWN_TYPES) + r")\s*<", re.I)


@dataclass
class Listing:
    """A normalised property record."""

    ref: str
    url: str
    # Which site this record came from. Refs are only unique *within* a source,
    # so adapters for other sites must namespace theirs (e.g. 'confidence:1234')
    # to keep the primary key global.
    source: str = "jskre"
    title: str | None = None
    property_type: str | None = None
    price_usd: int | None = None
    area_m2: float | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    town: str | None = None
    district: str | None = None
    governorate: str | None = None
    location_raw: str | None = None
    description: str | None = None
    description_truncated: bool = False
    photo_count: int | None = None
    image_urls: list[str] = field(default_factory=list)
    # The agency's own reference, where it differs from the id we key on.
    source_reference: str | None = None
    # Only some sources publish these. Days-on-market and building age are
    # genuinely useful and simply absent from jskre.
    published_at: str | None = None
    year_built: int | None = None

    @property
    def price_per_m2(self) -> float | None:
        if self.price_usd and self.area_m2:
            return self.price_usd / self.area_m2
        return None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["image_urls"] = json.dumps(self.image_urls)
        d["price_per_m2"] = self.price_per_m2
        return d


# ------------------------------------------------------------------ helpers


def _text(fragment: str) -> str:
    """Strip tags/scripts and collapse whitespace, keeping line structure."""
    fragment = SCRIPT_STYLE_RE.sub(" ", fragment)
    fragment = TAG_RE.sub("\n", fragment)
    fragment = html.unescape(fragment)
    lines = [WS_RE.sub(" ", ln).strip() for ln in fragment.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _js_string(literal: str) -> str:
    """Decode a JS double-quoted string literal (handles \\n, \\", \\u...)."""
    try:
        return json.loads(literal)
    except json.JSONDecodeError:
        # Fall back to a tolerant unescape for malformed literals.
        return (
            literal.strip('"')
            .replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\/", "/")
            .replace("\\'", "'")
        )


def ref_from_url(url: str) -> str | None:
    """`/properties/nice-flat-l22296` -> `L22296`."""
    match = REF_IN_SLUG_RE.search(url.rstrip("/"))
    return f"L{match.group(1)}" if match else None


def split_location(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """Split "Sin El Fil, El Metn, Mount Lebanon" into (town, district, gov).

    Some listings carry only two parts ("Douar, Mount Lebanon") or one
    (foreign stock like "Cyprus"); we fill from the left and leave the rest
    None so the comps engine can degrade gracefully.
    """
    if not raw:
        return None, None, None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None, None, None
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], parts[1], parts[-1]


# -------------------------------------------------------------- index pages


def parse_result_count(html_text: str) -> int | None:
    match = RESULTS_RE.search(html_text)
    return _int(match.group(1)) if match else None


def parse_max_page(html_text: str) -> int | None:
    pages = [int(m) for m in MAX_PAGE_RE.findall(html_text)]
    return max(pages) if pages else None


def iter_listing_urls(html_text: str) -> list[str]:
    """Unique property URLs on an index page, in document order."""
    seen: dict[str, None] = {}
    for href in PROPERTY_HREF_RE.findall(html_text):
        seen.setdefault(href, None)
    return list(seen)


def parse_index_page(html_text: str) -> list[Listing]:
    """Extract one Listing per card on an index page.

    Cards are delimited by their own property link. The site renders each card
    twice (desktop + mobile variants), so we slice from the first occurrence of
    a link to the first occurrence of the *next distinct* link and merge.
    """
    urls = iter_listing_urls(html_text)
    if not urls:
        return []

    # Record where each distinct URL first appears, then carve the document
    # into one segment per card.
    positions: list[tuple[int, str]] = []
    for url in urls:
        idx = html_text.find(f'href="{url}"')
        if idx >= 0:
            positions.append((idx, url))
    positions.sort()

    listings: list[Listing] = []
    for i, (start, url) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(html_text)
        segment = html_text[start:end]
        # `start` points at `href=`, i.e. inside the opening <a> tag. Drop the
        # rest of that tag so its attributes are not mistaken for card text.
        tag_end = segment.find(">")
        if tag_end != -1:
            segment = segment[tag_end + 1 :]
        listing = parse_card(segment, url)
        if listing:
            listings.append(listing)
    return listings


def parse_card(segment: str, url: str) -> Listing | None:
    ref = REF_RE.search(segment)
    ref_value = ref.group(1).upper() if ref else ref_from_url(url)
    if not ref_value:
        return None

    text = _text(segment)
    lines = text.split("\n")

    price = PRICE_RE.search(segment)
    beds = BEDS_RE.search(segment)
    baths = BATHS_RE.search(segment)
    area = AREA_RE.search(segment) or AREA_TEXT_RE.search(text)
    photos = PHOTOS_RE.search(text)
    ptype = TYPE_RE.search(segment)

    title = _first_group(TITLE_H2_RE, segment) or _first_group(TITLE_P_RE, segment)
    location_raw = _location_from_address(segment)
    raw_blurb = _blurb_after_ref(lines)
    if title is None or location_raw is None:
        # Fall back to positional text parsing for cards with a different
        # template (the site renders a few layout variants).
        alt_title, alt_location, alt_description = _card_text_fields(lines, ref_value)
        title = title or alt_title
        location_raw = location_raw or alt_location
        raw_blurb = raw_blurb or alt_description
    description, truncated = clean_blurb(raw_blurb)

    return Listing(
        ref=ref_value,
        url=url,
        title=title,
        property_type=ptype.group(1).title() if ptype else None,
        price_usd=_int(price.group(1)) if price else None,
        area_m2=_float(area.group(1)) if area else None,
        bedrooms=_float(beds.group(1)) if beds else None,
        bathrooms=_float(baths.group(1)) if baths else None,
        location_raw=location_raw,
        description=description,
        description_truncated=truncated,
        photo_count=_int(photos.group(1)) if photos else None,
        **dict(
            zip(("town", "district", "governorate"), split_location(location_raw))
        ),
    )


TITLE_H2_RE = re.compile(r"<h2\b[^>]*>\s*(.*?)\s*</h2>", re.S)
TITLE_P_RE = re.compile(r'<p class="mb-4 poppins-bold[^"]*">\s*(.*?)\s*</p>', re.S)
ADDRESS_RE = re.compile(
    r'alt="location_icon"\s*>(.*?)</address>', re.S | re.I
)


def _first_group(pattern: re.Pattern[str], segment: str) -> str | None:
    match = pattern.search(segment)
    if not match:
        return None
    value = _text(match.group(1)).replace("\n", " ").strip()
    return WS_RE.sub(" ", value) or None


def _location_from_address(segment: str) -> str | None:
    """Read the address block, which may wrap across lines mid-value."""
    match = ADDRESS_RE.search(segment)
    if not match:
        return None
    raw = _text(match.group(1))
    # Lines can arrive as "Jbeil, Jbeil" + ", Mount Lebanon"; rejoin them and
    # normalise the comma spacing.
    joined = " ".join(ln.strip() for ln in raw.split("\n") if ln.strip())
    joined = re.sub(r"\s*,\s*", ", ", joined).strip(" ,")
    return joined or None


# Card markup puts the contact buttons immediately after the description, so
# their labels land in the extracted text. Everything from the first of these
# onwards is chrome, not listing content.
CTA_MARKERS = (
    "whatsapp us",
    "call us",
    "read more",
    "view details",
    "view property",
    "enquire",
)


def clean_blurb(text: str | None) -> tuple[str | None, bool]:
    """Strip card chrome from a description. Returns (text, was_truncated).

    Truncation has to be judged *after* the trailing call-to-action labels are
    removed: the site's ellipsis sits before them, so testing the raw string for
    a trailing "..." always said False and left every truncated blurb looking
    complete.
    """
    if not text:
        return None, False

    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if line.lower() in CTA_MARKERS:
            break
        # A bare tag fragment left over from slicing mid-element.
        if re.fullmatch(r"</?[a-zA-Z][^>]*>?", line):
            break
        lines.append(line)

    body = "\n".join(lines).strip()
    # Drop any residual fragment on the final line.
    body = re.sub(r"<[a-zA-Z/][^>\n]*$", "", body).strip()
    if not body:
        return None, False

    truncated = bool(re.search(r"\.{3,}\s*$", body))
    body = re.sub(r"\.{3,}\s*$", "...", body)
    return body, truncated


def _blurb_after_ref(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        if re.fullmatch(r"REF:\s*L\d+", line, re.I):
            blurb = "\n".join(ln for ln in lines[i + 1 :] if ln).strip()
            return blurb or None
    return None


def _card_text_fields(
    lines: list[str], ref: str
) -> tuple[str | None, str | None, str | None]:
    """Pull title, location and blurb out of a card's flattened text.

    Card text runs: "<n> Photos", "<n>", title, location..., price, type,
    beds, baths, area, "REF: L#####", then the description blurb.
    """
    title = None
    location_parts: list[str] = []
    ref_index = None

    for i, line in enumerate(lines):
        if re.fullmatch(r"REF:\s*L\d+", line, re.I):
            ref_index = i
            break

    for i, line in enumerate(lines[: ref_index if ref_index is not None else len(lines)]):
        # The title is the first substantial line that is not a photo counter
        # or a bare number.
        if title is None:
            if PHOTOS_RE.fullmatch(line) or re.fullmatch(r"[\d.,]+", line):
                continue
            if line.startswith("$"):
                continue
            title = line
            continue
        # Location follows the title and precedes the price.
        if line.startswith("$"):
            break
        if line.startswith(",") or "," in line or line in ("Mount Lebanon",):
            location_parts.append(line.lstrip(", ").strip())
        elif not location_parts:
            location_parts.append(line.strip())

    location_raw = ", ".join(p for p in location_parts if p) or None

    description = None
    if ref_index is not None:
        blurb = [ln for ln in lines[ref_index + 1 :] if ln]
        if blurb:
            description = "\n".join(blurb).strip() or None
            # Cards end their blurb with an ellipsis of varying length.
            description = re.sub(r"\.{3,}$", "...", description)

    return title, location_raw, description


# ------------------------------------------------------------- detail pages


def parse_detail_page(html_text: str, url: str) -> Listing | None:
    """Parse a /properties/... page. Yields the untruncated description."""
    ref = REF_RE.search(html_text) or re.search(r"Ref:\s*(L\d+)", html_text, re.I)
    ref_value = ref.group(1).upper() if ref else ref_from_url(url)
    if not ref_value:
        return None

    # Narrow to the spec block to avoid picking up prices from "similar
    # properties" further down the page.
    body = html_text
    anchor = body.find("Ref:")
    spec_block = body[: anchor + 200] if anchor > 0 else body

    price = PRICE_RE.search(spec_block)
    beds = BEDS_RE.search(spec_block)
    baths = BATHS_RE.search(spec_block)
    area = AREA_RE.search(spec_block)
    ptype = TYPE_RE.search(spec_block)
    photos = PHOTOS_RE.search(_text(spec_block))

    location_raw = None
    loc_match = re.search(
        r'alt="location_icon"\s*>\s*([^<]+)', spec_block, re.I
    )
    if loc_match:
        location_raw = WS_RE.sub(" ", html.unescape(loc_match.group(1))).strip()

    title = None
    title_match = re.search(
        r'<p class="mb-4 poppins-bold[^"]*">\s*([^<]+?)\s*</p>', spec_block
    )
    if title_match:
        title = html.unescape(title_match.group(1)).strip()
    if not title:
        head = re.search(r"<title>\s*(.*?)\s*</title>", html_text, re.S)
        if head:
            title = re.sub(
                r"\s*-\s*JSK Real Estate\s*$", "", html.unescape(head.group(1))
            ).strip()

    description = None
    desc_match = DESCRIPTION_JS_RE.search(html_text)
    if desc_match:
        description = _js_string(desc_match.group(1)).strip() or None

    image_urls: list[str] = []
    img_match = IMAGES_JS_RE.search(html_text)
    if img_match:
        try:
            for item in json.loads(img_match.group(1)):
                src = item.get("src") if isinstance(item, dict) else None
                if src:
                    image_urls.append(src)
        except (json.JSONDecodeError, AttributeError):
            pass

    town, district, governorate = split_location(location_raw)
    return Listing(
        ref=ref_value,
        url=url,
        title=title,
        property_type=ptype.group(1).title() if ptype else None,
        price_usd=_int(price.group(1)) if price else None,
        area_m2=_float(area.group(1)) if area else None,
        bedrooms=_float(beds.group(1)) if beds else None,
        bathrooms=_float(baths.group(1)) if baths else None,
        town=town,
        district=district,
        governorate=governorate,
        location_raw=location_raw,
        description=description,
        description_truncated=False,
        photo_count=_int(photos.group(1)) if photos else len(image_urls) or None,
        image_urls=image_urls,
    )
