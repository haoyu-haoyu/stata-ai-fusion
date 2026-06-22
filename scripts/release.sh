#!/usr/bin/env bash
#
# Release helper for stata-ai-fusion.
#
# Builds the current version (from pyproject.toml) and publishes it to PyPI and
# the VS Code Marketplace.  Credentials are read from the macOS login keychain
# at runtime, so NO token lives in this file, in shell history, or in the repo.
#
# One-time keychain setup (already done once):
#   security add-generic-password -a stata-pypi -s stata-ai-fusion-pypi-token -w '<pypi-token>' -U
#   security add-generic-password -a stata-vsce -s stata-ai-fusion-vsce-pat   -w '<azure-pat>'  -U
#
# Usage:
#   scripts/release.sh            # build + publish to PyPI + Marketplace
#   scripts/release.sh --dry-run  # build + verify credentials, but DO NOT publish
#
set -euo pipefail

cd "$(dirname "$0")/.."

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

VERSION="$(grep -m1 '^version' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')"
echo ">> stata-ai-fusion release v${VERSION}$( [ "$DRY_RUN" = 1 ] && echo ' (dry run)' )"

# --- credentials from the macOS keychain ---
PYPI_TOKEN="$(security find-generic-password -s stata-ai-fusion-pypi-token -w)" \
  || { echo "!! PyPI token not found in keychain (service: stata-ai-fusion-pypi-token)"; exit 1; }
VSCE_PAT="$(security find-generic-password -s stata-ai-fusion-vsce-pat -w)" \
  || { echo "!! VS Marketplace PAT not found in keychain (service: stata-ai-fusion-vsce-pat)"; exit 1; }
echo ">> credentials loaded from keychain"

# --- build ---
rm -f dist/*.whl dist/*.tar.gz
uv build >/dev/null
( cd vscode-extension && ./node_modules/.bin/vsce package >/dev/null )
echo ">> built dist/stata_ai_fusion-${VERSION}.{whl,tar.gz} + vscode-extension/stata-ai-fusion-${VERSION}.vsix"

if [ "$DRY_RUN" = 1 ]; then
  echo ">> dry run complete (nothing published)"
  exit 0
fi

# --- publish ---
UV_PUBLISH_TOKEN="$PYPI_TOKEN" uv publish "dist/stata_ai_fusion-${VERSION}"*
echo ">> PyPI: published ${VERSION}"

( cd vscode-extension && ./node_modules/.bin/vsce publish \
    --packagePath "stata-ai-fusion-${VERSION}.vsix" -p "$VSCE_PAT" )
echo ">> Marketplace: published ${VERSION}"

echo ">> done -- v${VERSION} is live on PyPI and the VS Marketplace."
