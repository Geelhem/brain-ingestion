"""Human-monitorable status: STATUS.md + console summary."""

from __future__ import annotations

import time
from pathlib import Path

from .manifest import Manifest


def render_status(cfg, manifest: Manifest) -> str:
    counts = manifest.count_by_status()
    chunk_counts = manifest.chunk_counts()
    runs = manifest.conn.execute(
        "SELECT stage, started_at, finished_at FROM runs ORDER BY run_id DESC LIMIT 5"
    ).fetchall()
    pii = manifest.conn.execute("SELECT COUNT(*) FROM pii_map").fetchone()[0]
    lines = [
        "# Ingestion STATUS",
        "",
        f"_Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
        "## Files by stage",
    ]
    total = sum(counts.values())
    done = counts.get("indexed", 0) + counts.get("deduped", 0)
    lines += [f"- total: **{total}**", ""]
    for s in ["discovered", "extracted", "deduped", "anonymised", "synthesised",
              "indexed", "failed", "quarantined"]:
        if counts.get(s):
            lines.append(f"- `{s}`: {counts[s]}")
    lines += ["", "## Chunks", ""]
    for s, n in sorted(chunk_counts.items()):
        lines.append(f"- `{s}`: {n}")
    lines += ["", f"- PII redactions recorded: {pii}", "", "## Recent runs", ""]
    for r in runs:
        state = "done" if r["finished_at"] else "**RUNNING**"
        lines.append(f"- `{r['stage']}` {state} at {time.strftime('%H:%M:%S', time.localtime(r['started_at']))}")
    lines += ["", "## Resume", "",
              "Pipeline is checkpoint-based. Re-run any stage; completed files are skipped.",
              "```bash",
              "cd '/Volumes/Programmes SSD/ai_lab/mfl_second_brain'",
              ".venv/bin/python -m mfl_brain run --config ingest.config.yaml",
              "```",
              ""]
    return "\n".join(lines)


def write_status(cfg, manifest: Manifest) -> Path:
    path = cfg.vault_path / cfg.monitor.get("status_file", "90-System/STATUS.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_status(cfg, manifest), encoding="utf-8")
    return path


def print_summary(cfg, manifest: Manifest) -> None:
    counts = manifest.count_by_status()
    chunks = manifest.chunk_counts()
    print("files:", counts)
    print("chunks:", chunks)
