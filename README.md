# MFL Second Brain

A local-first AI knowledge base for a Modern Foreign Languages teacher (French/Spanish).
Ingests teaching resources, student work, reports, and CPD materials from OneDrive and
Obsidian into a local Obsidian vault, with on-device anonymisation, per-document-type
synthesis, and a vector index for semantic search.

## Features

- **Resumable pipeline** — crawl → dedup → anonymise → synthesise → index, with SQLite-backed state
- **OneDrive Files On-Demand aware** — streams placeholders with size-aware timeouts instead of failing
- **On-device PII anonymisation** — Apple Foundation Models first (guided generation), Ollama fallback
- **Per-document-type synthesis** — student work → error taxonomy, reports → voice profile, knowledge → atomic notes
- **Vector search** — sqlite-vec with namespaced queries
- **Interactive TUI monitor** — pause/resume, incremental update, live progress
- **Manual path ingestion** — `add-path` / `remove-path` commands for ad-hoc folders
- **Visual progress monitor** — `scripts/progress_monitor.py` with live bar, ETA, pause/resume

## Requirements

- macOS 14+ (Apple Silicon recommended for Foundation Models)
- Python 3.12+
- [Ollama](https://ollama.com) running locally (`ollama serve`)
- Optional: `apple-fm-sdk` for on-device PII redaction

## Install

```bash
git clone <repo-url>
cd mfl-brain
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[apple,dev]"
ollama pull gemma4:12b-mxfp8    # synthesis model
ollama pull paraphrase-multilingual  # embeddings
```

## Configure

```bash
cp ingest.config.yaml.example ingest.config.yaml
# Edit ingest.config.yaml: set your source paths, output vault, and model preferences
```

The config supports:
- Multiple sources (OneDrive, local folders, Obsidian vaults)
- Per-source pacing (`delay_ms`) and file globs
- Document-type routing by path keywords
- Tag taxonomy for the output vault

## Usage

```bash
# Initialise
.venv/bin/python -m mfl_brain init

# Add a source path manually
.venv/bin/python -m mfl_brain add-path /path/to/folder --name my-folder --crawl
.venv/bin/python -m mfl_brain remove-path --name my-folder
.venv/bin/python -m mfl_brain remove-path /path/to/folder --purge

# Run individual stages
.venv/bin/python -m mfl_brain crawl --source my-folder
.venv/bin/python -m mfl_brain dedup
.venv/bin/python -m mfl_brain anonymise
.venv/bin/python -m mfl_brain synthesise
.venv/bin/python -m mfl_brain index

# Or run the full pipeline in a loop
.venv/bin/python -m mfl_brain run

# Monitor live progress (interactive TUI)
.venv/bin/python -m mfl_brain monitor
# Keys: [p] pause/resume  [u] incremental update  [r] reset cycle  [q] quit

# Query the knowledge base
.venv/bin/python -m mfl_brain ask "common errors in IB French writing"
```

## Pipeline stages

1. **Crawl** — walks source directories, extracts text (with OCR fallback for scanned PDFs), chunks content
2. **Dedup** — marks exact duplicates by SHA-256, near-duplicates by embedding distance
3. **Anonymise** — detects and replaces PII (names, emails, grades, phones) using Apple FM guided generation, falls back to Ollama
4. **Synthesise** — per-document-type prompts produce structured notes:
   - `student_work` → error taxonomy MOC
   - `report` → voice profile MOC
   - `knowledge` → atomic notes (heading-based split)
   - `cpd`, `admin`, `resource` → standard summaries
5. **Index** — builds sqlite-vec index for semantic search

## Durability and integrity

The pipeline is meant to survive reboots, sleep/wake and crashes without manual
restarts. Three pieces work together:

**WAL state database.** `.ingest-state/manifest.db` uses `journal_mode=WAL`, so
the anonymise writer can commit while integrity checks read the DB. Keep WAL
and SHM files alongside the DB; do not copy the DB alone while the pipeline is
running. Dated `.bak-*` files in `.ingest-state/` are pre-repair snapshots.

**LaunchAgents.** `scripts/install_launchd.sh` installs and reloads the plists
from `ops/launchd/` into `~/Library/LaunchAgents`:

- `com.mfl.secondbrain.anonymise` — `KeepAlive`/`RunAtLoad` supervisor for
  `scripts/anonymise_supervisor.py`. launchd relaunches it after a crash or
  reboot; the supervisor in turn restarts the `anonymise` worker and reaps
  orphaned writers left behind by a killed supervisor.
- `com.mfl.secondbrain.integrity` — runs `scripts/integrity_guard.py` every
  7200 s (`StartInterval`), at low I/O priority so it does not starve the writer.

Install / re-install after a clone or a plist change:

```bash
scripts/install_launchd.sh
launchctl list | grep mfl.secondbrain
# verify self-heal: PID must change
launchctl kickstart -k "gui/$(id -u)/com.mfl.secondbrain.anonymise"
```

**Integrity guard.** `scripts/integrity_guard.py` runs `PRAGMA quick_check`
(`--full` uses `integrity_check`) against the state DB. On index-only corruption
(it recognises `idx_files_status`, `idx_chunks_status`, `sqlite_autoindex`,
"row missing from index", etc.) it runs a non-destructive `REINDEX` and
re-checks. Unrecoverable corruption writes `logs/INTEGRITY-FAILURE.txt` and
exits 2; the supervisor then skips anonymise writes and retries later instead
of crash-looping. A busy/locked DB exits 3 and is retried. All outcomes are
logged to `logs/integrity.log`.

**Re-extracting discovered PDFs.** Use path order, not size order — size-first
pickup hits 0-byte/HTML/JSON junk left by OneDrive placeholders:

```bash
.venv/bin/python -m mfl_brain crawl --no-walk --order path --ext pdf --limit 20
```

Record `discovered`/`extracted`/`extraction_failed` counts before and after each
bounded batch (the state DB is the source of truth; see `logs/gs568_reextract.log`).

Set `TMPDIR` to a stable directory when running OCR batches. Paperclip run
scratch dirs are deleted when a heartbeat ends, and tesseract keeps intermediate
PPM files there, so an ephemeral `TMPDIR` makes OCR fail with
"image file not found" and silently moves files to `extraction_failed`:

```bash
TMPDIR=/private/tmp .venv/bin/python -m mfl_brain crawl --no-walk --order path --ext pdf --limit 2
```

Scanned PDFs from the external volume are OCR-bound (roughly 3 min/file with
four languages), so run small batches; draining all remaining `discovered`
files is a long-running job, not a single heartbeat.

## Architecture

```
src/mfl_brain/
├── config.py          # YAML config loader
├── manifest.py        # SQLite state (files, chunks, runs)
├── crawler.py         # Walk + no-walk retry with hydration
├── hydrate.py         # OneDrive placeholder streaming
├── extract.py         # Text extraction (docx, pptx, pdf, xlsx, msg)
├── dedup.py           # Exact + near-duplicate detection
├── anonymise.py       # Apple FM + Ollama PII redaction
├── synthesise.py      # Per-doc-type synthesis + MOC builders
├── atomic_splitter.py # Heading-based Obsidian note splitting
├── indexvec.py        # sqlite-vec index + query
├── monitor.py         # STATUS.md writer
├── monitor_tui.py     # Interactive TUI with keyboard input
└── cli.py             # Argparse entry point
```

## Privacy

This pipeline processes potentially sensitive educational data (student work, reports).
All processing is local — no data leaves the machine. The anonymisation stage is
designed to be the first thing that runs on extracted text, before synthesis or indexing.

**Before pushing to a remote repo:** ensure `.ingest-state/`, logs, and your real
`ingest.config.yaml` are excluded (see `.gitignore`). Commit only `ingest.config.yaml.example`.

## License

MIT
