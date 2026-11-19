#!/usr/bin/env python3
"""Visual progress monitor for the MFL Brain pipeline.

Shows a live progress bar with ETA, stage counts, and supports
stop/resume via keyboard (q=quit, p=pause, r=resume).

Usage:
    python3 scripts/progress_monitor.py
    python3 scripts/progress_monitor.py --stage anonymise
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DB = ROOT / ".ingest-state" / "manifest.db"
REFRESH = 2.0


def load_sources() -> list[dict]:
    """Load configured sources from ingest.config.yaml."""
    try:
        import yaml
        cfg = yaml.safe_load(open(ROOT / "ingest.config.yaml"))
        return cfg.get("sources", [])
    except Exception:
        return []


def counts() -> dict:
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        files = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM files GROUP BY status").fetchall()}
        chunks = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM chunks GROUP BY status").fetchall()}
        total_files = sum(files.values())
        total_chunks = sum(chunks.values())
        conn.close()
        return {"files": files, "chunks": chunks,
                "total_files": total_files, "total_chunks": total_chunks}
    except Exception as e:
        return {"error": str(e)}


def bar(done: int, total: int, width: int = 40) -> str:
    if total <= 0:
        return "[" + " " * width + "]"
    pct = min(1.0, done / total)
    filled = int(pct * width)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct*100:5.1f}%"


def fmt(n: int) -> str:
    return f"{n:,}"



def do_add_path() -> str:
    """Prompt user for a path and add it to config. Returns status message."""
    import subprocess
    print("\n\033[2J\033[H", end="")
    print("=== Add Path ===")
    path = input("  Enter folder path: ").strip()
    if not path:
        return "cancelled"
    name = input("  Source name (blank = auto): ").strip()
    crawl = input("  Crawl now? (y/N): ").strip().lower() == "y"
    cmd = [sys.executable, "-m", "mfl_brain", "add-path", path]
    if name:
        cmd += ["--name", name]
    if crawl:
        cmd += ["--crawl"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                             cwd=str(ROOT),
                             env={**os.environ, "PYTHONPATH": f"{ROOT/'src'}:{ROOT/'.venv'/'lib'/'python3.12'/'site-packages'}"})
        msg = (out.stdout + out.stderr).strip()
        return msg if msg else "done"
    except Exception as e:
        return f"error: {e}"


def do_remove_path() -> str:
    """Prompt user for a source name/path and remove it. Returns status message."""
    import subprocess
    print("\n\033[2J\033[H", end="")
    print("=== Remove Path ===")
    print("  Current sources:")
    try:
        out = subprocess.run(
            [sys.executable, "-m", "mfl_brain", "remove-path", "--name", "__list__"],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": f"{ROOT/'src'}:{ROOT/'.venv'/'lib'/'python3.12'/'site-packages'}"})
    except Exception:
        pass
    # Show sources from config
    try:
        import yaml
        cfg = yaml.safe_load(open(ROOT / "ingest.config.yaml"))
        for src in cfg.get("sources", []):
            print(f"    {src.get('name'):30s} {src.get('path')}")
    except Exception as e:
        print(f"  (could not list: {e})")
    target = input("\n  Enter source name or path to remove: ").strip()
    if not target:
        return "cancelled"
    purge = input("  Purge manifest rows too? (y/N): ").strip().lower() == "y"
    cmd = [sys.executable, "-m", "mfl_brain", "remove-path"]
    # detect if it's a name or path
    cmd += ["--name", target]
    if purge:
        cmd += ["--purge"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                             cwd=str(ROOT),
                             env={**os.environ, "PYTHONPATH": f"{ROOT/'src'}:{ROOT/'.venv'/'lib'/'python3.12'/'site-packages'}"})
        msg = (out.stdout + out.stderr).strip()
        return msg if msg else "done"
    except Exception as e:
        return f"error: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", "anonymise", "synthesise", "index"])
    args = ap.parse_args()

    paused = False
    running = True

    def _sigint(_s, _f):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _sigint)

    # Track rate for ETA
    prev_done = None
    prev_time = None
    rate = 0.0

    print("\033[2J\033[H", end="")  # clear screen
    while running:
        c = counts()
        if "error" in c:
            print(f"\r  DB error: {c['error']}", end="", flush=True)
            time.sleep(REFRESH)
            continue

        files = c["files"]
        chunks = c["chunks"]
        tf = c["total_files"]
        tc = c["total_chunks"]

        # Determine current stage
        n_anon = chunks.get("anonymised", 0)
        n_pend = chunks.get("pending", 0)
        n_quar = chunks.get("quarantined", 0)
        n_skip = chunks.get("skipped", 0)
        done_chunks = n_anon + n_quar + n_skip
        total_chunks = tc

        # Rate calculation (rolling window, only count non-zero deltas)
        now = time.time()
        if not hasattr(main, "_history"):
            main._history = []
        if prev_done is not None and prev_time is not None and not paused:
            dt = now - prev_time
            dd = done_chunks - prev_done
            if dt > 0 and dd > 0:
                main._history.append((dd, dt))
                if len(main._history) > 10:
                    main._history.pop(0)
            total_dd = sum(d for d, _ in main._history)
            total_dt = sum(t for _, t in main._history)
            if total_dt > 0:
                rate = total_dd / total_dt
        prev_done = done_chunks
        prev_time = now

        remaining = total_chunks - done_chunks

        # Clear and redraw
        print("\033[2J\033[H", end="")
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║          MFL Brain — Pipeline Progress Monitor               ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        if rate > 0:
            rate_str = f"{rate:6.1f} chunks/s"
        else:
            rate_str = "  processing..."
        if rate > 0 and remaining > 0:
            eta_s = remaining / rate
            if eta_s < 60:
                eta_str = f"{eta_s:.0f}s"
            elif eta_s < 3600:
                eta_str = f"{eta_s/60:.1f}m"
            elif eta_s < 86400:
                eta_str = f"{eta_s/3600:.1f}h"
            else:
                eta_str = f"{eta_s/86400:.1f}d"
        else:
            eta_str = "calculating..."
        print(f"║  Stage: {'ANONYMISE' if n_pend > 0 else 'COMPLETE/IDLE':20s}  Rate: {rate_str:>16s}  ETA: {eta_str:>12s}  ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Chunks: {bar(done_chunks, total_chunks)}  ║")
        print(f"║    anonymised: {fmt(n_anon):>10s}   pending: {fmt(n_pend):>10s}   quarantined: {fmt(n_quar):>6s}  ║")
        print(f"║    skipped:    {fmt(n_skip):>10s}   total:   {fmt(total_chunks):>10s}                            ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Files:  {bar(sum(1 for s in files if s in ('anonymised','deduped','extracted')), tf)}  ║")
        for s in ["extracted", "anonymised", "deduped", "discovered", "extraction_failed", "failed", "quarantined"]:
            v = files.get(s, 0)
            if v > 0:
                print(f"║    {s:20s}: {fmt(v):>10s}                                    ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        sources = load_sources()
        print(f"║  Ingestion Paths ({len(sources)}):                                                ║")
        for src in sources:
            name = src.get("name", "?")[:22]
            path = src.get("path", "?")
            enabled = "✓" if src.get("enabled", True) else "✗"
            # Truncate path for display
            if len(path) > 42:
                path = "..." + path[-39:]
            print(f"║    {enabled} {name:22s} {path:42s}  ║")
        # Show last log line for activity feedback
        try:
            with open("/tmp/anonymise.log") as f:
                lines = f.readlines()
            last_log = lines[-1].strip() if lines else "(no log)"
            if len(last_log) > 60:
                last_log = last_log[:57] + "..."
        except Exception:
            last_log = "(no log)"
        print(f"║  Last: {last_log:60s}  ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print("║  Controls: [q] quit [p] pause [r] resume [a] add-path [d] remove-path [Ctrl+C] exit ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        spin_idx = int(time.time() * 4) % len(spinner)
        if paused:
            print("  ⏸  PAUSED — press 'r' to resume")
        else:
            print(f"  {spinner[spin_idx]} Live   Last update: {time.strftime('%H:%M:%S')}   (refreshes every {REFRESH:.0f}s)")

        # Non-blocking keyboard input
        if sys.stdin.isatty():
            import select
            rlist, _, _ = select.select([sys.stdin], [], [], REFRESH)
            if rlist:
                ch = sys.stdin.read(1)
                if ch == "q":
                    running = False
                elif ch == "p":
                    paused = True
                elif ch == "r":
                    paused = False
                elif ch == "a":
                    msg = do_add_path()
                    print(f"\n  Result: {msg}")
                    input("  Press Enter to return to monitor...")
                elif ch == "d":
                    msg = do_remove_path()
                    print(f"\n  Result: {msg}")
                    input("  Press Enter to return to monitor...")
        else:
            time.sleep(REFRESH)

    print("\nMonitor exited.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
