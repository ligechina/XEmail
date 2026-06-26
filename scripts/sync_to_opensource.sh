#!/usr/bin/env bash
# Sync this working copy to the open-source release folder at
# /Users/lige/Documents/Workspace/XEmail.
#
# What gets synced:
#   - All source code, web assets, docs, requirements, README, VERSION,
#     LICENSE, the package_installers.sh script, etc. — full mirror with
#     --delete so removed files in source disappear in target too.
#   - dist/ artifacts are copied additively (no --delete) so older
#     installer .pkg / .zip files already committed to the target repo's
#     git history aren't wiped out by the sync.
#
# What is NEVER touched:
#   - data/       — user mail / accounts / config / secrets live here, on
#                   both sides.
#   - .git/       — target's git history.
#   - .gitignore, .gitattributes
#                 — target keeps its own (target ignores .packaging_tmp/
#                   but tracks dist/; source ignores both).
#   - scripts/reset_before_install.sh
#                 — target ships the sanitized open-source version (relative
#                   `dist/...` path); source has the developer convenience
#                   version with absolute personal paths.
#   - .venv/, __pycache__/, *.pyc, .DS_Store, .claude/, .packaging_tmp/
#                 — build/runtime debris that doesn't belong in the repo.
#
# Usage:
#   bash scripts/sync_to_opensource.sh           # do it
#   bash scripts/sync_to_opensource.sh --dry-run # preview only, no writes

set -euo pipefail

SRC="/Users/lige/Documents/Workspace/XEmail_pub_working"
DST="/Users/lige/Documents/Workspace/XEmail"

DRY_RUN=""
if [ "${1:-}" = "--dry-run" ] || [ "${1:-}" = "-n" ]; then
  DRY_RUN="--dry-run"
  echo "==> DRY RUN — no files will be written"
fi

if [ ! -d "$SRC" ]; then
  echo "error: source not found: $SRC" >&2
  exit 1
fi
if [ ! -d "$DST" ]; then
  echo "error: destination not found: $DST" >&2
  exit 1
fi
if [ "$SRC" = "$DST" ]; then
  echo "error: source and destination are the same path" >&2
  exit 1
fi

# Pass 1 — code, configs, web, docs, scripts (excluding dist/).
# --delete so a file removed in source is removed in target too.
echo "==> [1/2] Mirror code → $DST"
rsync -av --checksum $DRY_RUN --delete \
  --exclude='.git/' \
  --exclude='.DS_Store' \
  --exclude='.claude/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='.venv/' \
  --exclude='.packaging_tmp/' \
  --exclude='dist/' \
  --exclude='data/' \
  --exclude='.gitignore' \
  --exclude='.gitattributes' \
  --exclude='scripts/reset_before_install.sh' \
  "$SRC/" "$DST/"

# Pass 2 — installer artifacts, additive only (no --delete).
# The target's git history tracks earlier release pkgs; we add new ones
# without erasing the old ones the user has already committed.
if [ -d "$SRC/dist" ]; then
  # Copy only the NEWEST installer pair (.pkg + .zip) — older timestamped
  # builds in source/dist/ are dev history that probably shouldn't pollute
  # the release repo every sync. The user can always manually `cp` an older
  # pair if they need to reinstate one.
  latest_pkg=$(ls -t "$SRC/dist/"xemail-macos-installer-*.pkg 2>/dev/null | head -1 || true)
  latest_zip=$(ls -t "$SRC/dist/"xemail-windows-installer-*.zip 2>/dev/null | head -1 || true)
  if [ -n "$latest_pkg" ] || [ -n "$latest_zip" ]; then
    echo "==> [2/2] Copy newest installer pair → $DST/dist/"
    mkdir -p "$DST/dist"
    for f in "$latest_pkg" "$latest_zip"; do
      [ -z "$f" ] && continue
      base=$(basename "$f")
      if [ -e "$DST/dist/$base" ] && [ -z "$DRY_RUN" ]; then
        # Already there with the same name — sizes should match (timestamped
        # filenames are content-addressed by build time). Skip the copy
        # rather than rewrite a possibly-tracked git blob in place.
        echo "  skip (already present): $base"
      else
        echo "  copy: $base"
        [ -z "$DRY_RUN" ] && cp -p "$f" "$DST/dist/$base"
      fi
    done
  else
    echo "==> [2/2] No installer artifacts in $SRC/dist/ — skipping"
  fi
fi

if [ -z "$DRY_RUN" ]; then
  echo ""
  echo "==> Done. Target git status:"
  ( cd "$DST" && git status -s )
fi
