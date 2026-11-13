"""PushPin serving API: click scores and push decisions for one training run.

Serves the run's precomputed test-set predictions (predictions.parquet), so it covers the
impressions of that run's test day. Scoring arbitrary live requests would need the per-user
features computed online (a feature store); the endpoints are shaped so that can be swapped in.

    GET  /health                       liveness + which run is loaded
    GET  /model                        run name, excluded features, test metrics
    GET  /users/{user_id}/impressions  a user's impressions (to find ids to score)
    POST /score                        P(click) and novelty for an impression's candidates
    POST /push                         the push chosen by the objective at a given friction,
                                       and whether the send policy would send it

    GET  /metrics                      Prometheus metrics (service health + push behavior)

The send decision applies the threshold and risk adjustment from configs/objective.yml; the
per-user daily cap needs to know what was already sent today, so the caller enforces it.

Monitoring: every request writes one JSON log line (endpoint, status, latency, and for /push the
decision), and /metrics exposes request counts, errors, latency, and what the pushes look like
(send rate, P(click), novelty, friction). A shift in those is the early warning that a training
run, feature, or data change has gone wrong upstream.
"""

import json
import logging
import os
import time
from pathlib import Path

import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

from pushpin.data import SLOTS
from pushpin.objective import objective_parts


class ScoreRequest(BaseModel):
    impression_id: int
    news_ids: list[str] | None = Field(None, description="subset of the impression's candidates; default all")


class PushRequest(ScoreRequest):
    friction: float = Field(0.2, ge=0, le=1, description="0 = pure click model, 1 = least-seen subcategory")


log = logging.getLogger("pushpin.api")


class Metrics:
    """Prometheus metrics, in a registry per app so tests can create several apps."""

    def __init__(self, run: str, test_auc: float | None):
        self.registry = r = CollectorRegistry()
        self.requests = Counter("pushpin_requests_total", "Requests by endpoint and status",
                                ["endpoint", "status"], registry=r)
        self.latency = Histogram("pushpin_request_seconds", "Request latency by endpoint", ["endpoint"],
                                 buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5), registry=r)
        self.decisions = Counter("pushpin_push_decisions_total", "Send policy decisions on /push",
                                 ["decision"], registry=r)
        self.p_click = Histogram("pushpin_push_p_click", "Calibrated P(click) of the chosen push",
                                 buckets=(0.02, 0.04, 0.06, 0.08, 0.1, 0.12, 0.15, 0.2, 0.3), registry=r)
        self.novelty = Histogram("pushpin_push_novelty", "Novelty of the chosen push",
                                 buckets=(0.5, 0.8, 0.9, 0.95, 0.99, 1.0), registry=r)
        self.friction = Histogram("pushpin_push_friction", "Friction requested on /push",
                                  buckets=(0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0), registry=r)
        info = Gauge("pushpin_model_info", "Loaded run (value is always 1)", ["run"], registry=r)
        info.labels(run=run).set(1)
        if test_auc is not None:
            Gauge("pushpin_model_test_auc", "Test AUC of the loaded run", registry=r).set(test_auc)


