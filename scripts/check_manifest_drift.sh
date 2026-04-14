#!/usr/bin/env bash
# check_manifest_drift.sh — detect drift between sidebar modes and chatbot manifest
#
# The chatbot reads data/features/manifest.md as its knowledge base. Each mode
# must have a `## [slug] Title` heading whose slug matches a sidebar
# `data-card-mode="slug"` attribute in index.html. This script verifies that
# both sides agree on the mode list.
#
# Usage:
#   ./scripts/check_manifest_drift.sh
#
# To install as pre-commit hook:
#   ln -s ../../scripts/check_manifest_drift.sh .git/hooks/pre-commit

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INDEX="$ROOT/src/ui/static/index.html"
MANIFEST="$ROOT/data/features/manifest.md"

if [[ ! -f "$INDEX" ]]; then
  echo "❌ index.html not found at $INDEX"
  exit 1
fi
if [[ ! -f "$MANIFEST" ]]; then
  echo "❌ manifest.md not found at $MANIFEST"
  exit 1
fi

# Sidebar slugs from data-card-mode attributes
sidebar_modes=$(grep -oE 'data-card-mode="[a-z_]+"' "$INDEX" \
  | sed -E 's/data-card-mode="([a-z_]+)"/\1/' \
  | sort -u)
sidebar_count=$(echo "$sidebar_modes" | grep -c . || true)

# Manifest slugs from `## [slug] ...` headings
manifest_modes=$(grep -oE '^##[[:space:]]+\[[a-z_]+\]' "$MANIFEST" \
  | sed -E 's/^##[[:space:]]+\[([a-z_]+)\]/\1/' \
  | sort -u)
manifest_count=$(echo "$manifest_modes" | grep -c . || true)

echo "Sidebar modes:  $sidebar_count"
echo "Manifest modes: $manifest_count"

if [[ "$sidebar_count" != "$manifest_count" ]]; then
  echo ""
  echo "❌ DRIFT DETECTED — sidebar and manifest have different mode counts."
  echo ""
  echo "In sidebar but not in manifest.md:"
  comm -23 <(echo "$sidebar_modes") <(echo "$manifest_modes") | sed 's/^/  - /'
  echo ""
  echo "In manifest.md but not in sidebar:"
  comm -13 <(echo "$sidebar_modes") <(echo "$manifest_modes") | sed 's/^/  - /'
  echo ""
  echo "Edit data/features/manifest.md so every sidebar mode has a matching"
  echo "'## [slug] Title' heading, then re-run."
  exit 1
fi

# Deep check: every sidebar slug must exist in manifest
missing=$(comm -23 <(echo "$sidebar_modes") <(echo "$manifest_modes"))
if [[ -n "$missing" ]]; then
  echo "❌ Sidebar modes missing from manifest.md:"
  echo "$missing" | sed 's/^/  - /'
  exit 1
fi

echo "✅ Manifest is in sync with sidebar ($sidebar_count modes)."
