#!/usr/bin/env bash
set -euo pipefail

# Regenerates the architecture-review dump.
#
# The previous dump (FieldHorizon_ARCHITECTURE_REVIEW_*.txt) walked the raw
# filesystem: it picked up 15,000+ lines of Rust build fingerprints under
# fieldhorizon-rust/target/ while never once dumping the actual .rs source
# files, and it included the quarantined Quran text (data/quarantine/) as
# if it were live corpus material -- the dump filter was inverted for the
# Rust tree (kept the noise, dropped the source) and blind to quarantine.
#
# This version builds its file list from git itself (tracked files, plus
# untracked files git does not ignore), so .gitignore is the single source
# of truth for target/, outputs/, logs/, *.sqlite3, etc. On top of that:
#   - data/quarantine/ is hard-excluded regardless of git-tracking status
#     (it is tracked, but quarantined material never belongs in a review).
#   - Prior dump artifacts (FieldHorizon_ARCHITECTURE_REVIEW_*.txt is
#     tracked and NOT gitignored) are excluded too -- otherwise a stale
#     dump's own embedded "FILE: ./...target/..." text gets swept into
#     the new one verbatim, since it's just text as far as git is concerned.
#   - fieldhorizon-rust/src/*.rs presence is asserted as a regression guard
#     for the exact defect this script replaces.
#
# Usage: scripts/make_review_dump.sh [output_path]

cd "$(git rev-parse --show-toplevel)"

OUT="${1:-fieldhorizon_full_project_dump_$(date +%Y%m%d_%H%M%S).txt}"

# --cached: tracked files. --others --exclude-standard: untracked files
# not covered by .gitignore / .git/info/exclude / core.excludesFile. Together
# this is "everything a reviewer should see", filtered by the same rules
# git itself uses.
mapfile -t FILES < <(
    git ls-files --cached --others --exclude-standard -z | tr '\0' '\n' \
        | grep -Ev '^data/quarantine/|(^|/)target/|^outputs/|^logs/|\.sqlite3$|^(FieldHorizon_ARCHITECTURE_REVIEW|fieldhorizon_full_project_dump)_.*\.txt$'
)

if [ "${#FILES[@]}" -eq 0 ]; then
    echo "error: filtered file list is empty -- refusing to write an empty dump" >&2
    exit 1
fi

rust_src_count=0
for f in "${FILES[@]}"; do
    case "$f" in
        fieldhorizon-rust/src/*.rs) rust_src_count=$((rust_src_count + 1)) ;;
    esac
done
if [ "$rust_src_count" -eq 0 ]; then
    echo "warning: no fieldhorizon-rust/src/*.rs files found -- this is the exact regression this script exists to prevent" >&2
fi

{
    echo "============================================================"
    echo "FIELD HORIZON - ARCHITECTURE REVIEW DUMP"
    echo "============================================================"
    echo
    echo "DATE:"
    date
    echo
    echo "GIT:"
    echo "  branch: $(git rev-parse --abbrev-ref HEAD)"
    echo "  commit: $(git rev-parse HEAD)"
    echo
    echo "============================================================"
    echo "PROJECT TREE"
    echo "============================================================"
    printf '%s\n' "${FILES[@]}" | tree --fromfile
    echo
    echo "============================================================"
    echo "FILES"
    echo "============================================================"

    for f in "${FILES[@]}"; do
        echo
        echo "############################################################"
        echo "FILE: ./$f"
        echo "############################################################"
        if [ -f "$f" ]; then
            cat "$f"
        else
            echo "[tracked in git but missing on disk]"
        fi
    done
} > "$OUT"

echo "Wrote $OUT ($(wc -l < "$OUT") lines, ${#FILES[@]} files, ${rust_src_count} Rust source files)"