def create_app(run_dir: str | Path, send_cfg: dict | None = None) -> FastAPI:
    run_dir = Path(run_dir)
    send_cfg = send_cfg or {"threshold": 0.0, "risk_penalty": 0.0}
    preds = pd.read_parquet(run_dir / "predictions.parquet")
    if "p_return" not in preds:  # runs from before the return model
        preds["p_return"] = 1.0
    metrics = json.loads((run_dir / "metrics.json").read_text())
    config_path = run_dir / "config.yml"
    config = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    positions = preds.groupby("impression_id").indices
    impressions = preds.groupby("impression_id").agg(
        user_id=("user_id", "first"), time=("time", "first"), slot=("slot", "first"),
        n_candidates=("news_id", "size")).reset_index()

    app = FastAPI(title="PushPin", description="Push notification scoring and selection")
    stats = Metrics(run_dir.name, metrics.get("auc"))

    @app.middleware("http")
    async def monitor(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        seconds = time.perf_counter() - start
        route = request.scope.get("route")
        endpoint = route.path if route else "unmatched"  # templates, e.g. /users/{user_id}/impressions
        stats.requests.labels(endpoint, str(response.status_code)).inc()
        stats.latency.labels(endpoint).observe(seconds)
        log.info(json.dumps({"method": request.method, "endpoint": endpoint, "status": response.status_code,
                             "ms": round(seconds * 1000, 1)} | getattr(request.state, "log", {})))
        return response

    def candidates(req: ScoreRequest) -> pd.DataFrame:
        if req.impression_id not in positions:
            raise HTTPException(404, f"impression {req.impression_id} is not in run {run_dir.name}")
        df = preds.iloc[positions[req.impression_id]]
        if req.news_ids is not None:
            missing = sorted(set(req.news_ids) - set(df["news_id"]))
            if missing:
                raise HTTPException(404, f"not candidates in impression {req.impression_id}: {missing}")
            df = df[df["news_id"].isin(req.news_ids)]
        return df.reset_index(drop=True)

    def header(df: pd.DataFrame) -> dict:
        row = df.iloc[0]
        return {"impression_id": int(row["impression_id"]), "user_id": row["user_id"],
                "time": row["time"].isoformat(), "slot": SLOTS[int(row["slot"])]}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "run": run_dir.name, "impressions": len(positions)}

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(generate_latest(stats.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/model")
    def model() -> dict:
        return {"run": run_dir.name, "excluded_features": config.get("features", {}).get("exclude", []),
                "metrics": metrics}

    @app.get("/users/{user_id}/impressions")
    def user_impressions(user_id: str) -> list[dict]:
        rows = impressions[impressions["user_id"] == user_id]
        if rows.empty:
            raise HTTPException(404, f"user {user_id} has no impressions in run {run_dir.name}")
        return [{"impression_id": int(r.impression_id), "time": r.time.isoformat(),
                 "slot": SLOTS[int(r.slot)], "n_candidates": int(r.n_candidates)}
                for r in rows.itertuples()]

    @app.post("/score")
    def score(req: ScoreRequest) -> dict:
        df = candidates(req).sort_values("p_click", ascending=False)
        return header(df) | {"candidates": df[["news_id", "p_click", "novelty"]].to_dict("records")}

    @app.post("/push")
    def push(req: PushRequest, request: Request) -> dict:
        df = candidates(req)
        engage, novel = objective_parts(df, df["p_click"].to_numpy())
        df = df.assign(engagement_rank=engage, novelty_rank=novel,
                       score=(1 - req.friction) * engage + req.friction * novel)
        ranked = df.sort_values("score", ascending=False, kind="stable")[
            ["news_id", "score", "p_click", "novelty", "engagement_rank", "novelty_rank"]]
        top = ranked.head(5).to_dict("records")
        risk = 1 - float(df["p_return"].iloc[0])
        threshold = send_cfg["threshold"] + send_cfg["risk_penalty"] * risk
        send = {"decision": bool(top[0]["p_click"] >= threshold), "threshold": threshold, "risk": risk}
        stats.decisions.labels("send" if send["decision"] else "hold").inc()
        stats.p_click.observe(top[0]["p_click"])
        stats.novelty.observe(top[0]["novelty"])
        stats.friction.observe(req.friction)
        request.state.log = {"impression_id": req.impression_id, "friction": req.friction,
                             "news_id": top[0]["news_id"], "p_click": round(top[0]["p_click"], 4),
                             "send": send["decision"]}
        return header(df) | {"friction": req.friction, "push": top[0], "send": send, "runners_up": top[1:]}

    return app


def main() -> None:
    import uvicorn

    config_path = Path(os.environ.get("PUSHPIN_OBJECTIVE_CONFIG", "configs/objective.yml"))
    send_cfg = yaml.safe_load(config_path.read_text())["send"] if config_path.exists() else None
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = create_app(os.environ.get("PUSHPIN_RUN_DIR", "outputs/full"), send_cfg)
    # access_log off: the monitor middleware already writes one structured line per request
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)
