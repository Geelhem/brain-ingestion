"""Interactive pipeline monitor with pause/resume and incremental support."""

from __future__ import annotations

import atexit
import json
import os
import select
import signal
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.align import Align
from rich.console import Console, ConsoleOptions, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text

from .config import load_config
from .manifest import Manifest


@dataclass
class PipelineState:
    paused: bool = False
    cycle: int = 0
    stage: str = "idle"
    last_update: float = field(default_factory=time.time)

    @staticmethod
    def load(path: Path) -> "PipelineState":
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return PipelineState(**data)
            except Exception:
                pass
        return PipelineState()

    def save(self, path: Path) -> None:
        self.last_update = time.time()
        path.write_text(json.dumps({
            "paused": self.paused,
            "cycle": self.cycle,
            "stage": self.stage,
            "last_update": self.last_update,
        }))


class PipelineMonitor:
    def __init__(self, config_path: str, state_dir: Path):
        self.cfg = load_config(config_path)
        self.manifest = Manifest(self.cfg.vault_path / self.cfg.state_db)
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = state_dir / "pipeline_state.json"
        self.console = Console()
        self.running = True
        self.paused = False
        self.cycle = 0
        self.stage = "idle"
        self.start_time = time.time()
        self.stage_start = time.time()
        self.last_heartbeat = 0.0
        self.heartbeat_sec = 30
        self.last_rates: dict[str, list[tuple[float, int]]] = {}

    def _load_state(self) -> None:
        s = PipelineState.load(self.state_file)
        self.paused = s.paused
        self.cycle = s.cycle
        self.stage = s.stage

    def _save_state(self) -> None:
        PipelineState(
            paused=self.paused,
            cycle=self.cycle,
            stage=self.stage,
        ).save(self.state_file)

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self._save_state()

    def get_stats(self) -> dict[str, Any]:
        file_counts = self.manifest.count_by_status()
        chunk_counts = self.manifest.chunk_counts()
        pii_count = self.manifest.conn.execute("SELECT COUNT(*) FROM pii_map").fetchone()[0]
        tier_counts = {}
        for tier in ["apple_pcc", "apple_device", "ollama", "prefilter-clean", "prefilter-unknown"]:
            tier_counts[tier] = self.manifest.conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE status='anonymised' AND tier=?", (tier,)
            ).fetchone()[0] or 0
        return {
            "files": file_counts,
            "chunks": chunk_counts,
            "pii": pii_count,
            "tiers": tier_counts,
        }

    def get_rates(self) -> dict[str, float]:
        """Estimate rates per stage from recent history."""
        rates = {}
        for stage in ["crawl", "dedup", "anonymise", "synthesise", "index"]:
            rows = self.manifest.conn.execute(
                "SELECT started_at, finished_at, note FROM runs WHERE stage=? ORDER BY run_id DESC LIMIT 5",
                (stage,)
            ).fetchall()
            if not rows:
                rates[stage] = 0.0
                continue
            total_items = 0
            total_time = 0.0
            for r in rows:
                if r["finished_at"]:
                    total_time += r["finished_at"] - r["started_at"]
                    try:
                        note = json.loads(r["note"]) if r["note"] else {}
                        if isinstance(note, dict):
                            total_items += note.get("extracted", 0) + note.get("anonymised", 0) + note.get("indexed", 0)
                    except Exception:
                        pass
            rates[stage] = total_items / total_time if total_time > 0 else 0.0
        return rates

    def estimate_eta(self, stage: str, pending: int, rate: float) -> float:
        if rate <= 0:
            return float("inf")
        return pending / rate

    def render_dashboard(self) -> Panel:
        stats = self.get_stats()
        rates = self.get_rates()

        total_files = sum(stats["files"].values())
        extracted = stats["files"].get("extracted", 0) + stats["files"].get("anonymised", 0) + \
                    stats["files"].get("synthesised", 0) + stats["files"].get("indexed", 0) + \
                    stats["files"].get("deduped", 0)
        pending_files = total_files - extracted
        pending_chunks = stats["chunks"].get("pending", 0)
        anonymised_chunks = stats["chunks"].get("anonymised", 0)
        indexed_chunks = stats["chunks"].get("indexed", 0)

        stage_order = ["crawl", "dedup", "anonymise", "synthesise", "index"]
        stage_idx = stage_order.index(self.stage) if self.stage in stage_order else 0
        progress_pct = (stage_idx / len(stage_order)) * 100

        elapsed = time.time() - self.start_time
        stage_elapsed = time.time() - self.stage_start

        # Build table
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("key", style="cyan", no_wrap=True)
        table.add_column("value", style="white")

        table.add_row("Cycle", str(self.cycle))
        table.add_row("Stage", f"[bold]{self.stage.upper()}[/bold] ({stage_idx+1}/{len(stage_order)})")
        table.add_row("Status", "[red]PAUSED[/red]" if self.paused else "[green]RUNNING[/green]")
        table.add_row("Elapsed", time.strftime("%H:%M:%S", time.gmtime(elapsed)))
        table.add_row("", "")
        table.add_row("Files total", f"{total_files:,}")
        table.add_row("Files done", f"{extracted:,}")
        table.add_row("Files pending", f"{pending_files:,}")
        table.add_row("", "")
        table.add_row("Chunks pending", f"{pending_chunks:,}")
        table.add_row("Chunks anonymised", f"{anonymised_chunks:,}")
        table.add_row("Chunks indexed", f"{indexed_chunks:,}")
        table.add_row("", "")
        table.add_row("PII redactions", f"{stats['pii']:,}")
        table.add_row("", "")
        table.add_row("Tiers", ", ".join(f"{k}: {v:,}" for k, v in stats["tiers"].items() if v))

        # ETA estimates
        eta_lines = []
        if self.stage == "crawl":
            rate = rates.get("crawl", 0)
            if rate > 0:
                eta = self.estimate_eta("crawl", pending_files, rate)
                eta_lines.append(f"Crawl ETA: {time.strftime('%H:%M:%S', time.gmtime(eta))}")
        if self.stage in ("anonymise", "dedup", "synthesise", "index"):
            rate = rates.get(self.stage, 0)
            pending = pending_chunks if self.stage == "anonymise" else (
                pending_files if self.stage == "synthesise" else (
                    anonymised_chunks if self.stage == "index" else 0))
            if rate > 0 and pending > 0:
                eta = self.estimate_eta(self.stage, pending, rate)
                eta_lines.append(f"{self.stage.capitalize()} ETA: {time.strftime('%H:%M:%S', time.gmtime(eta))}")

        if eta_lines:
            table.add_row("", "")
            for line in eta_lines:
                table.add_row("ETA", line)

        # Progress bar
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
        )
        task = progress.add_task("Pipeline", total=100, completed=progress_pct)

        layout = Layout()
        layout.split_column(
            Layout(Panel(table, title="[bold]MFL Brain Pipeline Monitor[/bold]", border_style="blue"), size=25),
            Layout(Panel(progress, title="Overall Progress", border_style="green"), size=5),
            Layout(Panel(
                Align.center(Text("Controls: [p] Pause/Resume  [u] Update (incremental)  [r] Reset cycle  [q] Quit")),
                border_style="yellow"
            ), size=4),
        )
        return Panel(layout, border_style="dim")

    def run_monitor(self) -> None:
        self._load_state()
        self.start_time = time.time()
        self.stage_start = time.time()

        def handle_key(ch: str) -> None:
            if ch in ("p", "P"):
                self.toggle_pause()
            elif ch in ("u", "U"):
                # Run incremental in a worker thread so the dashboard stays live
                threading.Thread(target=self.run_incremental, daemon=True).start()
            elif ch in ("r", "R"):
                self.cycle = 0
                self.stage = "idle"
                self.start_time = time.time()
                self._save_state()
            elif ch in ("q", "Q", "\x03", "\x1b"):
                self.running = False

        # Background thread: read single chars from stdin in cbreak mode
        fd = sys.stdin.fileno()
        old_term = termios.tcgetattr(fd)
        atexit.register(termios.tcsetattr, fd, termios.TCSADRAIN, old_term)

        def keyboard_loop() -> None:
            try:
                tty.setcbreak(fd)
                while self.running:
                    rlist, _, _ = select.select([fd], [], [], 0.1)
                    if rlist:
                        try:
                            ch = os.read(fd, 1).decode("utf-8", errors="ignore")
                        except OSError:
                            break
                        handle_key(ch)
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
                except termios.error:
                    pass

        kb_thread = threading.Thread(target=keyboard_loop, daemon=True)
        kb_thread.start()

        try:
            with Live(self.render_dashboard(), console=self.console, refresh_per_second=1/30, screen=True) as live:
                while self.running:
                    self._load_state()  # sync with external pause/resume
                    if time.time() - self.last_heartbeat >= self.heartbeat_sec:
                        self.last_heartbeat = time.time()
                        live.update(self.render_dashboard())
                    time.sleep(0.5)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
            except termios.error:
                pass

    def run_incremental(self) -> None:
        """Run incremental update: detect new/changed files and process them."""
        self.console.print("\n[bold yellow]Running incremental update...[/bold yellow]")
        from .crawler import crawl
        from .dedup import mark_exact_duplicates, mark_near_duplicates, skip_noncanonical
        from .anonymise import anonymise_chunk
        from .synthesise import write_source_note, summarise_source, write_synthesis_note, build_mocs
        from .indexvec import build_index
        from .monitor import write_status

        # Crawl new/changed files (no-walk retry + mtime check)
        crawl(self.cfg, self.manifest, verbose=True, walk=True, delay_ms=200, limit=None)
        # The crawler already skips unchanged files via mtime/sha256 check

        # Run rest of pipeline
        mark_exact_duplicates(self.manifest)
        skip_noncanonical(self.manifest)
        mark_near_duplicates(self.manifest, self.cfg.embeddings["ollama_model"])

        rows = self.manifest.pending_chunks()
        for r in rows:
            try:
                text, tier, _ = anonymise_chunk(r["text"], self.manifest, self.cfg.anonymiser)
                self.manifest.set_chunk_status(r["chunk_id"], "anonymised", text)
            except RuntimeError:
                self.manifest.set_chunk_status(r["chunk_id"], "quarantined")

        files = self.manifest.files_with_status("anonymised")
        for f in files:
            write_source_note(self.manifest, f["path"], self.cfg)
            res = summarise_source(self.manifest, f["path"], self.cfg)
            if res:
                summary, tags = res
                write_synthesis_note(self.manifest, f["path"], summary, tags, self.cfg)
                self.manifest.set_status(f["path"], "synthesised")

        build_mocs(self.cfg, self.manifest)
        build_index(self.manifest, self.cfg)
        write_status(self.cfg, self.manifest)
        self.console.print("[green]Incremental update complete[/green]")


def main(config: str, state_dir: Path) -> int:
    monitor = PipelineMonitor(config, state_dir)
    try:
        monitor.run_monitor()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="ingest.config.yaml")
    p.add_argument("--state-dir", default=".ingest-state")
    args = p.parse_args()
    sys.exit(main(args.config, Path(args.state_dir)))