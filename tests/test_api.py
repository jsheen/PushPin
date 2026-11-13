import json

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from pushpin.api import create_app


@pytest.fixture
def client(tmp_path) -> TestClient:
    """A fake run: impression 1 has three candidates, impression 2 has two."""
    preds = pd.DataFrame({
        "impression_id": [1, 1, 1, 2, 2],
        "user_id": ["U1", "U1", "U1", "U2", "U2"],
        "time": pd.to_datetime(["2019-11-14 08:00"] * 3 + ["2019-11-14 19:00"] * 2),
        "slot": [0, 0, 0, 2, 2],
        "news_id": ["N1", "N2", "N3", "N4", "N5"],
        "label": [1, 0, 0, 0, 1],
        "p_click": [0.30, 0.10, 0.05, 0.2, 0.1],
        "novelty": [0.10, 0.50, 1.00, 0.3, 0.9],
        "p_return": [0.9, 0.9, 0.9, 0.2, 0.2],
    })
    preds.to_parquet(tmp_path / "predictions.parquet", index=False)
    (tmp_path / "metrics.json").write_text(json.dumps({"auc": 0.66}))
    (tmp_path / "config.yml").write_text(yaml.safe_dump({"features": {"exclude": ["sim_text"]}}))
    return TestClient(create_app(tmp_path, {"threshold": 0.2, "risk_penalty": 0.5}))


def test_health_and_model(client):
    assert client.get("/health").json()["impressions"] == 2
    model = client.get("/model").json()
    assert model["metrics"]["auc"] == 0.66 and model["excluded_features"] == ["sim_text"]


def test_user_impressions(client):
    imps = client.get("/users/U1/impressions").json()
    assert imps == [{"impression_id": 1, "time": "2019-11-14T08:00:00", "slot": "morning", "n_candidates": 3}]
    assert client.get("/users/nobody/impressions").status_code == 404


def test_score_sorts_by_click_probability(client):
    body = client.post("/score", json={"impression_id": 1}).json()
    assert [c["news_id"] for c in body["candidates"]] == ["N1", "N2", "N3"]
    assert body["user_id"] == "U1" and body["slot"] == "morning"


@pytest.mark.parametrize("friction, expected", [(0.0, "N1"), (1.0, "N3")])
def test_push_follows_friction(client, friction, expected):
    body = client.post("/push", json={"impression_id": 1, "friction": friction}).json()
    assert body["push"]["news_id"] == expected
    assert len(body["runners_up"]) == 2
    assert np.isclose(body["push"]["score"], (1 - friction) * body["push"]["engagement_rank"]
                      + friction * body["push"]["novelty_rank"])


def test_push_on_a_subset_of_candidates(client):
    body = client.post("/push", json={"impression_id": 1, "friction": 0, "news_ids": ["N2", "N3"]}).json()
    assert body["push"]["news_id"] == "N2"


def test_bad_requests(client):
    assert client.post("/push", json={"impression_id": 1, "friction": 1.5}).status_code == 422
    assert client.post("/score", json={"impression_id": 999}).status_code == 404
    assert client.post("/score", json={"impression_id": 1, "news_ids": ["N9"]}).status_code == 404


def test_push_send_decision_uses_risk_adjusted_threshold(client):
    send = client.post("/push", json={"impression_id": 1, "friction": 0}).json()["send"]
    assert send["decision"] and np.isclose(send["threshold"], 0.2 + 0.5 * 0.1)  # p_click 0.30 clears 0.25
    send = client.post("/push", json={"impression_id": 2, "friction": 0}).json()["send"]
    assert not send["decision"] and np.isclose(send["threshold"], 0.2 + 0.5 * 0.8)  # 0.2 < 0.6


def test_metrics_track_requests_and_push_decisions(client):
    client.post("/push", json={"impression_id": 1, "friction": 0})   # sent
    client.post("/push", json={"impression_id": 2, "friction": 0})   # held
    client.post("/score", json={"impression_id": 999})                # 404
    text = client.get("/metrics").text
    assert 'pushpin_push_decisions_total{decision="send"} 1.0' in text
    assert 'pushpin_push_decisions_total{decision="hold"} 1.0' in text
    assert 'pushpin_requests_total{endpoint="/score",status="404"} 1.0' in text
    assert 'pushpin_model_info{run=' in text and "pushpin_model_test_auc 0.66" in text


def test_each_request_logs_one_json_line(client, caplog):
    with caplog.at_level("INFO", logger="pushpin.api"):
        client.post("/push", json={"impression_id": 1, "friction": 0.2})
        client.get("/users/U1/impressions")
    lines = [json.loads(r.message) for r in caplog.records if r.name == "pushpin.api"]
    assert lines[0]["endpoint"] == "/push" and lines[0]["status"] == 200 and "send" in lines[0]
    assert lines[1]["endpoint"] == "/users/{user_id}/impressions"  # route template, not the raw path
