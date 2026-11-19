#!/usr/bin/env python3
"""Parallel repair of corrupted chunk text.

The broken anonymise stage overwrote chunks.text with corrupted output.
Source documents are intact on disk and extract_text() -> chunk_text() is
deterministic, so the pristine text is fully reproducible.

Runs N worker processes over the affected files and writes results back to
the manifest DB in batched transactions.

Usage:
    python3 scripts/repair_chunks_parallel.py --workers 6 [--limit N]
                                               [--source NAME] [--reset-pii]
"""

from __future__ import annotations

import argparse
import hashlib
import multiprocessing as mp
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_CFG = None


def _init(config_path: str):
    global _CFG
    from mfl_brain.config import load_config
    _CFG = load_config(config_path)


def _work(item):
    """Process one file: re-extract, re-chunk, return (path, [(seq, text)], error)."""
    path = item
    try:
        from mfl_brain.crawler import chunk_text
        from mfl_brain.extract import extract_text
        text = extract_text(Path(path), _CFG.ocr)
        chunks = chunk_text(text)
        return path, chunks, None
    except Exception as exc:  # noqa: BLE001
        return path, None, f"{type(exc).__name__}: {exc}"[:200]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "ingest.config.yaml"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--reset-pii", action="store_true")
    ap.add_argument("--batch", type=int, default=100)
    args = ap.parse_args()

    from mfl_brain.config import load_config
    from mfl_brain.manifest import Manifest

    cfg = load_config(args.config)
    m = Manifest(cfg.state_db)

    rows = m.conn.execute(
        "SELECT DISTINCT f.path FROM files f JOIN chunks c ON c.file_path=f.path "
        "WHERE c.status NOT IN ('pending','skipped') ORDER BY f.path"
    ).fetchall()
    paths = [r["path"] for r in rows]
    if args.limit:
        paths = paths[: args.limit]
    print(f"[repair] {len(paths)} files, {args.workers} workers", flush=True)

    t0 = time.time()
    repaired = failed = mismatch = 0
    pending_write = []
    now = time.time()

    ctx = mp.get_context("fork")
    with ctx.Pool(args.workers, initializer=_init, initargs=(args.config,)) as pool:
        for i, (path, chunks, err) in enumerate(
            pool.imap_unordered(_work, paths, chunksize=4), start=1
        ):
            if err:
                failed += 1
                print(f"[repair] FAIL {path[-65:]}: {err}", flush=True)
                continue
            old = m.conn.execute(
                "SELECT chunk_id, seq FROM chunks WHERE file_path=? ORDER BY seq", (path,)
            ).fetchall()
            if len(old) != len(chunks):
                mismatch += 1
                print(f"[repair] MISMATCH {path[-55:]}: {len(old)} vs {len(chunks)}", flush=True)
                continue
            for j, c in enumerate(chunks):
                pending_write.append(
                    (c, hashlib.sha256(c.encode()).hexdigest(), now, old[j]["chunk_id"])
                )
            pending_write.append(("__FILE__", path, now))

            if len(pending_write) >= args.batch:
                _flush(m, pending_write)
                pending_write = []

            if i % 200 == 0:
                el = time.time() - t0
                rate = i / max(el, 0.001)
                eta = (len(paths) - i) / max(rate, 0.0001) / 60
                print(f"[repair] {i}/{len(paths)}  {rate:.1f} files/s  ETA {eta:.1f} min",
                      flush=True)

    if pending_write:
        _flush(m, pending_write)

    if args.reset_pii:
        n = m.conn.execute("SELECT COUNT(*) FROM pii_map").fetchone()[0]
        m.conn.execute("DELETE FROM pii_map")
        m.conn.commit()
        print(f"[repair] cleared {n} poisoned pii_map rows", flush=True)

    print(f"[repair] done: repaired={repaired} failed={failed} mismatch={mismatch} "
          f"in {time.time()-t0:.0f}s", flush=True)
    m.close()
    return 0


def _flush(m, pending):
    """Write buffered chunk updates + file status resets in one transaction."""
    updates = [r for r in pending if r[0] != "__FILE__"]
    files = [(r[2], r[1]) for r in pending if r[0] == "__FILE__"]
    if updates:
        m.conn.executemany(
            "UPDATE chunks SET text=?, text_sha256=?, status='pending', tier=NULL, "
            "updated_at=? WHERE chunk_id=?", updates)
    if files:
        m.conn.executemany(
            "UPDATE files SET status='extracted', updated_at=? WHERE path=?",
            files)
    m.conn.commit()


if __name__ == "__main__":
    sys.exit(main())
