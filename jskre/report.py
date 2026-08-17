"""Output: CSV for spreadsheet work, HTML for reading.

CSV is the primary artefact -- the whole point is to get this into a tool where
you can re-sort and re-filter by hand. The HTML report is a readable snapshot of
the same numbers plus the market table.
"""

from __future__ import annotations

import csv
import html
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .analyze import Assumptions, DealAnalysis

DEAL_COLUMNS = [f.name for f in dataclass_fields(DealAnalysis)]


def write_deals_csv(deals: Sequence[DealAnalysis], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DEAL_COLUMNS)
        writer.writeheader()
        for deal in deals:
            writer.writerow(deal.as_dict())
    return path


def write_market_csv(summary: Sequence[dict], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not summary:
        path.write_text("", encoding="utf-8")
        return path
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    return path


# ----------------------------------------------------------------------- HTML

_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e2e2e2;
  --accent: #AD2F36; --good: #1a7f4b; --warn: #a06000; --chip: #f4f4f5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #16181c; --fg: #e8e8ea; --muted: #9a9aa2; --line: #2c2f36;
    --accent: #e2646c; --good: #4ec98a; --warn: #d9a13a; --chip: #22252b;
  }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width: 1400px; margin: 0 auto; }
h1 { font-size:1.6rem; margin:0 0 .25rem; }
h2 { font-size:1.15rem; margin:2.5rem 0 .75rem; padding-bottom:.4rem;
  border-bottom:2px solid var(--accent); }
.meta { color:var(--muted); font-size:.875rem; margin-bottom:1.5rem; }
.note { background:var(--chip); border-left:3px solid var(--accent);
  padding:.75rem 1rem; border-radius:6px; margin:1rem 0; font-size:.875rem; }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; font-size:.83rem; }
