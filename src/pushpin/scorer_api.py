"""Scoring service: the scorer side of the push job, over HTTP.

Holds a LocalScorer (model, MIND user state, article catalog) and answers the push job's four
calls (see push_job.py). In docker compose it runs as the `scorer` service, and the push job
reaches it by service name at http://scorer:8000; no host port needs publishing.

  GET  /health             liveness, end of the user-state log, catalog size
  GET  /users?n=&seed=     a sample of users to push to
  POST /articles           {"articles": [{news_id, title, abstract, published}, ...]}
  POST /score              {"requests": [{user_id, time, news_ids, impression_id, events}, ...],
                            "features": false, "articles": [...]}
                            articles: optional, candidates to add first if missing
                            events: optional, the user's earlier impressions (e.g. past pushes with
                            tap labels), used for this request only; see LiveScorer.score_many

Responses are JSON with NaN allowed (features can be missing), which Python's json reads back
exactly. Startup loads the MIND state and takes a minute or two; /health answers once it's ready.

Usage: PUSHPIN_RUN_DIR=outputs/ablation-with-llm pushpin-scorer
"""

import json
import logging
import os
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, Query, Response
from pydantic import BaseModel

log = logging.getLogger("pushpin.scorer")


class Articles(BaseModel):
    articles: list[dict]  # news_id, title, abstract, published (ISO time)


class Requests(BaseModel):
    requests: list[dict]  # user_id, time (ISO), news_ids, impression_id, events (optional)
    features: bool = False  # also return every model input, not just the scores
    articles: list[dict] | None = None  # add these first if missing (so any replica can score)


def _json(payload) -> Response:
    # json.dumps keeps full float precision and writes NaN for missing features
    return Response(json.dumps(payload), media_type="application/json")


def create_app(scorer) -> FastAPI:
    """`scorer` is anything with the push-job scorer interface (normally nyt.LocalScorer)."""
    app = FastAPI(title="pushPin scorer", description="Live scoring for the push job")

    @app.get("/health")
    def health() -> Response:
        return _json({"status": "ok", **scorer.info()})

    @app.get("/users")
    def users(n: int = Query(1000, ge=1), seed: int = 42) -> Response:
        return _json({"users": scorer.users(n, seed)})

    @app.post("/articles")
    def articles(body: Articles) -> Response:
        df = pd.DataFrame(body.articles)
        mapped = scorer.add_articles(df.assign(published=pd.to_datetime(df["published"])))
        log.info(json.dumps({"event": "articles", "added": len(df)}))
        return _json(mapped.to_dict("list"))

    @app.post("/score")
    def score(body: Requests) -> Response:
        if body.articles:
            df = pd.DataFrame(body.articles)
            missing = scorer.missing_articles(df["news_id"].tolist())
            if missing:
                df = df[df["news_id"].isin(missing)]
                scorer.add_articles(df.assign(published=pd.to_datetime(df["published"])))
                log.info(json.dumps({"event": "articles", "added": len(df), "inline": True}))
        rows = scorer.score(body.requests, features=True) if body.features else scorer.score(body.requests)
        return _json(rows.to_dict("list"))

    return app


def main() -> None:
    import uvicorn

    from pushpin.nyt import LocalScorer  # heavy: model, MIND data, embeddings

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_dir = Path(os.environ.get("PUSHPIN_RUN_DIR", "outputs/ablation-with-llm"))
    app = create_app(LocalScorer(run_dir))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
