#!/usr/bin/env bash
#
# Bump the app's semantic version (APP_VERSION_NAME in gradle.properties).
#
# versionName is the human-facing label (e.g. 1.0.0) and is bumped intentionally
# by a person. versionCode (the integer Android uses to gate upgrades) is NOT
# touched here — CI drives it from the Actions run number, so every automated
# build is a strictly-increasing, installable upgrade. See
# .github/workflows/build-apk.yml.
#
# Usage:
#   scripts/bump.sh [patch|minor|major] [--commit] [--tag]
#
#   patch (default)  1.0.0 -> 1.0.1
#   minor            1.0.0 -> 1.1.0
#   major            1.0.0 -> 2.0.0
#
#   --commit   git-commit the gradle.properties change
#   --tag      create a v<version> git tag (implies --commit)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROPS="${REPO_ROOT}/gradle.properties"

LEVEL="patch"
DO_COMMIT=0
DO_TAG=0
for arg in "$@"; do
  case "$arg" in
    patch|minor|major) LEVEL="$arg" ;;
    --commit)          DO_COMMIT=1 ;;
    --tag)             DO_TAG=1; DO_COMMIT=1 ;;
    *) echo "Usage: $0 [patch|minor|major] [--commit] [--tag]" >&2; exit 1 ;;
  esac
done

current="$(grep -E '^APP_VERSION_NAME=' "$PROPS" | cut -d= -f2 | tr -d '[:space:]')"
if [ -z "$current" ]; then
  echo "error: APP_VERSION_NAME not found in $PROPS" >&2
  exit 1
fi

IFS='.' read -r major minor patch <<EOF
$current
EOF
major="${major:-0}"; minor="${minor:-0}"; patch="${patch:-0}"

case "$LEVEL" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
esac
new="${major}.${minor}.${patch}"

# Portable in-place edit (works with both BSD/macOS and GNU sed)
tmp="$(mktemp)"
sed "s/^APP_VERSION_NAME=.*/APP_VERSION_NAME=${new}/" "$PROPS" > "$tmp" && mv "$tmp" "$PROPS"

echo "APP_VERSION_NAME: ${current} -> ${new}"

if [ "$DO_COMMIT" = 1 ]; then
  git -C "$REPO_ROOT" add gradle.properties
  git -C "$REPO_ROOT" commit -m "chore: bump version to ${new}"
  echo "committed."
fi

if [ "$DO_TAG" = 1 ]; then
  git -C "$REPO_ROOT" tag "v${new}"
  echo "tagged v${new}"
fi
