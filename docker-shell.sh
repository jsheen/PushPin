#!/usr/bin/env bash
# Build the training image and drop into an interactive shell inside it.
# Usage: ./docker-shell.sh [--no-build]
set -euo pipefail

IMAGE_NAME="pushpin-train:latest"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" != "--no-build" ]]; then
  docker build -t "$IMAGE_NAME" -f "$BASE_DIR/Dockerfile" "$BASE_DIR"
fi

mkdir -p "$BASE_DIR/data" "$BASE_DIR/outputs"

ENV_ARGS=()
[[ -f "$BASE_DIR/.env" ]] && ENV_ARGS=(--env-file "$BASE_DIR/.env")

docker run --rm -it \
  --name pushpin-shell \
  --shm-size=2g \
  ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
  -e DATA_DIR=/app/data \
  -e OUTPUT_DIR=/app/outputs \
  -v "$BASE_DIR/src":/app/src \
  -v "$BASE_DIR/configs":/app/configs \
  -v "$BASE_DIR/data":/app/data \
  -v "$BASE_DIR/outputs":/app/outputs \
  "$IMAGE_NAME" bash
