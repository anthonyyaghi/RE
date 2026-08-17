"""Tests for structured feature extraction and the comp adjustment machinery."""

import math

import pytest

from jskre.features import extract, effective_indoor_m2, summary
from jskre.analyze import (
    ADJUST_FEATURES,
    Assumptions,
    CompsIndex,
    feature_vector,
    fit_within_town_betas,
)
from tests.test_analyze import make_row

# ---------------------------------------------------------------- extraction


def test_bullet_list_extraction():
    f = extract(
        "Deluxe Apartment | Sea View | Achrafieh",
        "- 131 m² Indoor\n- 50 m2 Terrace\n- Maid's Room\n- Cave\n"
        "- 3 Parking Spaces\n- 2 Balconies\n- Chimney",
    )
    assert f.indoor_m2 == 131.0
    assert f.terrace_m2 == 50.0
    assert f.sea_view and f.maid_room and f.storage and f.chimney
    assert f.parking_spaces == 3
    assert f.balconies == 2


def test_prose_variants():
    f = extract(None, "terrace of 30 sqm, garden: 105 m2, 1 underground parking space")
    assert f.terrace_m2 == 30.0
    assert f.garden_m2 == 105.0
    assert f.parking_spaces == 1


def test_unnumbered_parking_counts_as_one():
    assert extract("x", "parking available").parking_spaces == 1
    assert extract("x", "no mention of anything").parking_spaces == 0


def test_new_build_markers():
    assert extract("Under Construction l 3 Bedrooms", "").new_build
    assert extract("x", "20% Down Payment plan available").new_build
    assert not extract("Fully renovated apartment", "turnkey").new_build


def test_floor_extraction_bounds():
    assert extract("x", "on the 4th floor").floor == 4
    # An implausible floor is rejected rather than stored.
    assert extract("x", "floor 99").floor is None


def test_empty_input():
    f = extract(None, "")
    assert f.parking_spaces == 0 and not f.sea_view and f.indoor_m2 is None


def test_summary_is_compact_and_covers_key_features():
    f = extract("Sea view apartment", "- 45 m² terrace\n- 2 parking spaces\n- maid's room")
    s = summary(f)
    assert "sea view" in s
    assert "terrace 45m²" in s
    assert "parking ×2" in s
    assert "maid's room" in s


# ---------------------------------------------------------- effective area


def test_effective_area_trusts_stated_indoor_when_smaller():
    f = extract("x", "- 131 m² indoor\n- 90 m2 terrace")
    assert effective_indoor_m2(221.0, f) == 131.0


def test_effective_area_keeps_headline_when_it_matches_indoor():
    f = extract("x", "- 160 m² indoor\n- 30 m2 terrace")
    assert effective_indoor_m2(160.0, f) == 160.0


def test_effective_area_without_indoor_statement_is_headline():
    f = extract("x", "- 30 m2 terrace")
    assert effective_indoor_m2(160.0, f) == 160.0
    assert effective_indoor_m2(None, f) is None


# ------------------------------------------------------------- beta fitting


def synth_town(town, n, base, view_premium):
    """n listings, half with sea view at a multiplicative premium."""
    out = []
    for i in range(n):
        has_view = i % 2 == 0
        ppm2 = base * (view_premium if has_view else 1.0)
        f = extract("sea view" if has_view else "plain", "")
        out.append((town, math.log(ppm2), feature_vector(f)))
    return out


def test_fitted_beta_recovers_a_known_premium():
    samples = synth_town("A", 40, 2000, 1.20) + synth_town("B", 40, 900, 1.20)
    betas = fit_within_town_betas(samples)
    idx = ADJUST_FEATURES.index("sea_view")
    assert betas[idx] == pytest.approx(math.log(1.20), abs=0.02)
    # Features that never varied stay at zero.
    assert betas[ADJUST_FEATURES.index("garden")] == pytest.approx(0.0, abs=1e-6)


def test_too_little_data_yields_zero_betas():
    assert fit_within_town_betas(synth_town("A", 10, 2000, 1.3)) == [0.0] * len(ADJUST_FEATURES)


