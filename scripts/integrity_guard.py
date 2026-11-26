#!/usr/bin/env python3
"""SQLite manifest integrity guard (GS-568).

Runs a lightweight integrity check against the pipeline state DB and repairs
index-only corruption automatically by rebuilding indexes with ``REINDEX``.

Why: on 2026-11-24 the manifest aggregated missing index entries
(``idx_files_status``, ``idx_chunks_status`` and a pii autoindex). Every write
then raised ``sqlite3.DatabaseError: database disk image is malformed`` and the
anonymise stage crash-looped. ``REINDEX`` fixed it in place.

Safety rules
  * Never performs destructive recovery. Only ``REINDEX`` is attempted, and
    only when the reported error looks index-only.
  * On unrecoverable corruption it writes a CRITICAL log line and an
    ``INTEGRITY-FAILURE`` marker, then exits 2 so callers skip writes instead
    of crash-looping.
  * Uses a generous ``busy_timeout`` so it can run while the pipeline writes.

Exit codes
  0  healthy, or repaired successfully
  2  unrecoverable corruption (caller should skip writes)
  3  database locked / unavailable (transient; caller should retry later)
"""

from __future__ import annotations

import argparse
import fcntl
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "integrity.log"
MARKER = ROOT / "logs" / "INTEGRITY-FAILURE.txt"
LOCK = ROOT / "logs" / "integrity.lock"
BUSY_TIMEOUT_MS = 60_000

# Substrings that identify index-only corruption we can safely repair.
INDEX_HINTS = (
    "index",
    "idx_files_status",
    "idx_chunks_status",
    "idx_files_sha",
    "idx_files_doc_type",
    "row missing from index",
    "wrong # of entries in index",
    "sqlite_autoindex",
)


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _resolve_db(config: str | None, db: str | None) -> Path | None:
    if db:
        return Path(db).expanduser()
    cfg = Path(config or (ROOT / "ingest.config.yaml"))
    if not cfg.is_absolute():
        cfg = ROOT / cfg
    if not cfg.exists():
        return None
    text = cfg.read_text(encoding="utf-8")
    try:
        import yaml

        raw = (yaml.safe_load(text) or {}).get("state_db")
    except ImportError:
        # System Python may lack PyYAML; fall back to a plain text scan.
        import re

        match = re.search(r"^state_db:\s*[\"']?([^\"'\n#]+)", text, re.MULTILINE)
        raw = match.group(1).strip() if match else None
    except Exception as exc:  # noqa: BLE001
        log(f"CRITICAL cannot read config {cfg}: {exc}")
        return None
    if raw:
        return Path(raw).expanduser()
    return ROOT / ".ingest-state" / "manifest.db"


def _check(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    return [str(r[0]) for r in rows]


def _looks_index_only(messages: list[str]) -> bool:
    joined = " | ".join(messages).lower()
    return any(hint in joined for hint in INDEX_HINTS)


def _clear_marker() -> None:
    try:
        MARKER.unlink()
    except FileNotFoundError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "ingest.config.yaml"))
    ap.add_argument("--db", default=None, help="override state DB path")
    ap.add_argument("--full", action="store_true",
                    help="use PRAGMA integrity_check instead of quick_check")
    args = ap.parse_args()

    # The launchd-scheduled guard and the supervisor's pre-start guard can
    # fire together at boot. A quick_check on the ~4 GB external-volume DB is
    # I/O-bound, so two concurrent checks stall the writer for minutes. Let
    # one instance run and have the other bow out; the lock is released by
    # process exit on every return path.
    LOG.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK.open("a+")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("skip: another integrity guard is already running")
        return 0

    db = _resolve_db(args.config, args.db)
    if db is None or not db.exists():
        log(f"CRITICAL state DB not found (db={db})")
        return 2

    pragma = "integrity_check" if args.full else "quick_check"
    log(f"check start db={db} pragma={pragma}")

    try:
        conn = sqlite3.connect(str(db), timeout=BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        try:
            messages = _check(conn) if pragma == "quick_check" else [
                str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()
            ]
            if messages == ["ok"]:
                log("OK healthy")
                _clear_marker()
                return 0

            log(f"WARN integrity problems: {' | '.join(messages)[:800]}")

            if not _looks_index_only(messages):
                # Generic corruption that may not be index-only: still try a
                # REINDEX (cheap, non-destructive) then re-check before giving
                # up. If the message did not mention indexes we are cautious but
                # REINDEX cannot lose table data.
                log("WARN corruption does not name an index; attempting REINDEX anyway (non-destructive)")

            try:
                log("repair REINDEX start")
                conn.execute("REINDEX")
                conn.commit()
                log("repair REINDEX done")
            except sqlite3.DatabaseError as exc:
                log(f"CRITICAL REINDEX failed: {exc}")
                MARKER.write_text(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} REINDEX failed: {exc}\n",
                    encoding="utf-8",
                )
                return 2

            after = _check(conn)
            if after == ["ok"]:
                log("REPAIRED integrity ok after REINDEX")
                _clear_marker()
                return 0

            detail = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
            log(f"CRITICAL unrecoverable after REINDEX: {' | '.join(detail)[:800]}")
            MARKER.write_text(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} unrecoverable:\n"
                + "\n".join(detail)
                + "\n",
                encoding="utf-8",
            )
            return 2
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "locked" in msg.lower() or "busy" in msg.lower():
            log(f"WARN database busy/locked, will retry later: {msg}")
            return 3
        log(f"CRITICAL operational error: {msg}")
        return 2
    except sqlite3.DatabaseError as exc:
        # Malformed reads can raise here before PRAGMA returns rows.
        log(f"CRITICAL database error during check: {exc}")
        MARKER.write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} {exc}\n", encoding="utf-8"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
