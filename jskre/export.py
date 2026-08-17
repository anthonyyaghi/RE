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


def _read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def restore_all(db: Database, outdir: str | Path = "exports") -> dict[str, int]:
    """Rebuild database tables from exports/ snapshots.

    This is what makes a fresh clone usable without an 80-minute crawl: the
    exports are committed to git, the SQLite file is not. Restoring into a
    database that already has listings is refused -- re-running a crawl on top
    of restored data is fine, but silently merging two datasets is not.
    """
    outdir = Path(outdir)
    existing = db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    if existing:
        raise SystemExit(
            f"Refusing to restore into a database that already has {existing:,} "
            "listings. Delete the db file (or point --db elsewhere) first."
        )

    from .photos import ensure_schema as ensure_photo_schema

    ensure_photo_schema(db)
    counts: dict[str, int] = {}
    for table, filename in (
        ("properties", "listings.jsonl.gz"),
        ("price_history", "price_history.jsonl.gz"),
        ("photo_assessments", "photo_assessments.jsonl.gz"),
    ):
        path = outdir / filename
        if not path.exists():
            continue
        table_cols = {
            r["name"]
            for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        n = 0
        for row in _read_jsonl_gz(path):
            cols = [c for c in row if c in table_cols]
            db.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols],
            )
            n += 1
        counts[table] = n
    db.conn.commit()
    return counts


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
