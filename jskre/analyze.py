"""Comparable-market benchmarks and the renovate-to-resell margin model.

The question this answers: *if I buy this apartment, renovate it, and resell it
at what finished apartments in the same area actually ask, what is left over?*

Two stages:

1. **Comps.** For each micro-market (town, then district, then governorate) we
   take the $/m2 of *finished* listings only -- turnkey, renovated, decorated --
   and use a configurable percentile as the achievable resale rate. Unfinished
   stock is excluded from the benchmark on purpose: including it would drag the
   exit price down toward the very prices we are trying to beat.

2. **Margin.** Asking price (less an assumed negotiation discount) plus purchase
   fees, renovation and holding costs, against that resale rate net of selling
   costs.

Every rate, fee and threshold is a config value, not a constant buried in code,
because the honest answer to "what does renovation cost per m2 in Lebanon" is
"it depends, go and price two jobs". Treat the defaults as placeholders to
calibrate, not as market truth.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence

from .condition import assess

# --------------------------------------------------------------------- config


@dataclass
class Assumptions:
    """Deal assumptions. Override these from config.yml -- see README."""

    # Acquisition
    negotiation_discount: float = 0.07     # expect to buy 7% under asking
    purchase_fees_pct: float = 0.06        # registration/notary/legal on purchase

    # Renovation, $/m2 of built area, by depth of work
    reno_cost_light: float = 180.0         # cosmetic: paint, fixtures, floors
    reno_cost_medium: float = 320.0        # kitchens, baths, wiring
    reno_cost_full: float = 550.0          # structural/full gut
    reno_contingency_pct: float = 0.15     # on top of the estimate

    # Holding
    holding_months: int = 8
    holding_cost_per_month: float = 250.0  # fees, utilities, insurance

    # Exit
    sale_commission_pct: float = 0.025     # agency fee on resale
    exit_percentile: float = 0.60          # where in the finished-comp range you sell

    # Comps quality gates
    min_comps: int = 5                     # below this, treat the benchmark as weak
    comp_area_tolerance: float = 0.45      # comps within +/-45% of subject area
    max_comp_ppm2_outlier: float = 3.0     # drop comps > 3x the market median

    # Guardrails against non-comparable benchmarks. Without these, a listing in
    # a cheap area gets valued against expensive stock several towns away and
    # shows a fantasy ROI. Both default to "reject rather than flatter".
    max_benchmark_scope: str = "district"  # widest scope allowed to pass screening
    max_resale_uplift: float = 2.5         # reject if modelled resale/m2 exceeds
                                           # this multiple of asking/m2 -- that
                                           # gap means bad comps, not a bargain

    # Screening
    min_profit_usd: float = 15_000.0
    min_roi: float = 0.15
    max_price_usd: float | None = None     # your budget ceiling, if any
    min_area_m2: float = 60.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "Assumptions":
        if not data:
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# ---------------------------------------------------------------------- comps


@dataclass
class Benchmark:
    scope: str            # 'town' | 'district' | 'governorate' | 'global'
    key: str
    resale_ppm2: float
    median_ppm2: float
    n_comps: int
    low_ppm2: float
    high_ppm2: float

    @property
    def is_weak(self) -> bool:
        return self.n_comps < 5


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile; `pct` in [0, 1]."""
    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = pct * (len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


class CompsIndex:
    """Per-area resale benchmarks built from finished listings."""

    def __init__(self, rows: Iterable[sqlite3.Row], assumptions: Assumptions) -> None:
        self.a = assumptions
        self.finished: list[dict] = []
        self.all_ppm2: list[float] = []

        for row in rows:
            ppm2 = _ppm2(row)
            if ppm2 is None or ppm2 <= 0:
                continue
            self.all_ppm2.append(ppm2)
            condition = assess(row["title"], row["description"])
            if condition.is_finished:
                self.finished.append(
                    {
                        "ppm2": ppm2,
                        "area_m2": row["area_m2"],
                        "town": row["town"],
                        "district": row["district"],
                        "governorate": row["governorate"],
                        "property_type": row["property_type"],
                    }
                )

        # A market-wide sanity ceiling so one mispriced trophy listing cannot
        # define a town's benchmark.
        self.global_median = statistics.median(self.all_ppm2) if self.all_ppm2 else 0.0
        ceiling = self.global_median * self.a.max_comp_ppm2_outlier
        if ceiling > 0:
            self.finished = [c for c in self.finished if c["ppm2"] <= ceiling]

        self._cache: dict[tuple, Benchmark | None] = {}

    def benchmark_for(
        self,
        town: str | None,
        district: str | None,
        governorate: str | None,
        area_m2: float | None,
        property_type: str | None,
    ) -> Benchmark | None:
        """Best available benchmark, narrowest scope that clears min_comps."""
        cache_key = (town, district, governorate, property_type, _area_bucket(area_m2))
        if cache_key in self._cache:
            return self._cache[cache_key]

        result: Benchmark | None = None
        for scope, key in (
            ("town", town),
            ("district", district),
            ("governorate", governorate),
        ):
            if not key:
                continue
            pool = [c for c in self.finished if c[scope] == key]
            if property_type:
                typed = [c for c in pool if c["property_type"] == property_type]
                # Only narrow by type if doing so leaves a usable sample.
                if len(typed) >= self.a.min_comps:
                    pool = typed
            sized = _filter_by_area(pool, area_m2, self.a.comp_area_tolerance)
            if len(sized) >= self.a.min_comps:
                pool = sized
            if pool:
                candidate = self._build(scope, key, pool)
                result = candidate
                if candidate.n_comps >= self.a.min_comps:
                    break

        if result is None and self.finished:
            result = self._build("global", "all", self.finished)

        self._cache[cache_key] = result
        return result

    def _build(self, scope: str, key: str, pool: list[dict]) -> Benchmark:
        values = [c["ppm2"] for c in pool]
        return Benchmark(
            scope=scope,
            key=key,
            resale_ppm2=percentile(values, self.a.exit_percentile),
            median_ppm2=statistics.median(values),
            n_comps=len(values),
            low_ppm2=min(values),
            high_ppm2=max(values),
        )


SCOPE_ORDER = ("town", "district", "governorate", "global")


def scope_is_local_enough(scope: str, max_scope: str) -> bool:
    """True if `scope` is no wider than `max_scope`."""
    try:
        return SCOPE_ORDER.index(scope) <= SCOPE_ORDER.index(max_scope)
    except ValueError:
        return True


def _area_bucket(area_m2: float | None) -> int | None:
    return int(area_m2 // 50) if area_m2 else None


def _filter_by_area(
    pool: list[dict], area_m2: float | None, tolerance: float
) -> list[dict]:
    if not area_m2:
        return pool
    low, high = area_m2 * (1 - tolerance), area_m2 * (1 + tolerance)
    return [c for c in pool if c["area_m2"] and low <= c["area_m2"] <= high]


def _ppm2(row: sqlite3.Row) -> float | None:
    try:
        if row["price_per_m2"]:
            return float(row["price_per_m2"])
    except (IndexError, KeyError):
        pass
    if row["price_usd"] and row["area_m2"]:
        return float(row["price_usd"]) / float(row["area_m2"])
    return None


# --------------------------------------------------------------------- margin


@dataclass
class DealAnalysis:
    ref: str
    url: str
    title: str | None
    town: str | None
    district: str | None
    property_type: str | None
    area_m2: float
    bedrooms: float | None
    asking_price: int
    asking_ppm2: float

    condition_label: str
    condition_score: float
    condition_signals: str
    reno_depth: str

    resale_ppm2: float
    benchmark_scope: str
    benchmark_key: str
    n_comps: int
    market_median_ppm2: float
    resale_uplift_ratio: float
    comps_are_local: bool

    purchase_price: float
    purchase_fees: float
    reno_cost: float
    holding_cost: float
    all_in_cost: float
    gross_resale: float
    selling_costs: float
    net_resale: float
    profit_usd: float
    roi_pct: float
    margin_pct: float
    discount_to_market_pct: float

    price_cuts: int
    days_tracked: int
    confidence: str
    flags: str

    def as_dict(self) -> dict:
        return asdict(self)


def reno_depth_for(condition_label: str, condition_score: float) -> str:
    """Map condition onto a renovation scope."""
    if condition_label == "renovation_target":
        return "full" if condition_score <= -0.6 else "medium"
    if condition_label in ("neutral", "unknown"):
        return "light"
    return "light"


def analyse_listing(
    row: sqlite3.Row,
    comps: CompsIndex,
    a: Assumptions,
    days_tracked: int = 0,
) -> DealAnalysis | None:
    area = row["area_m2"]
    price = row["price_usd"]
    if not area or not price or area <= 0:
        return None

    asking_ppm2 = price / area
    condition = assess(row["title"], row["description"])
    benchmark = comps.benchmark_for(
        row["town"], row["district"], row["governorate"], area, row["property_type"]
    )
    if benchmark is None:
        return None

    depth = reno_depth_for(condition.label, condition.score)
    reno_rate = {
        "light": a.reno_cost_light,
        "medium": a.reno_cost_medium,
        "full": a.reno_cost_full,
    }[depth]

    purchase_price = price * (1 - a.negotiation_discount)
    purchase_fees = purchase_price * a.purchase_fees_pct
    reno_cost = area * reno_rate * (1 + a.reno_contingency_pct)
    holding_cost = a.holding_months * a.holding_cost_per_month
    all_in = purchase_price + purchase_fees + reno_cost + holding_cost

    gross_resale = benchmark.resale_ppm2 * area
    selling_costs = gross_resale * a.sale_commission_pct
    net_resale = gross_resale - selling_costs

    profit = net_resale - all_in
    roi = profit / all_in if all_in else 0.0
    margin = profit / net_resale if net_resale else 0.0
    discount = (
        (benchmark.median_ppm2 - asking_ppm2) / benchmark.median_ppm2
        if benchmark.median_ppm2
        else 0.0
    )

    flags: list[str] = []
    if benchmark.is_weak or benchmark.n_comps < a.min_comps:
        flags.append(f"thin comps (n={benchmark.n_comps})")
    if benchmark.scope in ("governorate", "global"):
        flags.append(f"benchmark from {benchmark.scope} level")
    if row["price_changes"]:
        flags.append(f"{row['price_changes']} price change(s)")
    if condition.label == "unknown":
        flags.append("condition not stated")
    if row["description_truncated"]:
        flags.append("description truncated - run detail pass")
    if asking_ppm2 > benchmark.resale_ppm2:
        flags.append("asking above modelled resale")

    # Round once, here, and use this single value for the flag, the confidence
    # downgrade and the screen. Comparing a raw ratio against a rounded stored
    # field lets a listing be flagged as implausible yet still pass screening.
    uplift = round(benchmark.resale_ppm2 / asking_ppm2, 2) if asking_ppm2 else 0.0
    comps_are_local = scope_is_local_enough(benchmark.scope, a.max_benchmark_scope)
    if not comps_are_local:
        flags.append(
            f"no local comps: benchmark borrowed from {benchmark.scope} level"
        )
    if uplift > a.max_resale_uplift:
        flags.append(
            f"implausible uplift ({uplift:.1f}x asking) - comps likely "
            "non-comparable, verify before trusting"
        )

    confidence = _confidence(benchmark, condition.label, a)
    if not comps_are_local or uplift > a.max_resale_uplift:
        confidence = "low"

    return DealAnalysis(
        ref=row["ref"],
        url=row["url"],
        title=row["title"],
        town=row["town"],
        district=row["district"],
        property_type=row["property_type"],
        area_m2=area,
        bedrooms=row["bedrooms"],
        asking_price=price,
        asking_ppm2=round(asking_ppm2, 1),
        condition_label=condition.label,
        condition_score=condition.score,
        condition_signals="; ".join(condition.signals),
        reno_depth=depth,
        resale_ppm2=round(benchmark.resale_ppm2, 1),
        benchmark_scope=benchmark.scope,
        benchmark_key=benchmark.key,
        n_comps=benchmark.n_comps,
        market_median_ppm2=round(benchmark.median_ppm2, 1),
        resale_uplift_ratio=uplift,
        comps_are_local=comps_are_local,
        purchase_price=round(purchase_price),
        purchase_fees=round(purchase_fees),
        reno_cost=round(reno_cost),
        holding_cost=round(holding_cost),
        all_in_cost=round(all_in),
        gross_resale=round(gross_resale),
        selling_costs=round(selling_costs),
        net_resale=round(net_resale),
        profit_usd=round(profit),
        roi_pct=round(roi * 100, 1),
        margin_pct=round(margin * 100, 1),
        discount_to_market_pct=round(discount * 100, 1),
        price_cuts=row["price_changes"] or 0,
        days_tracked=days_tracked,
        confidence=confidence,
        flags="; ".join(flags),
    )


def _passes_screens(deal: DealAnalysis, a: Assumptions) -> bool:
    """Return True if a deal clears both the return and the credibility gates.

    The credibility gates matter more than the return gates: a listing valued
    against stock from another town will always look spectacular, and letting
    those to the top of the table would make the whole report useless.
    """
    if deal.profit_usd < a.min_profit_usd:
        return False
    if deal.roi_pct < a.min_roi * 100:
        return False
    if not deal.comps_are_local:
        return False
    if deal.resale_uplift_ratio > a.max_resale_uplift:
        return False
    return True


def _confidence(benchmark: Benchmark, condition_label: str, a: Assumptions) -> str:
    score = 0
    if benchmark.n_comps >= a.min_comps * 3:
        score += 2
    elif benchmark.n_comps >= a.min_comps:
        score += 1
    if benchmark.scope == "town":
        score += 2
    elif benchmark.scope == "district":
        score += 1
    if condition_label in ("renovation_target", "finished"):
        score += 1
    return "high" if score >= 4 else "medium" if score >= 2 else "low"


def rank_deals(
    rows: Sequence[sqlite3.Row],
    a: Assumptions,
    comps: CompsIndex | None = None,
    apply_screens: bool = True,
    conditions: Sequence[str] | None = None,
) -> list[DealAnalysis]:
    """Analyse every listing and return them ranked by expected profit.

    `conditions` restricts the output to given condition labels. This matters
    because the ranking mixes two different theses: a `renovation_target` that
    is cheap because it needs work (add value by renovating), and a `finished`
    listing that is cheap relative to its comps (buy under market). The second
    is usually explained by something the data cannot see -- floor, view, exact
    street, building age -- so filter to the first if you want the flip thesis
    specifically.
    """
    comps = comps or CompsIndex(rows, a)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    deals: list[DealAnalysis] = []

    for row in rows:
        if a.max_price_usd and row["price_usd"] and row["price_usd"] > a.max_price_usd:
            continue
        if a.min_area_m2 and row["area_m2"] and row["area_m2"] < a.min_area_m2:
            continue

        days = 0
        try:
            first_seen = datetime.fromisoformat(row["first_seen"])
            days = max(0, (now - first_seen).days)
        except (TypeError, ValueError):
            pass

        deal = analyse_listing(row, comps, a, days_tracked=days)
        if deal is None:
            continue
        if conditions and deal.condition_label not in conditions:
            continue
        if apply_screens and not _passes_screens(deal, a):
            continue
        deals.append(deal)

    deals.sort(key=lambda d: d.profit_usd, reverse=True)
    return deals


def market_summary(rows: Sequence[sqlite3.Row], a: Assumptions) -> list[dict]:
    """Per-town market table: supply, price spread, and finished-vs-target mix."""
    by_town: dict[str, dict] = {}
    for row in rows:
        town = row["town"] or "(unknown)"
        ppm2 = _ppm2(row)
        if ppm2 is None:
            continue
        entry = by_town.setdefault(
            town,
            {
                "town": town,
                "district": row["district"],
                "listings": 0,
                "ppm2": [],
                "targets": 0,
                "finished": 0,
            },
        )
        entry["listings"] += 1
        entry["ppm2"].append(ppm2)
        condition = assess(row["title"], row["description"])
        if condition.is_renovation_target:
            entry["targets"] += 1
        elif condition.is_finished:
            entry["finished"] += 1

    summary = []
    for entry in by_town.values():
        values = entry["ppm2"]
        if len(values) < 3:
            continue
        summary.append(
            {
                "town": entry["town"],
                "district": entry["district"],
                "listings": entry["listings"],
                "median_ppm2": round(statistics.median(values)),
                "p25_ppm2": round(percentile(values, 0.25)),
                "p75_ppm2": round(percentile(values, 0.75)),
                "spread_pct": round(
                    (percentile(values, 0.75) - percentile(values, 0.25))
                    / statistics.median(values)
                    * 100
                ),
                "renovation_targets": entry["targets"],
                "finished": entry["finished"],
            }
        )
    # Wide spread between tired and finished stock is where flips live.
    summary.sort(key=lambda e: (e["spread_pct"], e["listings"]), reverse=True)
    return summary
