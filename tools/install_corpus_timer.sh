#!/usr/bin/env bash
# Install the Field Horizon corpus harvester systemd USER timer.
#
# User units only. This script never touches the system systemd, never
# uses sudo, and never edits cron. It installs into
# ~/.config/systemd/user/ and, unless you pass --enable, leaves the timer
# switched off and tells you how to start it.
#
#   ./tools/install_corpus_timer.sh                 # install, do not enable
#   ./tools/install_corpus_timer.sh --enable        # install and start
#   FIELDHORIZON_HARVESTER_CONTACT=you@example.org ./tools/install_corpus_timer.sh --enable
#
# Undo with tools/uninstall_corpus_timer.sh.

set -euo pipefail

ENABLE=0
for arg in "$@"; do
  case "$arg" in
    --enable) ENABLE=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 64 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SOURCE_DIR="$REPO_ROOT/deploy/systemd-user"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. This system does not use systemd; install a" >&2
  echo "cron entry or scheduler of your choice calling:" >&2
  echo "  cd $REPO_ROOT && python -m fieldhorizon.cli corpus daemon-once" >&2
  exit 1
fi

# The contact address goes into the User-Agent of every request.
# Institutional harvesting policies require a reachable operator, and
# running without one is impolite at best.
CONTACT="${FIELDHORIZON_HARVESTER_CONTACT:-}"
if [ -z "$CONTACT" ]; then
  echo "FIELDHORIZON_HARVESTER_CONTACT is not set." >&2
  echo "Institutional sources require a reachable contact in the User-Agent." >&2
  echo "Re-run as:" >&2
  echo "  FIELDHORIZON_HARVESTER_CONTACT=you@example.org $0 $*" >&2
  exit 78
fi

# Prefer the project's virtualenv; fall back to whatever python3 is on
# PATH. Recorded absolutely in the unit, since systemd has no PATH of
# yours to inherit.
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

echo "Repository : $REPO_ROOT"
echo "Python     : $PYTHON"
echo "Contact    : $CONTACT"
echo "Unit dir   : $UNIT_DIR"
echo

# Refuse to install a timer for a harvester that would refuse to run: a
# unit that fails on every firing is worse than no unit.
if ! "$PYTHON" -m fieldhorizon.cli corpus health >/dev/null 2>&1; then
  echo "corpus health reports problems. Fix them before installing the timer:" >&2
  echo >&2
  ( cd "$REPO_ROOT" && FIELDHORIZON_HARVESTER_CONTACT="$CONTACT" "$PYTHON" -m fieldhorizon.cli corpus health ) >&2 || true
  exit 1
fi

mkdir -p "$UNIT_DIR" "$REPO_ROOT/logs/corpus"

for unit in fieldhorizon-corpus.service fieldhorizon-corpus.timer; do
  sed -e "s|__FH_ROOT__|$REPO_ROOT|g" \
      -e "s|__FH_PYTHON__|$PYTHON|g" \
      -e "s|__FH_CONTACT__|$CONTACT|g" \
      "$SOURCE_DIR/$unit" > "$UNIT_DIR/$unit"
  echo "Installed $UNIT_DIR/$unit"
done

systemctl --user daemon-reload

if [ "$ENABLE" -eq 1 ]; then
  systemctl --user enable --now fieldhorizon-corpus.timer
  echo
  echo "Timer ENABLED. Next run:"
  systemctl --user list-timers fieldhorizon-corpus.timer --no-pager || true
  echo
  echo "To have it run while you are logged out:  loginctl enable-linger $USER"
else
  echo
  echo "Units installed but NOT enabled -- nothing will run yet."
  echo "To start the schedule:"
  echo "  systemctl --user enable --now fieldhorizon-corpus.timer"
  echo "To try one cycle right now:"
  echo "  systemctl --user start fieldhorizon-corpus.service"
  echo "  journalctl --user -u fieldhorizon-corpus.service -n 50"
fi
