#!/usr/bin/env python3
"""Re-extract chunk text from source files to undo anonymisation corruption.

The anonymise stage overwrote chunks.text in place with its (corrupted)
output. The source documents are intact on disk, so the original chunk text
is fully reproducible: extract_text() -> chunk_text() is deterministic.

This script:
  1. Re-reads each affected file from disk
  2. Recomputes its chunks
  3. Restores chunks.text to the pristine extracted text
  4. Resets chunk status to 'pending' so anonymise can re-run cleanly

Usage:
    python3 scripts/repair_chunks.py --limit N          # test on N files
    python3 scripts/repair_chunks.py --source NAME      # restrict to one source
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mfl_brain.config import load_config          # noqa: E402
from mfl_brain.crawler import chunk_text           # noqa: E402
from mfl_brain.extract import extract_text        # noqa: E402
from mfl_brain.manifest import Manifest            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "ingest.config.yaml"))
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset-pii", action="store_true",
                    help="also clear the poisoned pii_map table")
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    cfg = load_config(args.config)
    m = Manifest(cfg.state_db)

    # Files whose chunks need restoring: anything with chunk rows that are
    # not 'pending' (i.e. were processed by the broken anonymise).
    rows = m.conn.execute(
        "SELECT DISTINCT f.path FROM files f JOIN chunks c ON c.file_path=f.path "
        "WHERE c.status != 'pending' AND c.status != 'skipped' ORDER BY f.path"
    ).fetchall()
    paths = [r["path"] for r in rows]
    if args.limit:
        paths = paths[: args.limit]
    print(f"[repair] {len(paths)} files to restore", flush=True)

    t0 = time.time()
    repaired = 0
    failed = 0
    mismatch = 0
    done = 0

    for i, path in enumerate(paths):
        try:
            text = extract_text(Path(path), cfg.ocr)
            new_chunks = chunk_text(text)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[repair] extract failed: {path[-70:]} :: {exc}", flush=True)
            continue

        old = m.conn.execute(
            "SELECT chunk_id, seq FROM chunks WHERE file_path=? ORDER BY seq", (path,)
        ).fetchall()
        if len(old) != len(new_chunks):
            mismatch += 1
            print(f"[repair] chunk count differs for {path[-60:]}: "
                  f"{len(old)} -> {len(new_chunks)} (leaving as-is)", flush=True)
            continue

        if not args.dry_run:
            m.conn.executemany(
                "UPDATE chunks SET text=?, text_sha256=?, status='pending', tier=NULL, "
                "updated_at=? WHERE chunk_id=?",
                [
                    (
                        c,
                        __import__("hashlib").sha256(c.encode()).hexdigest(),
                        time.time(),
                        old[j]["chunk_id"],
                    )
                    for j, c in enumerate(new_chunks)
                ],
            )
            m.conn.commit()
            # reset file status back to extracted so synthesise can pick it up
            m.conn.execute(
                "UPDATE files SET status='extracted', updated_at=? WHERE path=?",
                (time.time(), path),
            )
            m.conn.commit()
        repaired += 1
        done += 1

        if done % 50 == 0:
            el = time.time() - t0
            rate = done / max(el, 0.001)
            remaining = (len(paths) - i - 1) / max(rate, 0.0001)
            print(f"[repair] {done}/{len(paths)} repaired  {rate:.1f} files/s  "
                  f"ETA {remaining/60:.1f} min", flush=True)

    if args.reset_pii and not args.dry_run:
        n = m.conn.execute("SELECT COUNT(*) FROM pii_map").fetchone()[0]
        m.conn.execute("DELETE FROM pii_map")
        m.conn.commit()
        print(f"[repair] cleared {n} poisoned pii_map rows", flush=True)

    print(f"[repair] done: repaired={repaired} failed={failed} mismatch={mismatch} "
          f"in {time.time()-t0:.0f}s", flush=True)
    m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
