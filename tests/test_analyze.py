"""Tests for the condition classifier, comps engine and margin model."""

import sqlite3

import pytest

from jskre.analyze import (
    Assumptions,
    CompsIndex,
    _passes_screens,
    analyse_listing,
    market_summary,
    percentile,
    rank_deals,
    reno_depth_for,
    scope_is_local_enough,
)
from jskre.condition import assess

# ------------------------------------------------------------------ condition


@pytest.mark.parametrize(
    "text",
    [
        "Apartment needs full renovation, sold as-is",
        "Core & shell apartment, buyer to finish",
        "Old building, needs work throughout",
        "Unfinished apartment without finishing",
    ],
)
def test_renovation_targets_detected(text):
    assert assess(text).label == "renovation_target"


@pytest.mark.parametrize(
    "text",
    [
        "Brand new apartment, never used",
        "Fully renovated and decorated, turnkey",
        "Deluxe apartment in excellent condition, high-end finishes",
    ],
)
def test_finished_stock_detected(text):
    assert assess(text).label == "finished"


def test_silent_listing_is_unknown():
    result = assess("Apartment for sale in Jbeil", "150 m2, 3 bedrooms, parking")
    assert result.label == "unknown"
    assert result.signals == ()


def test_empty_input_is_unknown():
    assert assess(None, "").label == "unknown"


def test_score_is_bounded_even_with_many_signals():
    piled_on = (
        "core & shell unfinished needs renovation requires work as-is "
        "fixer-upper demolition black work without finishing old building"
    )
    assert -1.0 <= assess(piled_on).score <= -0.9


def test_signals_are_reported_once_each():
    result = assess("renovated, renovated, fully renovated apartment")
    assert len(result.signals) == len(set(result.signals))


def test_conflicting_text_lands_between_extremes():
    # "Renovated kitchen but bathrooms need work" should not read as turnkey.
    result = assess("Recently renovated kitchen, bathrooms need work, old building")
    assert result.label in ("neutral", "renovation_target")


@pytest.mark.parametrize(
    "label,score,expected",
    [
        ("renovation_target", -0.9, "full"),
        ("renovation_target", -0.3, "medium"),
        ("neutral", 0.0, "light"),
        ("finished", 0.8, "light"),
    ],
)
def test_reno_depth_mapping(label, score, expected):
    assert reno_depth_for(label, score) == expected


# ---------------------------------------------------------------- percentiles


def test_percentile_interpolates():
    values = [100, 200, 300, 400]
    assert percentile(values, 0.0) == 100
    assert percentile(values, 1.0) == 400
    assert percentile(values, 0.5) == 250


def test_percentile_single_value():
    assert percentile([42], 0.9) == 42


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        percentile([], 0.5)


# ---------------------------------------------------------------------- comps


