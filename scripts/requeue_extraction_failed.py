#!/usr/bin/env python3
"""Requeue `extraction_failed` files for a bounded re-extraction pass (GS-564).

Safety rules:
  * Refuses to run while `mfl_brain.cli anonymise` is the active writer.
  * Takes a consistent SQLite backup before mutating the manifest.
  * Requeues one extension at a time, optionally capped with --limit.

Typical use (after the anonymise stage is stopped or has finished):

    python3 scripts/requeue_extraction_failed.py --ext pdf
    .venv/bin/python -m mfl_brain crawl --config ingest.config.yaml --no-walk --ext pdf
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def anonymise_processes() -> list[str]:
    out = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False
    ).stdout
    return [
        line.strip()
        for line in out.splitlines()
        if "mfl_brain.cli anonymise" in line and "grep" not in line
    ]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()
    return {status: n for status, n in rows}


def backup(db: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "ingest.config.yaml"))
    ap.add_argument("--ext", default="pdf", help="file extension to requeue (default: pdf)")
    ap.add_argument("--limit", type=int, default=0, help="max rows to requeue (0 = all)")
    ap.add_argument("--backup", default=None, help="backup path (default: alongside manifest.db)")
    ap.add_argument("--force", action="store_true", help="run even if anonymise is active")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    db = Path(cfg["state_db"])
    if not db.exists():
        print(f"error: state DB not found: {db}", file=sys.stderr)
        return 1

    running = anonymise_processes()
    if running and not args.force:
        print("refusing to run: anonymise is the active writer:", file=sys.stderr)
        for line in running:
            print(f"  {line}", file=sys.stderr)
        print("stop it (or pass --force) and retry.", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = Path(args.backup) if args.backup else db.with_name(f"{db.name}.bak-{stamp}")
    print(f"[backup] {db} -> {backup_path}")
    backup(db, backup_path)

    ext = args.ext.lower().lstrip(".")
    pattern = f"%.{ext}"
    conn = sqlite3.connect(str(db))
    try:
        before = counts(conn)
        target = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='extraction_failed' AND lower(path) LIKE ?",
            (pattern,),
        ).fetchone()[0]
        print(f"[requeue] {ext}: {target} extraction_failed rows")

        where = "status='extraction_failed' AND lower(path) LIKE ?"
        if args.limit:
            sql = (
                "UPDATE files SET status='discovered', attempts=0 "
                f"WHERE path IN (SELECT path FROM files WHERE {where} LIMIT {int(args.limit)})"
            )
        else:
            sql = f"UPDATE files SET status='discovered', attempts=0 WHERE {where}"
        changed = conn.execute(sql, (pattern,)).rowcount
        conn.commit()
        after = counts(conn)
    finally:
        conn.close()

    print(f"[requeue] changed={changed}")
    print("[before]", {k: before.get(k, 0) for k in
                       ("extracted", "extraction_failed", "discovered", "failed")})
    print("[after ]", {k: after.get(k, 0) for k in
                       ("extracted", "extraction_failed", "discovered", "failed")})
    print(f"[next  ] .venv/bin/python -m mfl_brain crawl --config {cfg_path} "
          f"--no-walk --ext {ext}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
