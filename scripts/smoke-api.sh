#!/usr/bin/env bash
# Start the API image on a tiny fixture run and check that /health, /push, and /metrics respond.
# Used by CI before images are released; also runnable locally.
# Usage: scripts/smoke-api.sh [image]   (default pushpin-api:latest)
set -euo pipefail

IMAGE="${1:-pushpin-api:latest}"
NAME="pushpin-api-smoke"
PORT=18000
RUN_DIR="$(mktemp -d)"
trap 'docker logs "$NAME" 2>/dev/null | tail -20; docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$RUN_DIR"' EXIT
chmod 777 "$RUN_DIR"

cat > "$RUN_DIR/make_fixture.py" <<'PY'
import json
import pandas as pd
pd.DataFrame({
    "impression_id": [1, 1, 1], "user_id": ["U1"] * 3, "time": pd.to_datetime(["2019-11-14 08:00"] * 3),
    "slot": [0] * 3, "news_id": ["N1", "N2", "N3"], "label": [1, 0, 0],
    "p_click": [0.3, 0.1, 0.05], "novelty": [0.1, 0.5, 1.0], "p_return": [0.9] * 3,
}).to_parquet("outputs/smoke/predictions.parquet", index=False)
open("outputs/smoke/metrics.json", "w").write(json.dumps({"auc": 0.66}))
PY
docker run --rm -v "$RUN_DIR":/app/outputs/smoke "$IMAGE" "python outputs/smoke/make_fixture.py"

docker run -d --name "$NAME" -p "$PORT:8000" -e PUSHPIN_RUN_DIR=outputs/smoke \
  -v "$RUN_DIR":/app/outputs/smoke:ro "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "localhost:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done

curl -fsS "localhost:$PORT/health"; echo
curl -fsS -X POST "localhost:$PORT/push" -H 'content-type: application/json' \
  -d '{"impression_id": 1, "friction": 0.2}' | grep -q '"send"'
curl -fsS "localhost:$PORT/metrics" | grep -q 'pushpin_push_decisions_total{decision='
echo "smoke test passed: /health, /push, /metrics"