th, td { padding:.5rem .6rem; text-align:left; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { background:var(--chip); font-weight:600; position:sticky; top:0; }
tbody tr:hover { background:var(--chip); }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.pos { color:var(--good); font-weight:600; }
.neg { color:var(--accent); }
.title-cell { white-space:normal; min-width:230px; max-width:330px; }
a { color:var(--accent); }
.chip { display:inline-block; padding:.1rem .45rem; border-radius:99px;
  background:var(--chip); font-size:.72rem; border:1px solid var(--line); }
.flags { color:var(--warn); font-size:.72rem; white-space:normal; max-width:220px; }
dl.assump { display:grid; grid-template-columns:auto auto; gap:.2rem 1rem;
  font-size:.83rem; margin:0; }
dl.assump dt { color:var(--muted); }
dl.assump dd { margin:0; font-variant-numeric:tabular-nums; }
footer { margin-top:3rem; color:var(--muted); font-size:.8rem; }
"""


def _thesis_split(deals: Sequence[DealAnalysis]) -> str:
    """One sentence describing how the shortlist breaks down by condition."""
    if not deals:
        return "No candidates passed screening."
    counts: dict[str, int] = {}
    for deal in deals:
        counts[deal.condition_label] = counts.get(deal.condition_label, 0) + 1
    order = ("renovation_target", "neutral", "unknown", "finished")
    parts = [
        f"{counts[label]} {label.replace('_', ' ')}"
        for label in order
        if counts.get(label)
    ]
    return "Of these, " + ", ".join(parts) + "."


def _money(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.0f}"


def _cls(value: float) -> str:
    return "pos" if value > 0 else "neg" if value < 0 else ""


def write_html_report(
    deals: Sequence[DealAnalysis],
    market: Sequence[dict],
    assumptions: Assumptions,
    stats: dict,
    path: str | Path,
    top_n: int = 100,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = []
    for d in deals[:top_n]:
        rows.append(
            f"""<tr>
  <td class="title-cell"><a href="https://www.jskre.com{html.escape(d.url)}"
      target="_blank" rel="noopener">{html.escape(d.title or d.ref)}</a><br>
      <span class="chip">{html.escape(d.ref)}</span>
      <span class="chip">{html.escape(d.condition_label)}</span>
      <span class="chip">reno: {html.escape(d.reno_depth)}</span></td>
  <td>{html.escape(d.town or "—")}</td>
  <td class="num">{d.area_m2:,.0f}</td>
  <td class="num">{_money(d.asking_price)}</td>
  <td class="num">{d.asking_ppm2:,.0f}</td>
  <td class="num">{d.resale_ppm2:,.0f}</td>
  <td class="num">{d.discount_to_market_pct:+.0f}%</td>
  <td class="num">{_money(d.reno_cost)}</td>
  <td class="num">{_money(d.all_in_cost)}</td>
  <td class="num">{_money(d.net_resale)}</td>
  <td class="num {_cls(d.profit_usd)}">{_money(d.profit_usd)}</td>
  <td class="num {_cls(d.roi_pct)}">{d.roi_pct:+.1f}%</td>
  <td>{html.escape(d.confidence)} <span class="chip">n={d.n_comps}</span></td>
  <td class="flags">{html.escape(d.flags)}</td>
</tr>"""
        )

    market_rows = [
        f"""<tr><td>{html.escape(m["town"] or "—")}</td>
  <td>{html.escape(m["district"] or "—")}</td>
  <td class="num">{m["listings"]}</td>
  <td class="num">{m["p25_ppm2"]:,}</td>
  <td class="num">{m["median_ppm2"]:,}</td>
  <td class="num">{m["p75_ppm2"]:,}</td>
  <td class="num">{m["spread_pct"]}%</td>
  <td class="num">{m["renovation_targets"]}</td>
  <td class="num">{m["finished"]}</td></tr>"""
        for m in market[:60]
    ]

    a = assumptions
    assumption_items = [
        ("Negotiation discount", f"{a.negotiation_discount:.0%}"),
        ("Purchase fees", f"{a.purchase_fees_pct:.1%}"),
        ("Reno light / medium / full",
         f"${a.reno_cost_light:.0f} / ${a.reno_cost_medium:.0f} / ${a.reno_cost_full:.0f} per m²"),
        ("Reno contingency", f"{a.reno_contingency_pct:.0%}"),
        ("Holding", f"{a.holding_months} months × ${a.holding_cost_per_month:.0f}"),
        ("Sale commission", f"{a.sale_commission_pct:.1%}"),
        ("Exit percentile of finished comps", f"{a.exit_percentile:.0%}"),
        ("Screens", f"≥ {_money(a.min_profit_usd)} profit, ≥ {a.min_roi:.0%} ROI"),
        ("Comp guardrails",
         f"scope no wider than {a.max_benchmark_scope}; "
         f"resale ≤ {a.max_resale_uplift:g}× asking per m²"),
    ]
    assumption_html = "".join(
        f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>"
        for k, v in assumption_items
    )

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JSK Flip Candidates</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>Renovate-and-resell candidates — JSK Real Estate</h1>
<p class="meta">Generated {generated} · {stats.get("active", 0):,} active listings tracked
 · {len(deals):,} passed screening · showing top {min(top_n, len(deals)):,}</p>

<div class="note"><strong>How to read this.</strong> Resale $/m² is the
{a.exit_percentile:.0%}th percentile of <em>finished</em> comparable listings in the same
town (falling back to district, then governorate). Profit is net resale minus the
all-in cost of buying, renovating and holding. These are <em>asking</em> prices on
both sides, not transacted prices — so treat the output as a shortlist to go and
verify, not a valuation. Check the confidence column and the flags before trusting
any single row.</div>

<div class="note"><strong>Two different theses are mixed in this table.</strong>
{_thesis_split(deals)} A <em>renovation_target</em> is cheap because it needs work —
that is the flip thesis, and renovation is what unlocks the value. A
<em>finished</em> listing that still looks cheap against its comps is a different
bet: buying under market. That gap is usually explained by something the data
cannot see (floor, view, exact street, building age, common areas), so treat
those rows with more suspicion. Use <code>analyze --condition
renovation_target,neutral</code> to isolate the renovation thesis.</div>

<h2>Candidates by expected profit</h2>
<div class="scroll"><table>
<thead><tr>
  <th>Property</th><th>Town</th><th class="num">m²</th><th class="num">Asking</th>
  <th class="num">Ask $/m²</th><th class="num">Resale $/m²</th><th class="num">vs mkt</th>
  <th class="num">Reno</th><th class="num">All-in</th><th class="num">Net resale</th>
  <th class="num">Profit</th><th class="num">ROI</th><th>Confidence</th><th>Flags</th>
</tr></thead>
<tbody>{"".join(rows) or '<tr><td colspan="14">No candidates passed screening.</td></tr>'}</tbody>
</table></div>

<h2>Market by town — where the spread is widest</h2>
<div class="note">A wide gap between the 25th and 75th percentile $/m² means tired
and finished stock trade far apart in that town, which is the condition a flip
needs. Towns with many renovation targets <em>and</em> many finished comps are the
best hunting grounds.</div>
<div class="scroll"><table>
<thead><tr><th>Town</th><th>District</th><th class="num">Listings</th>
  <th class="num">P25 $/m²</th><th class="num">Median $/m²</th><th class="num">P75 $/m²</th>
  <th class="num">Spread</th><th class="num">Reno targets</th><th class="num">Finished</th>
</tr></thead>
<tbody>{"".join(market_rows) or '<tr><td colspan="9">Not enough data yet.</td></tr>'}</tbody>
</table></div>

<h2>Assumptions used</h2>
<dl class="assump">{assumption_html}</dl>

<footer>Data scraped from jskre.com public listing pages for personal research.
Cost and fee defaults are placeholders — calibrate them in <code>config.yml</code>
against real contractor quotes and your notary's fee schedule before acting.</footer>
</div></body></html>"""

    path.write_text(doc, encoding="utf-8")
    return path
