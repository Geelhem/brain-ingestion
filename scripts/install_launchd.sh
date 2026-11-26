#!/bin/zsh
# Install and (re)load the MFL Second Brain LaunchAgents (GS-568).
#
#   scripts/install_launchd.sh
#
# Installs:
#   com.mfl.secondbrain.anonymise  KeepAlive supervisor for the anonymise stage
#   com.mfl.secondbrain.integrity  scheduled PRAGMA quick_check + REINDEX guard
#
# Idempotent: a loaded agent is booted out and bootstrapped again.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
AGENTS=(com.mfl.secondbrain.anonymise com.mfl.secondbrain.integrity)

mkdir -p "$LA"
for name in "${AGENTS[@]}"; do
  src="$REPO/ops/launchd/${name}.plist"
  if [[ ! -f "$src" ]]; then
    echo "error: missing $src" >&2
    exit 1
  fi
  cp "$src" "$LA/${name}.plist"
  launchctl bootout "${DOMAIN}/${name}" 2>/dev/null || true
  # Give launchd a moment to reap the old instance before bootstrapping again.
  sleep 2
  if ! launchctl bootstrap "$DOMAIN" "$LA/${name}.plist" 2>/dev/null; then
    sleep 3
    launchctl bootstrap "$DOMAIN" "$LA/${name}.plist"
  fi
  echo "loaded ${name}"
done

echo
echo "launchctl list (mfl):"
launchctl list | grep -i "mfl.secondbrain" || true
