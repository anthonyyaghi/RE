# JSK renovation-flip finder

Collects listings from [jskre.com](https://www.jskre.com) into a local database,
tracks how they change over time, and ranks them by the margin available if you
buy, renovate and resell.

The point is not to have a scraper. The point is to answer one question
repeatedly and consistently: **which listings are priced far enough below what
finished stock in the same area asks that a renovation still leaves a profit?**

---

## Quick start

```bash
pip install -r requirements.txt

python -m jskre scrape                 # crawl listings into data/jskre.db
python -m jskre details --limit 300    # optional: full descriptions
python -m jskre report                 # writes out/report.html + CSVs
open out/report.html
```

A full apartment crawl is ~254 pages at ~2s each, so budget about 9 minutes.
Re-running `scrape` updates existing rows rather than duplicating them.

---

## How the margin model works

Every listing is placed on one axis before anything else happens: **does it need
work, or is it already finished?** A flip only has margin when you buy the first
kind and sell at the price of the second, so that classification drives
everything downstream.

```
                     ┌─ description text ─┐
                     │                    │
              renovation_target      finished
                     │                    │
                     ▼                    ▼
              buy candidate      defines the resale benchmark
```

1. **Condition** (`condition.py`) scores the listing text against weighted
   keyword signals — `core & shell`, `needs renovation`, `as-is` on one side;
   `turnkey`, `fully renovated`, `decorated` on the other. Scores run −1 to +1.

2. **Comps** (`analyze.py`) build a resale benchmark per micro-market from
   **finished listings only**, at a configurable percentile (default 60th).
   Unfinished stock is deliberately excluded — including it would drag the exit
   price down toward the very prices you are trying to beat. Scope narrows to
   the town where possible, falling back to district, then governorate.

3. **Margin** nets the whole round trip:

   ```
   all-in  = asking × (1 − negotiation) + purchase fees
             + area × reno rate × (1 + contingency)
             + holding months × monthly cost

   exit    = benchmark $/m² × area × (1 − sale commission)

   profit  = exit − all-in          ROI = profit / all-in
   ```

4. **Screening** drops anything below your profit and ROI minimums, *and*
   anything whose comps aren't credible (see below).

---

## The guardrails matter more than the model

This kind of model fails in a flattering direction. A flat in a cheap town
compared against finished stock from an expensive one shows a spectacular ROI,
and those rows would otherwise sort straight to the top of your shortlist.

Two gates reject rather than flatter:

| Gate | Default | Why |
|---|---|---|
| `max_benchmark_scope` | `district` | A benchmark borrowed from governorate or global level isn't a comparable. Such rows are computed, flagged, and excluded from the ranking. |
| `max_resale_uplift` | `2.5` | If modelled resale/m² is more than 2.5× asking/m², the comp set is almost certainly wrong. A genuine 150% gross uplift is rarer than a bad comparable. |

Both are visible in the report's assumptions block, and `analyze --no-screens`
shows what they excluded. **Widening them is how you get impressive numbers and
lose money**, so widen them only once you have enough local data to know better.

Comp density is the real constraint. On a 200-listing sample, only 2 of 84 towns
had five or more finished comps — everything else fell back to borrowed
benchmarks. Crawl the full inventory before trusting any ranking.

---

## Commands

| Command | What it does |
|---|---|
| `scrape` | Crawl index pages. `--category`, `--max-pages`, `--start-page`. |
| `details` | Fetch detail pages for untruncated descriptions and photo URLs. |
| `analyze` | Rank candidates to the terminal. `--top`, `--town`, `--no-screens`, `--csv`. |
| `report` | Write `flip_candidates.csv`, `market_by_town.csv`, `report.html`. |
| `changes` | Price cuts and delistings in the last N days. |
| `status` | Row counts and recent crawl history. |

### Tracking change over time

The schema treats a listing as mutable, because the transitions are the signal:

- **`price_history`** — every observed price change. A seller who has already
  cut once is a seller who will cut again; `changes` ranks by size of cut.
- **`first_price_usd`** — the original asking price survives later edits.
- **`is_active` / `delisted_at`** — a listing that disappears has usually sold.
  Delistings are how you eventually calibrate the model against real exits
  rather than asking prices.
- **`crawl_runs`** — provenance for every crawl.

Delisting detection only runs after a complete sweep from page 1; a partial
crawl would wrongly retire everything it didn't reach, and says so instead.

Run `scrape` on a schedule (weekly is plenty) and the history builds itself:

```cron
0 6 * * 1  cd /path/to/RE && python -m jskre scrape && python -m jskre report
```

---

## Reading the output

`out/flip_candidates.csv` is the working artefact — every intermediate number is
a column, so you can re-sort and re-model in a spreadsheet without rerunning
anything. The HTML report is the readable snapshot.

Columns worth understanding:

- **`discount_to_market_pct`** — how far below the area median $/m² it's asking.
- **`resale_uplift_ratio`** — resale/m² ÷ asking/m². Above ~2 deserves suspicion.
- **`confidence`** — `high` needs many comps at town level; forced to `low`
  whenever a guardrail trips.
- **`flags`** — always read this column before acting on a row.
- **`market_by_town.csv`** — sorted by the P25–P75 spread. A wide spread means
  tired and finished stock trade far apart there, which is the precondition for
  a flip. Towns with many renovation targets *and* many finished comps are the
  best hunting grounds.

### What this cannot tell you

Both sides of every calculation are **asking** prices, not transacted prices, so
the model measures asking-price arbitrage, not realised profit. It also cannot
see the things that decide a real flip: structural condition, the state of the
building's common areas, title and permit problems, whether the described
"renovation" means paint or rewiring, or how long a finished unit actually takes
to sell. `days_tracked` starts when *you* first saw the listing, so it's a lower
bound on time on market and only means anything after a few months of crawling.

Treat the output as a shortlist to go and inspect, and calibrate the cost
assumptions in `config.yml` against real contractor quotes and your notary's
current fee schedule before acting on any row.

---

## Configuration

All assumptions live in `config.yml` — nothing is hardcoded, because the honest
answer to "what does renovation cost per m² in Lebanon" is "it depends, go and
price two jobs". The shipped defaults are plausible placeholders, **not market
research**. The ones that move the answer most:

```yaml
assumptions:
  negotiation_discount: 0.07    # how far under asking you expect to buy
  purchase_fees_pct: 0.06       # registration + notary — verify current rates
  reno_cost_medium: 320         # $/m² — replace with a real quote
  exit_percentile: 0.60         # 0.5 conservative, 0.75 optimistic
  max_price_usd: null           # your budget ceiling
```

Set `analysis.property_types` to control what's compared. Note the site labels
most residential stock `Apartment` even when the title says chalet.

---

## Scraping conduct

The site is someone's business, not a dataset:

- **robots.txt is fetched and enforced** on every request (`http.py`). If it
  can't be read, the crawler assumes deny rather than proceeding.
  jskre.com disallows `/property/` and `/property-id/` — legacy routes — but not
  `/properties/`, where live listings are. Query parameters `?type=`, `?sort=`
  and `?lang=` are disallowed and never used; `?page=` is permitted.
- **~2s between requests** with jitter, single-threaded, identifiable
  user-agent. Retries only on transient errors, with exponential backoff.
- **Index-first**: listing cards already carry price, area, beds, baths, town
  and description, so the whole inventory costs ~254 requests instead of 2,533.
  Only run `details` for the backlog you actually need.
- **Personal research use.** The site's `Content-Signal` header permits
  reference use and forbids AI training; this tool does neither training nor
  redistribution. Don't republish the scraped data.

If JSK offers a feed or API, prefer it. Ask.

---

## Layout

```
jskre/
  http.py        robots.txt enforcement, rate limiting, retries
  parse.py       index-card + detail-page parsers
  db.py          SQLite schema, upserts, change history
  scrape.py      crawl orchestration (index pass, detail pass)
  condition.py   renovation-state classifier
  analyze.py     comps engine, margin model, guardrails
  report.py      CSV + HTML output
  cli.py         command line interface
tests/
  fixtures/      trimmed real pages — refresh these when the site changes
```

The parsers anchor on structural signals (`alt="Bedrooms"`, `REF: L#####`,
`href="/properties/..."`) rather than Tailwind class names, which change with
the theme. The fixture tests are what will tell you when a redesign breaks
parsing — refresh the fixtures rather than loosening the assertions.

```bash
python -m pytest tests/ -q
```
