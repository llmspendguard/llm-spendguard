#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_RELEASE_VERSION_SOURCE_PATH="src/spendguard/_version.py"
readonly RELEASE_VERSION_SOURCE_PATH="${1:-${SPENDGUARD_RELEASE_VERSION_SOURCE_PATH:-$DEFAULT_RELEASE_VERSION_SOURCE_PATH}}"

TAG_VERSION="${GITHUB_REF_NAME#v}"
SOURCE_VERSION="$(python -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["__version__"])' "$RELEASE_VERSION_SOURCE_PATH")"
if [ "$TAG_VERSION" != "$SOURCE_VERSION" ]; then
  echo "::error::tag $GITHUB_REF_NAME (=$TAG_VERSION) != $RELEASE_VERSION_SOURCE_PATH ($SOURCE_VERSION) — bump the file to match the tag"
  exit 1
fi
echo "version SSOT ok: tag=$TAG_VERSION == source=$SOURCE_VERSION"
