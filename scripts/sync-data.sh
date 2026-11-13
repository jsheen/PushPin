#!/usr/bin/env bash
# Sync the GCS data bucket into ./data (only downloads new/changed files).
# Usage: ./scripts/sync-data.sh
set -euo pipefail

BUCKET="${BUCKET:-gs://pushpin-data}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$BASE_DIR/data"
gcloud storage rsync -r "$BUCKET" "$BASE_DIR/data"