def make_row(**kwargs) -> sqlite3.Row:
    """Build a Row with the columns the analyser reads."""
    defaults = {
        "ref": "L1",
        "url": "/properties/x-l1",
        "title": "Apartment",
        "description": "",
        "property_type": "Apartment",
        "price_usd": 200_000,
        "area_m2": 150.0,
        "bedrooms": 3.0,
        "bathrooms": 2.0,
        "town": "Jbeil",
        "district": "Jbeil",
        "governorate": "Mount Lebanon",
        "price_per_m2": None,
        "price_changes": 0,
        "description_truncated": 0,
        "first_seen": "2026-01-01T00:00:00+00:00",
    }
    defaults.update(kwargs)
    if defaults["price_per_m2"] is None and defaults["area_m2"]:
        defaults["price_per_m2"] = defaults["price_usd"] / defaults["area_m2"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(f"CREATE TABLE t ({cols})")
    conn.execute(f"INSERT INTO t VALUES ({placeholders})", list(defaults.values()))
    return conn.execute("SELECT * FROM t").fetchone()


def finished_rows(town="Jbeil", ppm2=2000, n=10, area=150.0):
    return [
        make_row(
            ref=f"F{i}",
            title="Fully renovated turnkey apartment",
            description="Brand new, decorated, high-end finishes",
            town=town,
            price_usd=int(ppm2 * area),
            area_m2=area,
        )
        for i in range(n)
    ]


def test_comps_benchmark_uses_finished_stock_only():
    rows = finished_rows(ppm2=2000, n=8) + [
        make_row(
            ref="T1",
            title="Apartment needs full renovation",
            description="core & shell, as-is",
            price_usd=90_000,
            area_m2=150.0,
        )
    ]
    comps = CompsIndex(rows, Assumptions())
    benchmark = comps.benchmark_for("Jbeil", "Jbeil", "Mount Lebanon", 150.0, "Apartment")

    assert benchmark is not None
    assert benchmark.n_comps == 8  # the shell listing is excluded
    assert benchmark.resale_ppm2 == pytest.approx(2000, abs=1)
    assert benchmark.scope == "town"


def test_benchmark_falls_back_to_wider_scope_when_town_is_thin():
    rows = finished_rows(town="Bsalim", ppm2=2500, n=9)
    comps = CompsIndex(rows, Assumptions())
    # Subject is in a town with no comps but the same district.
    benchmark = comps.benchmark_for(
        "Tiny Village", "Jbeil", "Mount Lebanon", 150.0, "Apartment"
    )
    assert benchmark is not None
    assert benchmark.scope in ("district", "governorate", "global")


def test_outlier_comp_is_excluded_from_benchmark():
    rows = finished_rows(ppm2=2000, n=10)
    rows.append(
        make_row(
            ref="TROPHY",
            title="Brand new luxury penthouse",
            description="turnkey, high-end",
            price_usd=5_000_000,
            area_m2=150.0,  # ~33k/m2, way beyond 3x median
        )
    )
    comps = CompsIndex(rows, Assumptions())
    benchmark = comps.benchmark_for("Jbeil", "Jbeil", "Mount Lebanon", 150.0, "Apartment")
    assert benchmark.high_ppm2 < 10_000


def test_comps_index_with_no_finished_stock_returns_none():
    rows = [
        make_row(ref="T1", title="Needs renovation", description="as-is shell"),
    ]
    comps = CompsIndex(rows, Assumptions())
    assert comps.benchmark_for("Jbeil", "Jbeil", "Mount Lebanon", 150.0, "Apartment") is None


# --------------------------------------------------------------- margin model


def test_underpriced_shell_shows_profit():
    market = finished_rows(ppm2=2000, n=10)
    subject = make_row(
        ref="DEAL",
        title="Apartment needs full renovation in Jbeil",
        description="Core & shell, sold as-is, buyer to finish",
        price_usd=120_000,
        area_m2=150.0,
    )
    comps = CompsIndex(market + [subject], Assumptions())
    deal = analyse_listing(subject, comps, Assumptions())

    assert deal is not None
    assert deal.condition_label == "renovation_target"
    assert deal.reno_depth == "full"
    # Exit at the 60th percentile of a flat 2000/m2 market on 150 m2.
    assert deal.gross_resale == pytest.approx(300_000, abs=1000)
    assert deal.profit_usd > 0
    assert deal.roi_pct > 0
    # All-in must include every cost component.
    assert deal.all_in_cost > deal.purchase_price
    assert deal.reno_cost > 150 * 550  # full rate plus contingency


def test_overpriced_finished_flat_shows_loss():
    market = finished_rows(ppm2=2000, n=10)
    subject = make_row(
        ref="BAD",
        title="Brand new decorated apartment",
        description="turnkey, high-end finishes",
        price_usd=400_000,
        area_m2=150.0,
    )
    comps = CompsIndex(market + [subject], Assumptions())
    deal = analyse_listing(subject, comps, Assumptions())
    assert deal.profit_usd < 0
    assert "asking above modelled resale" in deal.flags


def test_costs_reconcile_to_profit():
    market = finished_rows(ppm2=2200, n=10)
    subject = make_row(ref="X", price_usd=150_000, area_m2=140.0)
    a = Assumptions()
    comps = CompsIndex(market + [subject], a)
    deal = analyse_listing(subject, comps, a)

    expected_all_in = (
        deal.purchase_price + deal.purchase_fees + deal.reno_cost + deal.holding_cost
    )
    assert deal.all_in_cost == pytest.approx(expected_all_in, abs=2)
    assert deal.net_resale == pytest.approx(deal.gross_resale - deal.selling_costs, abs=2)
    assert deal.profit_usd == pytest.approx(deal.net_resale - deal.all_in_cost, abs=2)


def test_zero_area_listing_is_skipped():
    market = finished_rows(n=10)
    subject = make_row(ref="Z", area_m2=0, price_usd=100_000, price_per_m2=0)
    comps = CompsIndex(market, Assumptions())
    assert analyse_listing(subject, comps, Assumptions()) is None


def test_thin_comps_are_flagged_and_lower_confidence():
    market = finished_rows(town="Rare", ppm2=2000, n=2)
    subject = make_row(ref="S", town="Rare", price_usd=100_000, area_m2=150.0)
    a = Assumptions()
    comps = CompsIndex(market + [subject], a)
    deal = analyse_listing(subject, comps, a)
    assert "thin comps" in deal.flags
    assert deal.confidence in ("low", "medium")


def test_screens_filter_by_profit_and_roi():
    market = finished_rows(ppm2=2000, n=10)
    marginal = make_row(ref="M", price_usd=255_000, area_m2=150.0)
    a = Assumptions(min_profit_usd=50_000, min_roi=0.5)
    kept = rank_deals(market + [marginal], a, apply_screens=True)
    assert all(d.profit_usd >= 50_000 and d.roi_pct >= 50 for d in kept)

    unscreened = rank_deals(market + [marginal], a, apply_screens=False)
    assert len(unscreened) > len(kept)


def test_budget_ceiling_excludes_expensive_listings():
    market = finished_rows(ppm2=2000, n=10)
    a = Assumptions(max_price_usd=100_000, min_profit_usd=0, min_roi=-1)
    deals = rank_deals(market, a, apply_screens=True)
    assert all(d.asking_price <= 100_000 for d in deals)


def test_results_are_ranked_by_profit_descending():
    market = finished_rows(ppm2=2500, n=12)
    subjects = [
        make_row(ref=f"S{i}", price_usd=price, area_m2=150.0)
        for i, price in enumerate((120_000, 90_000, 200_000))
    ]
    a = Assumptions(min_profit_usd=-10**9, min_roi=-1)
    deals = rank_deals(market + subjects, a)
    profits = [d.profit_usd for d in deals]
    assert profits == sorted(profits, reverse=True)


# ----------------------------------------------------------------- guardrails


def test_scope_is_local_enough():
    assert scope_is_local_enough("town", "district")
    assert scope_is_local_enough("district", "district")
    assert not scope_is_local_enough("governorate", "district")
    assert not scope_is_local_enough("global", "town")
    assert scope_is_local_enough("global", "global")


def test_borrowed_benchmark_is_flagged_and_screened_out():
    """A cheap town valued against another town's stock must not rank."""
    subject = make_row(
        ref="CHEAP",
        town="Basta",
        district="Beirut Cheap",       # no comps in this district either
        governorate="Mount Lebanon",
        price_usd=120_000,
        area_m2=120.0,
    )
    # Put the comps in a different district so the only match is governorate.
    comps_rows = [
        make_row(
            ref=f"F{i}",
            title="Fully renovated turnkey apartment",
            description="Brand new, decorated, high-end finishes",
            town="Achrafieh",
            district="Beirut Prime",
            governorate="Mount Lebanon",
            price_usd=int(3300 * 120),
            area_m2=120.0,
        )
        for i in range(10)
    ]
    a = Assumptions()
    comps = CompsIndex(comps_rows + [subject], a)
    deal = analyse_listing(subject, comps, a)

    assert deal.benchmark_scope == "governorate"
    assert deal.comps_are_local is False
    assert "no local comps" in deal.flags
    assert deal.confidence == "low"
    # It would otherwise show a huge profit, so it must not survive screening.
    assert deal.profit_usd > 0
    assert not _passes_screens(deal, a)


def test_implausible_uplift_is_rejected():
    market = finished_rows(town="Jbeil", ppm2=4000, n=10)
    subject = make_row(ref="TOOGOOD", town="Jbeil", price_usd=60_000, area_m2=150.0)
    a = Assumptions()
    comps = CompsIndex(market + [subject], a)
    deal = analyse_listing(subject, comps, a)

    assert deal.resale_uplift_ratio > a.max_resale_uplift
    assert "implausible uplift" in deal.flags
    assert not _passes_screens(deal, a)


def test_plausible_local_deal_still_passes():
    market = finished_rows(town="Jbeil", ppm2=2000, n=10)
    subject = make_row(
        ref="GOOD",
        town="Jbeil",
        title="Apartment needs renovation",
        description="old building, needs work",
        price_usd=150_000,
        area_m2=150.0,
    )
    a = Assumptions()
    comps = CompsIndex(market + [subject], a)
    deal = analyse_listing(subject, comps, a)

    assert deal.benchmark_scope == "town"
    assert deal.comps_are_local is True
    assert deal.resale_uplift_ratio <= a.max_resale_uplift
    assert _passes_screens(deal, a)


def test_guardrails_can_be_relaxed_by_config():
    market = finished_rows(town="Jbeil", ppm2=4000, n=10)
    subject = make_row(ref="X", town="Jbeil", price_usd=60_000, area_m2=150.0)
    relaxed = Assumptions(max_resale_uplift=99.0, max_benchmark_scope="global")
    comps = CompsIndex(market + [subject], relaxed)
    deal = analyse_listing(subject, comps, relaxed)
    assert _passes_screens(deal, relaxed)


def test_flag_and_screen_agree_at_the_uplift_boundary():
    """A row flagged as implausible must never also pass screening.

    The flag and the screen have to compare the same value: testing a raw
    ratio against a rounded stored field let 2.5004 trip the flag, be forced
    to low confidence, and still survive a `> 2.5` screen.
    """
    a = Assumptions()
    # Sweep prices so at least one lands just above the boundary.
    for price in range(58_000, 64_000, 137):
        market = finished_rows(town="Jbeil", ppm2=1000, n=10, area=150.0)
        subject = make_row(ref="EDGE", town="Jbeil", price_usd=price, area_m2=150.0)
        comps = CompsIndex(market + [subject], a)
        deal = analyse_listing(subject, comps, a)

        flagged = "implausible uplift" in deal.flags
        passes = _passes_screens(deal, a)
        assert not (flagged and passes), (
            f"price {price}: uplift {deal.resale_uplift_ratio} was flagged "
            "but still passed screening"
        )
        if flagged:
            assert deal.confidence == "low"


def test_condition_filter_isolates_the_renovation_thesis():
    market = finished_rows(town="Jbeil", ppm2=2000, n=10)
    target = make_row(
        ref="RENO",
        town="Jbeil",
        title="Apartment needs renovation",
        description="old building, needs work",
        price_usd=150_000,
        area_m2=150.0,
    )
    a = Assumptions(min_profit_usd=-10**9, min_roi=-1)
    rows = market + [target]

    everything = rank_deals(rows, a)
    assert any(d.condition_label == "finished" for d in everything)

    reno_only = rank_deals(rows, a, conditions=["renovation_target"])
    assert [d.ref for d in reno_only] == ["RENO"]


def test_no_screens_shows_rejected_deals_anyway():
    market = finished_rows(town="Jbeil", ppm2=4000, n=10)
    subject = make_row(ref="X", town="Jbeil", price_usd=60_000, area_m2=150.0)
    a = Assumptions()
    rows = market + [subject]
    assert any(d.ref == "X" for d in rank_deals(rows, a, apply_screens=False))
    assert not any(d.ref == "X" for d in rank_deals(rows, a, apply_screens=True))


# -------------------------------------------------------------------- summary


def test_market_summary_reports_spread_and_mix():
    # Spread the finished stock across a range so the percentiles are distinct.
    rows = [
        make_row(
            ref=f"F{i}",
            town="Jbeil",
            title="Fully renovated turnkey apartment",
            description="Brand new, decorated, high-end finishes",
            price_usd=int(ppm2 * 150),
            area_m2=150.0,
        )
        for i, ppm2 in enumerate((2000, 2200, 2400, 2600, 2800, 3000))
    ] + [
        make_row(
            ref=f"T{i}",
            town="Jbeil",
            title="Needs renovation",
            description="old building, as-is",
            price_usd=int(ppm2 * 150),
            area_m2=150.0,
        )
        for i, ppm2 in enumerate((900, 1000, 1100, 1200))
    ]
    summary = market_summary(rows, Assumptions())
    jbeil = next(entry for entry in summary if entry["town"] == "Jbeil")

    assert jbeil["listings"] == 10
    assert jbeil["renovation_targets"] == 4
    assert jbeil["finished"] == 6
    assert jbeil["p25_ppm2"] < jbeil["p75_ppm2"]
    assert jbeil["p25_ppm2"] <= jbeil["median_ppm2"] <= jbeil["p75_ppm2"]
    assert jbeil["spread_pct"] > 0


def test_market_summary_skips_towns_with_too_few_listings():
    rows = finished_rows(town="Lonely", n=2)
    assert market_summary(rows, Assumptions()) == []
