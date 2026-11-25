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
GUARD = ROOT / "scripts" / "integrity_guard.py"
GUARD_TIMEOUT = 3600         # quick_check on the 4 GB manifest can take ~35 min
GUARD_MIN_INTERVAL = 900     # skip a repeat check if one passed this recently
GUARD_RETRY_SECONDS = 900    # idle this long after a failed/unreachable check

RC_TIMEOUT = 4  # sentinel: the guard did not finish, treat as transient


def run_integrity_guard(cfg: Path) -> int:
    """Check/repair the manifest before starting the writer.

    Returns the guard's exit code: 0 healthy/repaired, 2 unrecoverable,
    3 transient (DB busy), 4 transient (check timed out). The guard logs to
    logs/integrity.log.
    """
    print("[supervisor] running integrity guard", flush=True)
    try:
        proc = subprocess.run(
            [PYTHON, str(GUARD), "--config", str(cfg)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=GUARD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("[supervisor] integrity guard TIMED OUT", flush=True)
        return RC_TIMEOUT
    for line in (proc.stdout or "").splitlines():
        print(f"[supervisor] {line}", flush=True)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            print(f"[supervisor] guard stderr: {line}", flush=True)
    return proc.returncode


def terminate_stale_writers() -> None:
    """Kill orphaned `mfl_brain.cli anonymise` writers.

    If a previous supervisor was SIGKILLed (or crashed), its child keeps
    writing. Starting another child would put two writers on one manifest, so
    clean up any orphan before we launch ours.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, check=False,
        ).stdout
    except Exception:  # noqa: BLE001
        return
    me = os.getpid()
    for line in out.splitlines():
        line = line.strip()
        if "mfl_brain.cli anonymise" not in line or "grep" in line:
            continue
        try:
            pid = int(line.split()[0])
        except (ValueError, IndexError):
            continue
        if pid == me:
            continue
        print(f"[supervisor] terminating stale anonymise writer pid={pid}", flush=True)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        for _ in range(30):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _sleep(seconds: int, stopping) -> None:
    """Sleep up to `seconds`, waking early when a stop signal arrives."""
    for _ in range(int(seconds)):
        if stopping():
            return
        time.sleep(1)


def chunk_progress(state_db: Path) -> int:
    """Progress for stall detection.

    Prefer the worker's own log counter: the DB count query
    `SELECT COUNT(*) FROM chunks WHERE status IN (...)` scans millions of
    index entries on every 30s poll and itself stalls the supervisor on the
    large external-volume manifest. The worker prints a cumulative
    `[anonymise] N tiers=...` line every 50 chunks; read the last one.
    Fall back to the DB count only if the log has no counter yet.
    """
    import re

    try:
        with LOG.open("r", errors="replace") as f:
            for line in reversed(f.readlines()[-200:]):
                m = re.match(r"^\[anonymise\] (\d+) tiers=", line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass

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

    terminate_stale_writers()

    last_guard_ok = 0.0
    while not stopping:
        if time.time() - last_guard_ok >= GUARD_MIN_INTERVAL:
            grc = run_integrity_guard(cfg)
            if grc == 3:
                print("[supervisor] DB busy; waiting 60s before retrying the guard", flush=True)
                _sleep(60, lambda: stopping)
                continue
            if grc == RC_TIMEOUT:
                print("[supervisor] integrity check did not finish; retrying later", flush=True)
                _sleep(GUARD_RETRY_SECONDS, lambda: stopping)
                continue
            if grc != 0:
                # Confirmed unrecoverable corruption: never start the writer.
                # Stay alive and re-check later instead of exiting, so launchd
                # KeepAlive does not turn this into a crash loop.
                print(
                    f"[supervisor] CRITICAL integrity guard rc={grc} — skipping writes; "
                    f"re-check in {GUARD_RETRY_SECONDS}s (see logs/INTEGRITY-FAILURE.txt)",
                    flush=True,
                )
                _sleep(GUARD_RETRY_SECONDS, lambda: stopping)
                continue
            last_guard_ok = time.time()
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
                _sleep(POLL_SECONDS, lambda: stopping)
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
            _sleep(5, lambda: stopping)

    return 0


if __name__ == "__main__":
    sys.exit(main())
