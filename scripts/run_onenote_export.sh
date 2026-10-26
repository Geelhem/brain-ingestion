#!/bin/zsh
# OneNote Export Wrapper - runs Python script with logging and monitoring

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_SCRIPT="$SCRIPT_DIR/onenote_export.py"
LOG_FILE="$PROJECT_ROOT/.ingest-state/logs/onenote_export.log"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Check if OneNote is running
if ! pgrep -x "Microsoft OneNote" > /dev/null; then
    log "Starting Microsoft OneNote..."
    open -a "Microsoft OneNote"
    sleep 5
fi

# Check Accessibility permission
log "Checking Accessibility permission..."
osascript -e 'tell application "System Events" to get name of every process' > /dev/null 2>&1 || {
    log "ERROR: Accessibility permission not granted for Terminal/Script Editor"
    log "Grant in: System Settings > Privacy & Security > Accessibility"
    exit 1
}

# Run the Python script
log "Starting OneNote export (Python)..."
log "Export directory: /Volumes/GCH/Second Brain GCH Work /OneNote Exports/PDFs"

# Run with timeout (4 hours max)
timeout 14400 python3 "$PYTHON_SCRIPT" 2>&1 | while IFS= read -r line; do
    log "$line"
done

EXIT_CODE=${PIPESTATUS[0]}

if [ $EXIT_CODE -eq 0 ]; then
    log "OneNote export completed successfully"
    COUNT=$(find "/Volumes/GCH/Second Brain GCH Work /OneNote Exports/PDFs" -name "*.pdf" 2>/dev/null | wc -l | tr -d ' ')
    log "Total PDFs exported: $COUNT"
elif [ $EXIT_CODE -eq 124 ]; then
    log "Export timed out (4 hours)"
else
    log "Export failed with exit code $EXIT_CODE"
fi

exit $EXIT_CODE