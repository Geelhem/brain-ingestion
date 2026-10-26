#!/bin/zsh
# Long-running ingestion loop — resumable, monitored, designed to run for days.
# Usage: nohup zsh scripts/run_pipeline.sh >> .ingest-state/pipeline.log 2>&1 &
setopt no_unset
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
PY=.venv/bin/python
CONFIG=ingest.config.yaml

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
