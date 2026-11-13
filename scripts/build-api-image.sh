#!/usr/bin/env bash
# Build the production API image with one trained run baked in, then check that it serves
# with no mounted folders: /health reports the run, /push answers for a real impression.
# Usage: scripts/build-api-image.sh <run folder under outputs/> [image tag]
#   e.g. scripts/build-api-image.sh ablation-with-llm   -> pushpin-api:ablation-with-llm
set -euo pipefail

RUN="${1:?usage: scripts/build-api-image.sh <run folder under outputs/> [image tag]}"
TAG="${2:-pushpin-api:$RUN}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="pushpin-api-baked-smoke"
PORT=18001

for f in predictions.parquet metrics.json config.yml xgb_click.json; do
  [[ -f "$BASE_DIR/outputs/$RUN/$f" ]] || { echo "missing outputs/$RUN/$f; train that run first" >&2; exit 1; }
done

docker build -f "$BASE_DIR/Dockerfile.api" --target baked --build-arg MODEL_RUN="$RUN" -t "$TAG" "$BASE_DIR"

trap 'docker logs "$NAME" 2>/dev/null | tail -20; docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
docker rm -f "$NAME" >/dev/null 2>&1 || true

# an impression id from the baked predictions, read inside the image (no mounts anywhere)
IMP=$(docker run --rm "$TAG" "python -c \"import os, pandas as pd; \
print(pd.read_parquet(os.environ['PUSHPIN_RUN_DIR'] + '/predictions.parquet', columns=['impression_id']).impression_id.iloc[0])\"")

docker run -d --name "$NAME" -p "$PORT:8000" "$TAG" >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "localhost:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done

curl -fsS "localhost:$PORT/health" | tee /dev/stderr | grep -q "\"run\":\"$RUN\""; echo >&2
curl -fsS -X POST "localhost:$PORT/push" -H 'content-type: application/json' \
  -d "{\"impression_id\": $IMP, \"friction\": 0.2}" | grep -q '"send"'
echo "built $TAG; serves run $RUN with no mounts (/health, /push ok)"
