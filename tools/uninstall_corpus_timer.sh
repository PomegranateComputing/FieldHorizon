#!/usr/bin/env bash
# Remove the Field Horizon corpus harvester systemd USER timer.
#
# Removes only the two unit files this project installed. It never
# touches the harvested corpus, the database, the reports, or the logs --
# uninstalling a schedule is not a request to delete data.
#
#   ./tools/uninstall_corpus_timer.sh

set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found; nothing to uninstall."
  exit 0
fi

for unit in fieldhorizon-corpus.timer fieldhorizon-corpus.service; do
  # `|| true` throughout: a partially-installed or already-removed unit
  # must not make this script fail, or an interrupted install becomes
  # impossible to clean up.
  systemctl --user stop "$unit" 2>/dev/null || true
  systemctl --user disable "$unit" 2>/dev/null || true
  if [ -f "$UNIT_DIR/$unit" ]; then
    rm -f "$UNIT_DIR/$unit"
    echo "Removed $UNIT_DIR/$unit"
  else
    echo "Not installed: $unit"
  fi
done

systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

echo
echo "Timer removed. The corpus, database, reports, and logs are untouched."
echo "Run a harvest manually any time with:"
echo "  python -m fieldhorizon.cli corpus sync"