def test_betas_are_clamped():
    samples = synth_town("A", 200, 2000, 3.0)  # absurd 3x premium
    betas = fit_within_town_betas(samples)
    assert abs(betas[ADJUST_FEATURES.index("sea_view")]) <= 0.25


# ------------------------------------------------- feature-adjusted comps


def viewy_market(n=80, base=2000, premium=1.25):
    """Finished comps in one town, half with sea view priced at a premium.

    Prices carry a small deterministic jitter so no percentile of the pool
    degenerates onto a single value -- a two-point distribution makes the raw
    60th percentile coincide exactly with the adjusted one and turns strict
    inequalities in the assertions into flaky equalities.
    """
    rows = []
    for i in range(n):
        has_view = i % 2 == 0
        jitter = 1 + (i % 5) * 0.03
        rows.append(make_row(
            ref=f"C{i}",
            title="Fully renovated turnkey apartment" + (" with sea view" if has_view else ""),
            description="Brand new, decorated" + (", sea view" if has_view else ""),
            price_usd=int(base * jitter * (premium if has_view else 1.0) * 150),
            area_m2=150.0,
        ))
    return rows


def test_benchmark_adjusts_for_subject_without_the_feature():
    a = Assumptions()
    rows = viewy_market()
    comps = CompsIndex(rows, a)
    from jskre.features import extract as ex

    plain = comps.benchmark_for("Jbeil", "Jbeil", "Mount Lebanon", 150.0,
                                "Apartment", subject_features=ex("plain flat", ""))
    viewed = comps.benchmark_for("Jbeil", "Jbeil", "Mount Lebanon", 150.0,
                                 "Apartment", subject_features=ex("sea view flat", "sea view"))
    raw = comps.benchmark_for("Jbeil", "Jbeil", "Mount Lebanon", 150.0, "Apartment")

    # A subject without the view must be benchmarked below one with it.
    assert plain.resale_ppm2 < viewed.resale_ppm2
    # And the unadjusted benchmark sits between the two.
    assert plain.resale_ppm2 < raw.resale_ppm2 < viewed.resale_ppm2
    assert plain.adjustment_pct < 0 < viewed.adjustment_pct


def test_adjustment_is_noop_when_data_too_thin():
    a = Assumptions()
    rows = viewy_market(n=8)  # below the fitting threshold
    comps = CompsIndex(rows, a)
    assert comps.betas == [0.0] * len(ADJUST_FEATURES)
    from jskre.features import extract as ex
    b = comps.benchmark_for("Jbeil", "Jbeil", "Mount Lebanon", 150.0,
                            "Apartment", subject_features=ex("plain", ""))
    assert b.adjustment_pct == 0.0


# ------------------------------------------------------ new-build screening


def test_new_build_is_flagged_and_screened_out():
    from jskre.analyze import analyse_listing, _passes_screens, rank_deals

    a = Assumptions(min_profit_usd=-10**9, min_roi=-1)
    market = viewy_market()
    subject = make_row(
        ref="UC1",
        title="Under Construction l 3 Bedrooms l Jbeil",
        description="- 140 m2\n- 20% Down Payment\n- Delivery 2027",
        price_usd=150_000, area_m2=140.0,
    )
    comps = CompsIndex(market + [subject], a)
    deal = analyse_listing(subject, comps, a)

    assert deal.is_new_build
    assert deal.condition_label != "renovation_target"
    assert "new build" in deal.flags
    assert not _passes_screens(deal, Assumptions())
    assert not any(d.ref == "UC1" for d in rank_deals(market + [subject], Assumptions()))


def test_bundled_outdoor_area_is_corrected():
    from jskre.analyze import analyse_listing

    a = Assumptions(min_profit_usd=-10**9, min_roi=-1)
    market = viewy_market()
    subject = make_row(
        ref="BND",
        title="Apartment with terrace in Jbeil",
        description="- 100 m² indoor\n- 60 m2 terrace",
        price_usd=200_000, area_m2=160.0,   # headline bundles the terrace
    )
    comps = CompsIndex(market + [subject], a)
    deal = analyse_listing(subject, comps, a)

    assert deal.effective_area_m2 == 100.0
    assert deal.asking_ppm2 == pytest.approx(2000.0)   # 200k / 100m², not /160
    assert "bundles outdoor space" in deal.flags
