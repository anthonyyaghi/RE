"""Portable snapshots of the collected data.

The SQLite file lives in gitignored `data/` and, in an ephemeral development
environment, dies with the container. These exports are the survival path:
compressed JSONL that is small enough to commit, complete enough to rebuild
analysis from, and diffable enough that git history doubles as a crude
time-series of the market.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from .db import Database


def _dump_jsonl_gz(rows, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the gzip output byte-stable for identical content, so git
    # doesn't see a change when nothing changed.
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as handle:
        for row in rows:
            handle.write(
                (json.dumps(dict(row), ensure_ascii=False) + "\n").encode("utf-8")
            )
    return path


def export_all(db: Database, outdir: str | Path = "exports") -> list[Path]:
    outdir = Path(outdir)
    written = []

    written.append(_dump_jsonl_gz(
        db.conn.execute("SELECT * FROM properties ORDER BY ref"),
        outdir / "listings.jsonl.gz",
    ))
    written.append(_dump_jsonl_gz(
        db.conn.execute("SELECT * FROM price_history ORDER BY ref, id"),
        outdir / "price_history.jsonl.gz",
    ))

    tables = {
        r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "photo_assessments" in tables:
        written.append(_dump_jsonl_gz(
            db.conn.execute("SELECT * FROM photo_assessments ORDER BY ref"),
            outdir / "photo_assessments.jsonl.gz",
        ))
    return written
