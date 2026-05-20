#!/bin/bash
set -euo pipefail

# =============================================================================
# Anonymity Scanner (local)
# =============================================================================
# Mirrors the PCRE pattern in `.github/workflows/anon-check.yml` so the two
# guards (pre-commit hook + CI on push/PR) stay semantically identical. Word
# boundaries + negative lookaheads keep accessibility attributes (`aria-label`,
# `aria-hidden`) and npm package names (`aria-query`, `ark-*`) out of the
# false-positive lane.
#
# Called from two places:
#   - .githooks/pre-commit  (staged-file scan, fast)
#   - manual / ad-hoc       (full-tree scan when no ANON_SCAN_PATHS env is set)
#
# Exit code:
#   0 = clean
#   1 = personal identifier found
#
# Optional env:
#   ANON_SCAN_PATHS  newline-separated subset of files to scan (used by
#                    the pre-commit hook to limit the scan to staged files).
#                    Empty = scan the whole tree.
# =============================================================================

# PCRE pattern. Mirrors `.github/workflows/anon-check.yml`. Keep them in sync.
PATTERN='(?i)(\b(araya|arayabrain|okg|okajima|tsukasa|haven|tail662202)\b|com\.okg\.|com\.haven\.|\baria\b(?!-)|\bark\b(?!-))'

EXCLUDES=(
    '.git'
    'node_modules'
    'dist'
    'build'
    '.venv'
    'venv'
    '__pycache__'
    '.next'
)
EXCLUDE_GLOBS=(
    '*.lock'
    'package-lock.json'
    'yarn.lock'
    '*.min.js'
    '*.png'
    '*.jpg'
    '*.ico'
    'anon-check.yml'
    'anon-scan.sh'
    'LICENSE'
)

scan_with_perl() {
    local file="$1"
    perl -ne 'BEGIN { $re = qr/(?i)(\b(araya|arayabrain|okg|okajima|tsukasa|haven|tail662202)\b|com\.okg\.|com\.haven\.|\baria\b(?!-)|\bark\b(?!-))/ }
              if (/$re/) { print "$ARGV:$.:$_"; $found = 1 }
              END { exit($found ? 1 : 0) }' "$file"
}

is_excluded_basename() {
    local base
    base="$(basename "$1")"
    for glob in "${EXCLUDE_GLOBS[@]}"; do
        # shellcheck disable=SC2053
        if [[ "${base}" == ${glob} ]]; then
            return 0
        fi
    done
    return 1
}

scan_paths=()
if [ -n "${ANON_SCAN_PATHS:-}" ]; then
    while IFS= read -r p; do
        [ -z "$p" ] && continue
        [ -f "$p" ] || continue
        is_excluded_basename "$p" && continue
        scan_paths+=("$p")
    done <<< "${ANON_SCAN_PATHS}"
else
    find_args=(.)
    for dir in "${EXCLUDES[@]}"; do
        find_args+=(-not -path "*/${dir}/*")
    done
    for glob in "${EXCLUDE_GLOBS[@]}"; do
        find_args+=(-not -name "${glob}")
    done
    find_args+=(-type f)
    while IFS= read -r -d '' p; do
        scan_paths+=("$p")
    done < <(find "${find_args[@]}" -print0)
fi

found=0
for path in "${scan_paths[@]}"; do
    case "$path" in
        *.png|*.jpg|*.jpeg|*.gif|*.pdf|*.zip|*.gz|*.tar|*.so|*.dylib|*.dll|*.exe|*.bin|*.csv|*.npy|*.ipynb)
            continue
            ;;
    esac
    if ! scan_with_perl "$path"; then
        found=1
    fi
done

if [ "${found}" -ne 0 ]; then
    echo ""
    echo "Personal identifiers detected. Remove or move to gitignored config."
    exit 1
fi

echo "anon-scan: clean"
exit 0
