"""Infer a property's renovation state from its listing text.

This is the pivot of the whole analysis. A flip only has margin when you buy
*unfinished or dated* stock and sell it at *finished* prices, so every listing
has to be sorted onto that axis before any comparison is meaningful.

The agency writes free text, so we score keyword evidence rather than trusting
any single phrase. Scores run from -1 (needs full renovation) to +1 (turnkey):

    RENOVATION_TARGET  <= -0.25   buy candidates
    NEUTRAL                       unknown / plain stock
    FINISHED           >=  0.35   defines the resale benchmark

Tune the weights below against your own reading of the market -- they are
deliberately in one place for that reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (regex, weight, label). Negative weight = needs work.
SIGNALS: list[tuple[str, float, str]] = [
    # ---- strong "needs work" ----
    (r"\bcore\s*(?:&|and)?\s*shell\b", -1.0, "core & shell"),
    (r"\bshell\b(?!\s*fish)", -0.8, "shell"),
    (r"\bunfinished\b", -0.9, "unfinished"),
    (r"\bneeds?\s+(?:full\s+|complete\s+|total\s+)?renovation\b", -0.9, "needs renovation"),
    (r"\brequir(?:es|ing)\s+(?:renovation|work|refurbish\w*)\b", -0.9, "requires work"),
    (r"\bto\s+be\s+(?:renovated|finished|completed)\b", -0.8, "to be renovated"),
    (r"\bas[-\s]is\b", -0.6, "sold as-is"),
    (r"\bfixer[-\s]upper\b", -1.0, "fixer-upper"),
    (r"\bhandyman\b", -0.7, "handyman special"),
    (r"\bdemolition\b", -0.8, "demolition"),
    (r"\bblack\s*work\b", -0.9, "black work"),
    (r"\bwithout\s+finishing\b", -0.9, "without finishing"),
    (r"\bno\s+finishing\b", -0.9, "no finishing"),
    # ---- moderate "dated" ----
    (r"\bold\s+(?:building|apartment|house|construction)\b", -0.5, "old building"),
    (r"\bneeds?\s+(?:some\s+)?(?:work|updating|refreshing|maintenance|tlc)\b", -0.5, "needs work"),
    (r"\bdated\b", -0.5, "dated"),
    (r"\brenovation\s+(?:potential|opportunity)\b", -0.6, "renovation potential"),
    (r"\bpotential\s+to\s+(?:renovate|improve)\b", -0.6, "renovation potential"),
    (r"\bhandover\s+(?:condition|state)\b", -0.4, "handover condition"),
    (r"\bunder\s+construction\b", -0.4, "under construction"),
    (r"\bopportunity\s+for\s+investors?\b", -0.3, "investor opportunity"),
    (r"\bbargain\b|\bmust\s+sell\b|\burgent(?:ly)?\s+sale\b", -0.3, "distressed wording"),
    (r"\bempty\b|\bvacant\b", -0.15, "vacant"),
    # ---- strong "finished" ----
    (r"\bbrand\s*new\b", 0.8, "brand new"),
    (r"\bnewly\s+(?:built|renovated|refurbished|finished)\b", 0.8, "newly renovated"),
    (r"\bfully\s+(?:renovated|refurbished)\b", 0.9, "fully renovated"),
    (r"\bturn[-\s]?key\b", 0.9, "turnkey"),
    (r"\bdeluxe\b", 0.6, "deluxe"),
    (r"\bhigh[-\s]end\b|\bluxur(?:y|ious)\b|\bpremium\s+finish\w*\b", 0.7, "high-end"),
    (r"\bdecorated\b", 0.6, "decorated"),
    (r"\bfully\s+furnished\b", 0.5, "fully furnished"),
    (r"\bfurnished\b", 0.3, "furnished"),
    (r"\bmodern\b|\bcontemporary\b", 0.3, "modern"),
    (r"\bexcellent\s+condition\b|\bimmaculate\b|\bpristine\b", 0.7, "excellent condition"),
    (r"\bnever\s+(?:used|lived)\b", 0.6, "never used"),
    (r"\bunder\s+warranty\b", 0.3, "under warranty"),
    (r"\bmarble\b|\bparquet\b", 0.2, "quality materials"),
]

COMPILED = [(re.compile(p, re.I), w, label) for p, w, label in SIGNALS]

TARGET_THRESHOLD = -0.25
FINISHED_THRESHOLD = 0.35


@dataclass
class ConditionAssessment:
    score: float
    label: str
    signals: list[str]

    @property
    def is_renovation_target(self) -> bool:
        return self.label == "renovation_target"

    @property
    def is_finished(self) -> bool:
        return self.label == "finished"


def assess(*texts: str | None) -> ConditionAssessment:
    """Score the combined text of a listing (title + description).

    We sum matched weights and squash the result, so several weak signals can
    combine but no single phrase can dominate outright.
    """
    blob = " \n ".join(t for t in texts if t)
    if not blob.strip():
        return ConditionAssessment(0.0, "unknown", [])

    total = 0.0
    matched: list[str] = []
    seen: set[str] = set()
    for pattern, weight, label in COMPILED:
        if pattern.search(blob):
            if label in seen:
                continue
            seen.add(label)
            total += weight
            matched.append(label)

    # Squash to [-1, 1]; 1.5 of accumulated weight ~= 0.76.
    score = _squash(total)

    if score <= TARGET_THRESHOLD:
        label = "renovation_target"
    elif score >= FINISHED_THRESHOLD:
        label = "finished"
    elif matched:
        label = "neutral"
    else:
        label = "unknown"

    return ConditionAssessment(round(score, 3), label, matched)


def _squash(value: float) -> float:
    """tanh-like squash without importing math for one call."""
    # tanh(x) implemented via exp to keep the dependency surface trivial.
    import math

    return math.tanh(value)
