"""Photo-based condition assessment via the Claude API.

Why this exists: interior condition is the pivot of the whole flip analysis,
and the honest measurement earlier in this project showed the *text* condition
classifier explains almost nothing — descriptions are sales copy, and only
0.5% of listings admit to needing renovation. The photos are the closest thing
to ground truth this dataset has.

Design decisions, and the reasoning:

* **Batch API, not live calls.** ~2,500 independent classifications with no
  latency requirement is exactly what the Batch API is for, at half price.
  Batches persist server-side for 29 days keyed by batch id, and we record
  ids in the database, so submitting and importing can happen in different
  sessions (or different machines).
* **Image URLs, not downloads.** JSK's S3 photos are publicly accessible and
  the API accepts URL image sources, so the model fetches them directly --
  this pipeline never downloads, resizes, or stores an image.
* **Structured outputs.** The response is schema-enforced JSON, so importing
  is `json.loads` and an upsert -- no parsing heuristics.
* **Results are append-only facts.** An assessment records what a model said
  about a set of photos on a date. Nothing downstream is changed by this
  module; blending photo verdicts into the margin model is a separate,
  deliberate step.

Cost, measured on real photos (~1440x1080 -> ~2,100 tokens each, 4 per
listing): full backfill ~23M input tokens. At batch prices that is roughly
$65 on claude-opus-5, $26 on claude-sonnet-5, $13 on claude-haiku-4-5.
`estimate` prints the live number for whatever is pending.
"""

from __future__ import annotations

import json
import logging

from .db import Database, utcnow

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
MAX_PHOTOS_PER_LISTING = 4
MAX_TOKENS = 3000

