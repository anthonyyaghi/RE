"""Tests for the web API.

The request tests run a real server on an ephemeral port and talk to it over
HTTP, so routing, JSON encoding and query parsing are all exercised the way the
browser exercises them.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from jskre.analyze import Assumptions
from jskre.db import Database
from jskre.parse import Listing
from jskre.server import (
    Job,
    _replace_yaml_block,
    assumptions_from_query,
    make_server,
)

# ------------------------------------------------------- query -> assumptions


def test_query_overrides_are_applied():
    base = Assumptions()
    merged = assumptions_from_query(base, {"reno_cost_medium": ["500"], "min_roi": ["0.3"]})
    assert merged.reno_cost_medium == 500.0
    assert merged.min_roi == 0.3
    # Untouched fields keep the saved value.
    assert merged.reno_cost_light == base.reno_cost_light


def test_query_ignores_unknown_and_unparseable_values():
    base = Assumptions()
    merged = assumptions_from_query(
        base, {"not_a_field": ["9"], "reno_cost_full": ["abc"], "min_comps": [""]}
    )
    assert merged.reno_cost_full == base.reno_cost_full
    assert merged.min_comps == base.min_comps


def test_query_handles_null_budget_and_string_scope():
    merged = assumptions_from_query(
        Assumptions(), {"max_price_usd": ["null"], "max_benchmark_scope": ["town"]}
    )
    assert merged.max_price_usd is None
    assert merged.max_benchmark_scope == "town"


def test_integer_fields_stay_integers():
    merged = assumptions_from_query(Assumptions(), {"holding_months": ["9.0"]})
    assert merged.holding_months == 9
    assert isinstance(merged.holding_months, int)


# ------------------------------------------------------------ yaml rewriting


def test_replace_yaml_block_preserves_surrounding_content():
    raw = (
        "database:\n  path: data/x.db\n\n"
        "assumptions:\n  # a comment that must survive elsewhere\n  min_roi: 0.1\n\n"
        "http:\n  delay_seconds: 1.5\n"
    )
    out = _replace_yaml_block(raw, "assumptions", "assumptions:\n  min_roi: 0.4\n")
    assert "database:\n  path: data/x.db" in out
    assert "http:\n  delay_seconds: 1.5" in out
    assert "min_roi: 0.4" in out
    assert "min_roi: 0.1" not in out


def test_replace_yaml_block_appends_when_key_absent():
    out = _replace_yaml_block("database:\n  path: x\n", "assumptions", "assumptions:\n  a: 1\n")
    assert "database:" in out and "assumptions:" in out


# --------------------------------------------------------------------- jobs


def test_only_one_job_runs_at_a_time(tmp_path):
    from jskre.server import JobRunner

    runner = JobRunner(str(tmp_path / "j.db"), {})
    blocker = Job("scrape", {})
    runner.jobs.append(blocker)          # pretend one is already running
    job, error = runner.start("scrape", {})
    assert job is None
    assert "already running" in error


def test_job_reports_stop_request():
    job = Job("scrape", {"category": "for-sale"})
    assert job.should_stop() is False
    job.request_stop()
    assert job.should_stop() is True
    assert job.as_dict()["stop_requested"] is True


# ------------------------------------------------------------ live requests


def make_listing(ref, price, area, town, title, description):
    return Listing(
        ref=ref, url=f"/properties/x-{ref.lower()}", title=title,
        property_type="Apartment", price_usd=price, area_m2=area,
        bedrooms=3.0, bathrooms=2.0, town=town, district="D", governorate="G",
        location_raw=f"{town}, D, G", description=description,
    )


@pytest.fixture
def live_server(tmp_path):
    db_path = tmp_path / "srv.db"
    with Database(db_path) as db:
        for i in range(9):
            db.upsert(make_listing(
                f"F{i}", 300_000, 150.0, "Jbeil",
                "Fully renovated turnkey apartment",
                "Brand new, decorated, high-end finishes",
            ), category="apartment-for-sale")
        db.upsert(make_listing(
            "T1", 150_000, 150.0, "Jbeil", "Apartment needs renovation",
            "old building, needs work throughout",
        ), category="apartment-for-sale")

    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "database:\n  path: srv.db\n\nassumptions:\n  min_profit_usd: 0\n  min_roi: 0.0\n",
        encoding="utf-8",
    )

    httpd = make_server("127.0.0.1", 0, str(db_path), str(config_path))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.loads(response.read())


def post(base, path, payload):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_bootstrap_reports_state(live_server):
    data = get(live_server, "/api/bootstrap")
    assert data["stats"]["active"] == 10
    assert "Jbeil" in data["towns"]
    assert data["assumptions"]["min_profit_usd"] == 0
    assert "apartment-for-sale" in data["categories"]


def test_candidates_respond_to_assumption_overrides(live_server):
    subject = lambda payload: next(  # noqa: E731
        c for c in payload["candidates"] if c["ref"] == "T1"
    )
    # T1's text scores low enough to map to a full gut, so it is the *full*
    # rate that applies to it -- assert against the rate the model actually
    # uses, and pin the mapping so a change to it fails loudly here.
    cheap = get(live_server, "/api/candidates?screens=0&reno_cost_full=50")
    dear = get(live_server, "/api/candidates?screens=0&reno_cost_full=900")
    assert subject(cheap)["reno_depth"] == "full"
    assert subject(cheap)["profit_usd"] > subject(dear)["profit_usd"]

    # And a rate for a depth this listing does not use must not move anything.
    other = get(live_server, "/api/candidates?screens=0&reno_cost_light=999")
    assert subject(other)["profit_usd"] == subject(
        get(live_server, "/api/candidates?screens=0")
    )["profit_usd"]


def test_candidates_condition_filter(live_server):
    data = get(live_server, "/api/candidates?condition=renovation_target")
    assert data["candidates"]
    assert {c["condition_label"] for c in data["candidates"]} == {"renovation_target"}


def test_candidates_town_filter_keeps_market_wide_comps(live_server):
    data = get(live_server, "/api/candidates?town=Jbeil")
    assert data["analysed"] == 10
    assert data["market_size"] == 10


def test_listings_pagination_and_search(live_server):
    page = get(live_server, "/api/listings?per_page=4&page=2")
    assert page["page"] == 2 and page["per_page"] == 4
    assert len(page["listings"]) == 4

    found = get(live_server, "/api/listings?q=needs%20renovation")
    assert [l["ref"] for l in found["listings"]] == ["T1"]


def test_listing_detail_includes_deal_and_peers(live_server):
    data = get(live_server, "/api/listings/T1")
    assert data["ref"] == "T1"
    assert data["deal"]["condition_label"] == "renovation_target"
    assert data["peers"]
    assert all(p["ref"] != "T1" for p in data["peers"])


def test_unknown_listing_is_404(live_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(live_server, "/api/listings/NOPE")
    assert excinfo.value.code == 404


def test_unknown_endpoint_is_404(live_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(live_server, "/api/nonsense")
    assert excinfo.value.code == 404


def test_market_and_changes_endpoints(live_server):
    market = get(live_server, "/api/market")
    assert any(entry["town"] == "Jbeil" for entry in market["market"])
    changes = get(live_server, "/api/changes?days=30")
    assert changes["days"] == 30
    assert changes["price_cuts"] == [] and changes["delisted"] == []


def test_saving_assumptions_writes_config_and_reloads(live_server, tmp_path):
    result = post(live_server, "/api/assumptions",
                  {"assumptions": {"reno_cost_medium": 425}})
    assert result["saved"] is True

    text = open(result["path"], encoding="utf-8").read()
    assert "reno_cost_medium: 425" in text
    # The untouched database block must survive the rewrite.
    assert "database:" in text
    # And the server should now serve the new default.
    assert get(live_server, "/api/bootstrap")["assumptions"]["reno_cost_medium"] == 425.0


def test_saving_rejects_non_numeric(live_server):
    request = urllib.request.Request(
        live_server + "/api/assumptions",
        data=json.dumps({"assumptions": {"reno_cost_medium": "banana"}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 400


def test_bad_job_kind_is_rejected(live_server):
    request = urllib.request.Request(
        live_server + "/api/jobs",
        data=json.dumps({"kind": "mystery"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 400


def test_static_frontend_is_served(live_server):
    with urllib.request.urlopen(live_server + "/", timeout=10) as response:
        body = response.read().decode()
    assert "JSK deal finder" in body


def test_path_traversal_is_refused(live_server):
    # urllib normalises ../ away, so send the encoded form.
    try:
        with urllib.request.urlopen(live_server + "/%2e%2e/config.yml", timeout=10) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as exc:
        assert exc.code in (403, 404)
        return
    # If it resolved, it must have fallen back to the app shell, not the file.
    assert "assumptions:" not in body
