#!/usr/bin/env python3
"""Purge junk rows from the pii_map table (GS-571 follow-up).

The anonymiser's LLM tiers occasionally persisted prose, placeholders or
whitespace blobs as "PII".  Those rows make the known-PII pre-pass redact
half-sentences on later runs.  This script removes them safely:

  1. (--apply only) take a consistent SQLite backup of the state DB first,
     using the online backup API so WAL contents are included;
  2. report counts by junk rule (dry-run by default — nothing is deleted);
  3. only when --apply is passed, delete the junk rows in one transaction.

Validation rules are imported from mfl_brain.manifest so this script and
remember_pii_batch() can never drift apart.

Usage:
    # dry-run (default) — report only, no writes, no backup
    python3 scripts/purge_pii_junk.py --config ingest.config.yaml

    # actually delete (backup written next to the DB first)
    python3 scripts/purge_pii_junk.py --config ingest.config.yaml --apply

    # point straight at a DB file
    python3 scripts/purge_pii_junk.py --db /path/to/manifest.db

Exit codes: 0 ok, 2 bad arguments / missing DB.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mfl_brain.manifest import PII_JUNK_RULES, pii_junk_rules  # noqa: E402


def _resolve_db(args: argparse.Namespace) -> Path:
    if args.db:
        db = Path(args.db).expanduser()
    else:
        import yaml

        cfg_path = Path(args.config).expanduser()
        if not cfg_path.exists():
            raise SystemExit(f"error: config not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        state_db = Path(cfg.get("state_db", ".ingest-state/manifest.db"))
        db = state_db if state_db.is_absolute() else cfg_path.parent / state_db
    db = db.resolve()
    if not db.exists():
        raise SystemExit(f"error: database not found: {db}")
    return db


def _backup(db: Path, backup_dir: Path) -> Path:
    """Consistent online backup (includes WAL), returns the backup path."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{db.stem}.pii-junk-backup.{time.strftime('%Y%m%d-%H%M%S')}{db.suffix}"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # type: ignore[attr-defined]
    finally:
        dst.close()
        src.close()
    return dest


def _scan(db: Path) -> tuple[list[tuple[str, str, str]], dict[str, int], int]:
    """Return (junk rows, per-rule counts, total rows)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT real_value, placeholder, kind FROM pii_map"
        ).fetchall()
    finally:
        conn.close()

    counts = {name: 0 for name in PII_JUNK_RULES}
    junk: list[tuple[str, str, str]] = []
    for row in rows:
        real, kind = row["real_value"], row["kind"] or "other"
        matched = pii_junk_rules(real, kind)
        if not matched:
            continue
        for name in matched:
            counts[name] += 1
        junk.append((real, row["placeholder"], kind))
    return junk, counts, len(rows)


def _report(junk: list[tuple[str, str, str]], counts: dict[str, int], total: int) -> None:
    print(f"pii_map rows: {total}")
    print(f"junk rows (union): {len(junk)}")
    print("counts by rule (a row may match more than one):")
    for name, n in counts.items():
        print(f"  {name:>16}: {n}")
    if junk:
        print("sample junk values:")
        for real, _ph, kind in junk[:10]:
            preview = repr(real)[:80]
            print(f"  [{kind}] {preview}")


def _delete(db: Path, junk: list[tuple[str, str, str]]) -> int:
    if not junk:
        return 0
    conn = sqlite3.connect(str(db), timeout=60)
    try:
        before = conn.total_changes
        conn.executemany(
            "DELETE FROM pii_map WHERE real_value=? AND placeholder=?",
            [(real, ph) for real, ph, _kind in junk],
        )
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="ingest.config.yaml",
                   help="config file used to locate state_db (default: ingest.config.yaml)")
    p.add_argument("--db", help="explicit path to the manifest SQLite DB (overrides --config)")
    p.add_argument("--apply", action="store_true",
                   help="actually delete junk rows (default: dry-run, no writes)")
    p.add_argument("--backup-dir", help="where to write the backup (default: next to the DB)")
    args = p.parse_args(argv)

    db = _resolve_db(args)
    junk, counts, total = _scan(db)
    _report(junk, counts, total)

    if not args.apply:
        print("\nDRY RUN — no rows deleted. Re-run with --apply to purge.")
        return 0

    backup_dir = Path(args.backup_dir).expanduser() if args.backup_dir else db.parent
    backup = _backup(db, backup_dir)
    print(f"\nbackup written: {backup}")
    deleted = _delete(db, junk)
    print(f"deleted {deleted} junk rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
