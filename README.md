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
- **Manual path ingestion** — `add-path` command for ad-hoc folders

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
