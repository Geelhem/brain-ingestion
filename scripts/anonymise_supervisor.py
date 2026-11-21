#!/usr/bin/env python3
"""Supervisor for the anonymise stage.

Runs `mfl_brain anonymise` in a loop, restarting it if it stalls (no chunk
progress for STALL_SECONDS) or exits with an error. The pipeline is
checkpoint-based, so restarting resumes from the last committed chunk.

Usage:
    python3 scripts/anonymise_supervisor.py [--config ingest.config.yaml]
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = "/opt/homebrew/bin/python3.12"
SITE = ROOT / ".venv" / "lib" / "python3.12" / "site-packages"
STALL_SECONDS = 900          # restart if no chunk progress for 15 min
POLL_SECONDS = 30
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "anonymise.log"


def chunk_progress(state_db: Path) -> int:
    """Count chunks in a terminal state — used to detect stalls."""
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE status IN "
                "('anonymised','quarantined','skipped')"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="ingest.config.yaml")
    ap.add_argument("--stall", type=int, default=STALL_SECONDS)
    args = ap.parse_args()

    cfg = Path(args.config)
    if not cfg.is_absolute():
        cfg = ROOT / cfg
    state_db = ROOT / ".ingest-state" / "manifest.db"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT / 'src'}:{SITE}"

    restarts = 0
    stopping = False

    def _sig(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    while not stopping:
        print(f"[supervisor] starting anonymise (restart #{restarts})", flush=True)
        with LOG.open("a") as logf:
            logf.write(f"\n===== supervisor start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            logf.flush()
            proc = subprocess.Popen(
                [PYTHON, "-m", "mfl_brain.cli", "anonymise", "--config", str(cfg)],
                cwd=str(ROOT),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )

            last_progress = -1
            last_change = time.time()
            while proc.poll() is None and not stopping:
                time.sleep(POLL_SECONDS)
                if stopping:
                    break
                n = chunk_progress(state_db)
                if n != last_progress:
                    if n >= 0:
                        print(f"[supervisor] progress: {n} chunks done", flush=True)
                    last_progress = n
                    last_change = time.time()
                elif time.time() - last_change > args.stall:
                    print(f"[supervisor] STALLED for {args.stall}s — killing", flush=True)
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break

            if stopping:
                print("[supervisor] stopping", flush=True)
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                break

            rc = proc.returncode
            done = chunk_progress(state_db)
            print(f"[supervisor] process exited rc={rc}, chunks={done}", flush=True)

            if rc == 0:
                # Pipeline finished normally (no more pending chunks)
                pending = 0
                try:
                    import sqlite3

                    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=10)
                    pending = conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE status='pending'"
                    ).fetchone()[0]
                    conn.close()
                except Exception:
                    pending = -1
                if pending == 0:
                    print("[supervisor] complete — no pending chunks remain", flush=True)
                    return 0
                print(f"[supervisor] {pending} chunks still pending — restarting", flush=True)

            restarts += 1
            time.sleep(5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
