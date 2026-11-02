"""Shared fixtures: a tiny synthetic MIND-style dataset, so tests never need the real data."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from pushnyt.features import VECTOR_FEATURES

REPO = Path(__file__).resolve().parents[1]
WORDS = ["election", "senate", "vote", "budget", "storm", "flood", "hurricane", "game", "season",
         "coach", "team", "market", "stocks", "bank", "recipe", "chef", "dinner", "film", "actor", "movie"]
SUBCATS = [("news", "politics"), ("news", "weather"), ("sports", "football"),
           ("sports", "basketball"), ("finance", "markets"), ("lifestyle", "food")]


def write_mind(root: Path, seed: int = 0, n_users: int = 40, n_news: int = 60) -> Path:
    """Write behaviors.tsv, news.tsv and entity_embedding.vec covering 2019-11-09 .. 2019-11-15."""
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)
    subcat = np.arange(n_news) % len(SUBCATS)

    with open(root / "news.tsv", "w") as f:
        for i in range(n_news):
            cat, sub = SUBCATS[subcat[i]]
            ents = json.dumps([{"WikidataId": f"Q{i % 10}"}])
            title, abstract = (" ".join(rng.choice(WORDS, 5)) for _ in range(2))
            f.write("\t".join([f"N{i}", cat, sub, title, abstract, f"https://x/{i}", ents, ents]) + "\n")

    with open(root / "entity_embedding.vec", "w") as f:  # MIND's format has a trailing tab
        f.writelines(f"Q{q}\t" + "\t".join(f"{v:.4f}" for v in rng.normal(size=8)) + "\t\n"
                     for q in range(10))

    with open(root / "behaviors.tsv", "w") as f:
        imp_id = 1
        for u in range(n_users):
            likes = rng.integers(len(SUBCATS))
            pool = np.flatnonzero(subcat == likes)
            hist = rng.choice(pool, rng.integers(0, 6), replace=False) if u % 5 else []  # every 5th is cold-start
            history = " ".join(f"N{n}" for n in hist)
            for day in range(9, 16):
                for _ in range(rng.integers(1, 4)):
                    t = pd.Timestamp(2019, 11, day) + pd.Timedelta(minutes=int(rng.integers(24 * 60)))
                    shown = rng.choice(n_news, 8, replace=False)
                    clicked = rng.random(8) < np.where(subcat[shown] == likes, 0.5, 0.08)
                    clicked[0], clicked[1] = True, False  # MIND-like: at least one click and one skip
                    items = " ".join(f"N{n}-{int(c)}" for n, c in zip(shown, clicked))
                    f.write(f"{imp_id}\tU{u}\t{t.strftime('%m/%d/%Y %I:%M:%S %p')}\t{history}\t{items}\n")
                    imp_id += 1
    return root


@pytest.fixture(scope="session")
def mind_dir(tmp_path_factory) -> Path:
    return write_mind(tmp_path_factory.mktemp("mind"))


@pytest.fixture
def train_config(mind_dir, tmp_path) -> dict:
    """configs/train.yml pointed at the synthetic data, small and fast, without the vector database."""
    config = yaml.safe_load((REPO / "configs/train.yml").read_text())
    config["data"]["dir"] = str(mind_dir)
    config["output"]["dir"] = str(tmp_path / "run")
    config["features"].update(text_dim=4, exclude=list(VECTOR_FEATURES))
    config["model"].update(n_estimators=20, early_stopping_rounds=5)
    config["evaluation"].update(bootstrap_samples=20, min_auc=None)
    return config
