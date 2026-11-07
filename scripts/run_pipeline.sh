#!/bin/zsh
# Long-running ingestion loop — resumable, monitored, designed to run for days.
# Usage: zsh scripts/run_pipeline.sh
#        (interactive: asks about additional ingestion points before starting)
setopt no_unset
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
PY=.venv/bin/python
CONFIG=ingest.config.yaml

# ── Interactive: ask about additional ingestion points ──────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          MFL Second Brain — Ingestion Pipeline               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Current sources (from $CONFIG):"
$PY -c "
import yaml
with open('$CONFIG') as f:
    cfg = yaml.safe_load(f)
for s in cfg.get('sources', []):
    status = '✓' if s.get('enabled', True) else '✗'
    print(f'  {status} {s[\"name\"]:30} {s[\"path\"]}')
" 2>/dev/null
echo ""
echo "Output vault: $(grep '^vault_path:' $CONFIG | sed 's/vault_path: //;s/\"//g')"
echo "Synthesis model: gemma4:12b-mxfp8"
echo ""

# Ask if user wants to add more ingestion points
while true; do
    echo -n "Add another ingestion point? (y/n): "
    read -r answer
    case "$answer" in
        [Yy]*)
            echo -n "  Path to folder: "
            read -r new_path
            if [[ -z "$new_path" ]]; then
                echo "  (skipped — empty path)"
                continue
            fi
            echo -n "  Source name (default: auto): "
            read -r new_name
            echo -n "  Doc type hint [resource/knowledge/report/student_work/scheme/assessment/cpd/admin] (default: resource): "
            read -r new_type
            new_type="${new_type:-resource}"
            echo -n "  Delay ms between files (default: 20): "
            read -r new_delay
            new_delay="${new_delay:-20}"

            args=("$new_path")
            [[ -n "$new_name" ]] && args+=(--name "$new_name")
            args+=(--doc-type-hint "$new_type" --delay-ms "$new_delay" --crawl)
            echo "  → Adding source..."
            $PY -m mfl_brain add-path "${args[@]}" --config "$CONFIG" 2>&1 | tail -3
            echo ""
            ;;
        [Nn]*|"")
            break
            ;;
        *)
            echo "  Please answer y or n."
            ;;
    esac
done

echo ""
echo "Starting ingestion pipeline..."
echo "  Press Ctrl+C to stop. Monitor with: $PY -m mfl_brain monitor"
echo ""

# ── Main loop ──────────────────────────────────────────────────────────────
while true; do
  echo "=== cycle start $(date) ==="

  # 1. Walk for newly added files (discovery only; cheap), paced for OneDrive
  echo "--- [$(date +%H:%M:%S)] stage: crawl (walk) ---"
  $PY -m mfl_brain crawl --config $CONFIG --delay 300 --quiet || true

  # 2. Retry failed files without re-walking (OneDrive hydration recovery)
  echo "--- [$(date +%H:%M:%S)] stage: crawl (retry) ---"
  $PY -m mfl_brain crawl --config $CONFIG --no-walk --limit 2000 --delay 400 --quiet || true

  # 3. Dedup exact
  echo "--- [$(date +%H:%M:%S)] stage: dedup ---"
  $PY -m mfl_brain dedup --config $CONFIG --exact-only --quiet || true

  # 4. Anonymise pending chunks (Apple FM first, Ollama fallback)
  echo "--- [$(date +%H:%M:%S)] stage: anonymise ---"
  $PY -m mfl_brain anonymise --config $CONFIG --quiet || true

  # 5. Synthesise new files into vault notes + MOCs
  echo "--- [$(date +%H:%M:%S)] stage: synthesise ---"
  $PY -m mfl_brain synthesise --config $CONFIG --quiet || true

  # 6. Index anonymised chunks into sqlite-vec
  echo "--- [$(date +%H:%M:%S)] stage: index ---"
  $PY -m mfl_brain index --config $CONFIG --quiet || true

  # 7. Human-readable status
  $PY -m mfl_brain status --config $CONFIG --quiet || true

  echo "=== cycle done $(date) ==="
  sleep 600
done