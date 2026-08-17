"""Structured feature extraction from listing prose.

JSK writes descriptions as semi-standardised bullet lists ("- 3 Parking
Spaces", "- 45 m² Terrace", "- Maid's Room"), so far more structure is
recoverable than free text usually allows. These features exist to make the
resale benchmark honest: two flats in the same town are not comparable if one
has a sea view, three parking spaces and a 50 m² terrace and the other has
none of that.

Two hard limits, measured on the full inventory, worth remembering:

* **Building age and floor number are simply not published.** Age appears in
  ~0.1% of listings, floor in ~0.6%. No extractor fixes absence; `new_build`
  and `elevator` are the only weak proxies available.
* Coverage is bounded by description completeness -- most card blurbs are
  truncated mid-list, so features mentioned late (parking is usually last)
  are undercounted until the detail pass has run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields as dataclass_fields
from functools import lru_cache

_M2 = r"(?:m²|m2|sqm|square\s*met(?:er|re)s?)"


def _area_pattern(noun: str) -> re.Pattern[str]:
    """Match '45 m² terrace', 'terrace of 45 m2', 'terrace: 45 sqm'."""
    return re.compile(
        rf"(\d+(?:\.\d+)?)\s*{_M2}\s*(?:of\s+)?{noun}"
        rf"|{noun}\s*(?:of|:)?\s*(\d+(?:\.\d+)?)\s*{_M2}",
        re.I,
    )


INDOOR_RE = _area_pattern(r"indoor")
TERRACE_AREA_RE = _area_pattern(r"terrace")
GARDEN_AREA_RE = _area_pattern(r"garden")

PARKING_N_RE = re.compile(
    r"(\d+)\s*(?:underground\s+|covered\s+|private\s+)?parking", re.I
)
PARKING_RE = re.compile(r"\bparking\b|\bgarage\b", re.I)
BALCONY_N_RE = re.compile(r"(\d+)\s*balcon(?:y|ies)", re.I)
BALCONY_RE = re.compile(r"\bbalcon(?:y|ies)\b", re.I)
FLOOR_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)\s+floor\b|\bfloor\s*:?\s*(\d{1,2})\b", re.I
)

# Simple presence flags: (attribute, pattern)
FLAG_PATTERNS: list[tuple[str, str]] = [
    ("sea_view", r"\bsea\s*view\b"),
    ("mountain_view", r"\bmountain\s*view\b"),
    ("open_view", r"\bopen\s*view\b|\bpanoramic\b|\bunobstructed\b"),
    ("maid_room", r"\bmaid'?s?\s+room\b"),
    ("storage", r"\bcave\b|\bstorage\s+(?:unit|room)\b|\bdepot\b"),
    ("elevator", r"\belevator\b|\blift\b"),
    ("chimney", r"\bchimney\b|\bfireplace\b"),
    ("pool", r"\bpool\b"),
    ("gated", r"\bgated\b|\bcompound\b|\bresidence\b"),
    ("solar", r"\bsolar\b"),
    ("generator", r"\bgenerator\b|\b24/7\s+electricity\b"),
    ("furnished", r"\bfurnished\b"),
    ("duplex_layout", r"\bduplex\b|\btriplex\b"),
    ("rooftop", r"\brooftop\b|\btop\s+floor\b|\bpenthouse\b"),
    ("ground_floor", r"\bground\s+floor\b"),
    ("concierge", r"\bconcierge\b|\bdoorman\b"),
    (
        "new_build",
        r"\bunder[-\s]construction\b|\boff[-\s]plan\b|\bnew\s+(?:building|project|development)\b"
        r"|\bdelivery\s+(?:date|in|20\d\d)\b|\bpayment\s+(?:plan|facilit)",
    ),
    ("terrace", r"\bterrace\b"),
    ("garden", r"\bgarden\b"),
]
FLAG_COMPILED = [(name, re.compile(p, re.I)) for name, p in FLAG_PATTERNS]


@dataclass(frozen=True)
class PropertyFeatures:
    # Areas as stated in the prose, when stated.
    indoor_m2: float | None = None
    terrace_m2: float | None = None
    garden_m2: float | None = None
    floor: int | None = None
    parking_spaces: int = 0
    balconies: int = 0

    sea_view: bool = False
    mountain_view: bool = False
    open_view: bool = False
    maid_room: bool = False
    storage: bool = False
    elevator: bool = False
    chimney: bool = False
    pool: bool = False
    gated: bool = False
    solar: bool = False
    generator: bool = False
    furnished: bool = False
    duplex_layout: bool = False
    rooftop: bool = False
    ground_floor: bool = False
    concierge: bool = False
    new_build: bool = False
    terrace: bool = False
    garden: bool = False

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dataclass_fields(self)}


def _first_number(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    for group in match.groups():
        if group:
            return float(group)
    return None


@lru_cache(maxsize=8192)
def extract(*texts: str | None) -> PropertyFeatures:
    """Extract structured features from listing prose.

    Cached for the same reason `condition.assess` is: the web UI re-analyses
    the full inventory on every assumption change, and ~40 regexes over 2,500
    full-length descriptions cost ~700ms per pass. The result depends only on
    the input text; PropertyFeatures is frozen so sharing cached instances is
    safe.
    """
    blob = " \n ".join(t for t in texts if t)
    if not blob.strip():
        return PropertyFeatures()

    fields: dict = {}
    fields["indoor_m2"] = _first_number(INDOOR_RE.search(blob))
    fields["terrace_m2"] = _first_number(TERRACE_AREA_RE.search(blob))
    fields["garden_m2"] = _first_number(GARDEN_AREA_RE.search(blob))

    floor = _first_number(FLOOR_RE.search(blob))
    fields["floor"] = int(floor) if floor is not None and floor < 40 else None

    if PARKING_RE.search(blob):
        counts = [int(n) for n in PARKING_N_RE.findall(blob) if int(n) <= 10]
        fields["parking_spaces"] = max(counts) if counts else 1
    if BALCONY_RE.search(blob):
        counts = [int(n) for n in BALCONY_N_RE.findall(blob) if int(n) <= 10]
        fields["balconies"] = max(counts) if counts else 1

    for name, pattern in FLAG_COMPILED:
        if pattern.search(blob):
            fields[name] = True
    return PropertyFeatures(**fields)


def summary(f: PropertyFeatures) -> list[str]:
    """Compact human-readable feature list, for UI chips and CSV."""
    out: list[str] = []
    if f.new_build:
        out.append("new build / off-plan")
    for flag, label in (
        ("sea_view", "sea view"), ("mountain_view", "mountain view"),
        ("open_view", "open view"), ("rooftop", "rooftop"),
        ("ground_floor", "ground floor"), ("maid_room", "maid's room"),
        ("storage", "storage/cave"), ("elevator", "elevator"),
        ("chimney", "fireplace"), ("pool", "pool"), ("gated", "gated"),
        ("concierge", "concierge"), ("solar", "solar"),
        ("generator", "generator"), ("furnished", "furnished"),
        ("duplex_layout", "duplex"),
    ):
        if getattr(f, flag):
            out.append(label)
    if f.terrace_m2:
        out.append(f"terrace {f.terrace_m2:.0f}m²")
    elif f.terrace:
        out.append("terrace")
    if f.garden_m2:
        out.append(f"garden {f.garden_m2:.0f}m²")
    elif f.garden:
        out.append("garden")
    if f.parking_spaces:
        out.append(f"parking ×{f.parking_spaces}")
    if f.floor is not None:
        out.append(f"floor {f.floor}")
    if f.indoor_m2:
        out.append(f"indoor {f.indoor_m2:.0f}m²")
    return out


def effective_indoor_m2(listing_area: float | None, f: PropertyFeatures) -> float | None:
    """The floor area $/m² should be computed against.

    Most listings' headline area is indoor-only, but a minority bundle terrace
    or garden into it (measured: when bundled, a median 63% of phantom area).
    When the prose states an explicit indoor figure that is materially smaller
    than the headline, trust the prose.
    """
    if listing_area is None:
        return None
    if f.indoor_m2 and f.indoor_m2 < listing_area * 0.95:
        return f.indoor_m2
    # Headline area that exactly equals stated indoor+terrace is a bundle.
    if (
        f.indoor_m2 is None
        and f.terrace_m2
        and f.terrace_m2 >= listing_area * 0.2
        and f.terrace_m2 < listing_area
    ):
        # Can't be sure without an indoor figure; leave headline untouched.
        return listing_area
    return listing_area