# For the dry-run estimate only; billing truth comes from the API.
EST_TOKENS_PER_IMAGE = 2100
EST_PROMPT_TOKENS = 800
EST_OUTPUT_TOKENS = 700          # schema output plus modest adaptive thinking
BATCH_DISCOUNT = 0.5
PRICES_PER_MTOK = {              # (input $, output $)
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

PHOTO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS photo_assessments (
    ref                  TEXT PRIMARY KEY REFERENCES properties(ref),
    condition            TEXT,
    kitchen_era          TEXT,
    bathroom_era         TEXT,
    reno_scope           TEXT,
    confidence           TEXT,
    interior_photo_count INTEGER,
    evidence             TEXT,      -- JSON array of observations
    image_count          INTEGER,   -- how many photos were sent
    model                TEXT,
    batch_id             TEXT,
    scored_at            TEXT
);

CREATE TABLE IF NOT EXISTS photo_batches (
    id           TEXT PRIMARY KEY,  -- Anthropic batch id
    model        TEXT,
    n_requests   INTEGER,
    submitted_at TEXT,
    status       TEXT DEFAULT 'submitted',
    imported     INTEGER DEFAULT 0,
    errored      INTEGER DEFAULT 0
);
"""

SYSTEM_PROMPT = """\
You are a senior residential appraiser assessing apartment photos for a \
renovate-and-resell analysis in the Lebanese market. Your one job: judge the \
interior condition and renovation era from what is actually visible.

Market context. Much of Lebanon's apartment stock dates from the 1960s-1990s. \
Telltales of ORIGINAL-ERA interiors: dark anodized aluminum sliding windows, \
terrazzo or small-format ceramic tile floors, wooden roll-down shutters, \
laminate kitchen cabinetry with arched or routed details, colored bathroom \
suites (beige/pink/green ceramic), older wall-mounted AC units or sleeve \
vents, textured or yellowed plaster, ornate older light fixtures. Telltales \
of RECENT RENOVATION: gypsum false ceilings with LED spotlights, large-format \
porcelain tile or wood-look flooring, double-glazed PVC or thermally-broken \
window frames, handleless or shaker kitchens with stone counters, glass-panel \
walk-in showers, contemporary vanities and matte fixtures, fresh flat paint.

Definitions for `condition`:
- shell: unfinished concrete/blockwork, no finishes installed
- needs_full_renovation: finishes present but derelict, damaged, or stripped
- dated: intact and livable but essentially original 1970s-1990s finishes
- average: mixed or partially updated; livable and unremarkable
- renovated: recently modernized throughout
- luxury: high-end recent finishes, detailing, and materials

Rules:
- Judge ONLY interior photos. Count them in interior_photo_count. Exterior,
  building, and view shots carry no interior information.
- If there are no interior photos, set every era field to not_shown,
  confidence to low, and condition to your best guess from what little is
  visible.
- Judge the kitchen and bathrooms separately; they carry most of a flip's
  renovation cost. If a room type is not pictured, say not_shown -- and
  remember listing agents show the best rooms, so an unpictured kitchen in a
  9-photo listing is itself weak evidence of a dated kitchen. Reflect that in
  confidence, not in the era fields.
- reno_scope is the work needed to sell at renovated-stock prices: none,
  light (paint/fixtures/floors), medium (+ kitchen and bathrooms), full (gut).
- evidence: short concrete observations tied to what you saw, one per string.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "condition": {
            "type": "string",
            "enum": [
                "shell", "needs_full_renovation", "dated",
                "average", "renovated", "luxury",
            ],
        },
        "kitchen_era": {
            "type": "string",
            "enum": ["original", "partially_updated", "modern", "not_shown"],
        },
        "bathroom_era": {
            "type": "string",
            "enum": ["original", "partially_updated", "modern", "not_shown"],
        },
        "reno_scope": {
            "type": "string",
            "enum": ["none", "light", "medium", "full"],
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "interior_photo_count": {"type": "integer"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "condition", "kitchen_era", "bathroom_era", "reno_scope",
        "confidence", "interior_photo_count", "evidence",
    ],
    "additionalProperties": False,
}


def ensure_schema(db: Database) -> None:
    db.conn.executescript(PHOTO_SCHEMA_SQL)
    db.conn.commit()


# ------------------------------------------------------------------ selection


def pending_listings(db: Database, limit: int | None = None) -> list[dict]:
    """Active listings with stored photo URLs and no assessment yet."""
    ensure_schema(db)
    sql = """
        SELECT p.ref, p.title, p.image_urls
        FROM properties p
        LEFT JOIN photo_assessments a ON a.ref = p.ref
        WHERE p.is_active = 1
          AND p.image_urls IS NOT NULL AND p.image_urls != '[]'
          AND a.ref IS NULL
        ORDER BY p.ref
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for row in db.conn.execute(sql).fetchall():
        try:
            urls = json.loads(row["image_urls"])
        except (json.JSONDecodeError, TypeError):
            continue
        if urls:
            out.append({"ref": row["ref"], "title": row["title"], "urls": urls})
    return out


def select_photos(urls: list[str], max_n: int = MAX_PHOTOS_PER_LISTING) -> list[str]:
    """Up to max_n distinct photo URLs, in listing order.

    Listing order matters: agents lead with their best shots, which are the
    most informative about the finish level being marketed.
    """
    seen: dict[str, None] = {}
    for url in urls:
        if url and url not in seen:
            seen[url] = None
        if len(seen) >= max_n:
            break
    return list(seen)


# ------------------------------------------------------------------- requests


def build_params(ref: str, title: str | None, urls: list[str],
                 model: str = DEFAULT_MODEL) -> dict:
    """Messages API params for one listing. Plain dicts so tests need no SDK."""
    photos = select_photos(urls)
    content: list[dict] = [
        {"type": "image", "source": {"type": "url", "url": url}} for url in photos
    ]
    content.append({
        "type": "text",
        "text": (
            f"Listing {ref}"
            + (f": {title}" if title else "")
            + f". The {len(photos)} photos above are from this listing. "
            "Assess the interior condition."
        ),
    })
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        # Identical system prompt across every request in the batch: cache it.
        "system": [{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{"role": "user", "content": content}],
        "output_config": {
            "effort": "low",   # schema-guided classification; low still thinks
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
    }


# ------------------------------------------------------------------- estimate


def estimate(db: Database, limit: int | None = None) -> dict:
    pending = pending_listings(db, limit)
    n_images = sum(len(select_photos(p["urls"])) for p in pending)
    input_tokens = n_images * EST_TOKENS_PER_IMAGE + len(pending) * EST_PROMPT_TOKENS
    output_tokens = len(pending) * EST_OUTPUT_TOKENS
    costs = {}
    for model, (in_price, out_price) in PRICES_PER_MTOK.items():
        full = (input_tokens * in_price + output_tokens * out_price) / 1e6
        costs[model] = round(full * BATCH_DISCOUNT, 2)
    return {
        "pending_listings": len(pending),
        "images": n_images,
        "est_input_tokens": input_tokens,
        "est_output_tokens": output_tokens,
        "est_batch_cost_usd": costs,
    }


# --------------------------------------------------------------- submit/import


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The 'anthropic' package is required: pip install anthropic"
        ) from exc
    return anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / auth profile


def submit(db: Database, model: str = DEFAULT_MODEL,
           limit: int | None = None) -> str | None:
    """Submit one batch for everything pending. Returns the batch id."""
    pending = pending_listings(db, limit)
    if not pending:
        log.info("Nothing pending: no active listings with photos lack an assessment.")
        return None

    client = _client()
    requests = [
        {
            "custom_id": p["ref"],
            "params": build_params(p["ref"], p["title"], p["urls"], model),
        }
        for p in pending
    ]
    batch = client.messages.batches.create(requests=requests)
    db.conn.execute(
        "INSERT INTO photo_batches (id, model, n_requests, submitted_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (batch.id, model, len(requests), utcnow(), batch.processing_status),
    )
    db.conn.commit()
    log.info("Submitted batch %s with %s requests on %s", batch.id, len(requests), model)
    return batch.id


def import_result(db: Database, ref: str, data: dict, model: str,
                  batch_id: str | None, image_count: int | None = None) -> None:
    """Upsert one parsed assessment. Idempotent per ref."""
    ensure_schema(db)
    db.conn.execute(
        """
        INSERT INTO photo_assessments
            (ref, condition, kitchen_era, bathroom_era, reno_scope, confidence,
             interior_photo_count, evidence, image_count, model, batch_id, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ref) DO UPDATE SET
            condition=excluded.condition, kitchen_era=excluded.kitchen_era,
            bathroom_era=excluded.bathroom_era, reno_scope=excluded.reno_scope,
            confidence=excluded.confidence,
            interior_photo_count=excluded.interior_photo_count,
            evidence=excluded.evidence, image_count=excluded.image_count,
            model=excluded.model, batch_id=excluded.batch_id,
            scored_at=excluded.scored_at
        """,
        (
            ref, data.get("condition"), data.get("kitchen_era"),
            data.get("bathroom_era"), data.get("reno_scope"),
            data.get("confidence"), data.get("interior_photo_count"),
            json.dumps(data.get("evidence") or []), image_count, model,
            batch_id, utcnow(),
        ),
    )
    db.conn.commit()


def refresh_and_import(db: Database) -> dict:
    """Poll every recorded batch; import results for the ones that ended."""
    ensure_schema(db)
    client = _client()
    summary = {"imported": 0, "errored": 0, "still_processing": 0}

    rows = db.conn.execute(
        "SELECT id, model FROM photo_batches WHERE imported = 0"
    ).fetchall()
    for row in rows:
        batch = client.messages.batches.retrieve(row["id"])
        db.conn.execute(
            "UPDATE photo_batches SET status = ? WHERE id = ?",
            (batch.processing_status, row["id"]),
        )
        db.conn.commit()
        if batch.processing_status != "ended":
            summary["still_processing"] += 1
            log.info("Batch %s: %s", row["id"], batch.processing_status)
            continue

        imported = errored = 0
        for result in client.messages.batches.results(row["id"]):
            if result.result.type == "succeeded":
                message = result.result.message
                text = next(
                    (b.text for b in message.content if b.type == "text"), None
                )
                if text is None:
                    errored += 1
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    errored += 1
                    log.warning("Unparseable result for %s", result.custom_id)
                    continue
                import_result(db, result.custom_id, data, row["model"], row["id"])
                imported += 1
            else:
                errored += 1
                log.warning("Batch result %s: %s", result.custom_id, result.result.type)

        db.conn.execute(
            "UPDATE photo_batches SET imported = 1, errored = ? WHERE id = ?",
            (errored, row["id"]),
        )
        db.conn.commit()
        summary["imported"] += imported
        summary["errored"] += errored

    return summary


def status(db: Database) -> list[dict]:
    ensure_schema(db)
    assessed = db.conn.execute(
        "SELECT COUNT(*) FROM photo_assessments"
    ).fetchone()[0]
    batches = [
        dict(r)
        for r in db.conn.execute(
            "SELECT * FROM photo_batches ORDER BY submitted_at DESC"
        ).fetchall()
    ]
    return [{"assessed": assessed}] + batches
