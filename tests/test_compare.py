import numpy as np
import pandas as pd
import pytest

from pushpin.compare import load_pair, paired_bootstrap


def _rows(seed=0, n_imp=300, per=10):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"impression_id": np.repeat(np.arange(n_imp), per),
                       "news_id": np.tile([f"N{j}" for j in range(per)], n_imp),
                       "label": rng.integers(0, 2, n_imp * per)})
    noise = rng.random(len(df))
    return df, noise


def test_better_model_has_positive_ci():
    df, noise = _rows()
    y = df["label"].to_numpy()
    weak, strong = noise + 0.1 * y, noise + 0.5 * y
    r = paired_bootstrap(df, weak, strong, n=200, level=0.95, seed=0)
    for k in ("auc", "group_auc"):
        assert r[k]["diff"] > 0 and r[k]["ci"][0] > 0 and r[k]["p_b_not_better"] == 0


def test_identical_models_have_zero_diff():
    df, noise = _rows()
    r = paired_bootstrap(df, noise, noise, n=50, level=0.95, seed=0)
    assert r["auc"]["diff"] == 0 and r["auc"]["ci"] == [0, 0]


def test_load_pair_aligns_rows_in_different_order(tmp_path):
    df, noise = _rows(n_imp=20)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    df.assign(p_click=noise).to_parquet(a / "predictions.parquet")
    df.assign(p_click=2 * noise).sample(frac=1, random_state=0).to_parquet(b / "predictions.parquet")
    _, sa, sb = load_pair(a, b)
    assert np.allclose(sb, 2 * sa)


def test_load_pair_rejects_different_test_sets(tmp_path):
    df, noise = _rows(n_imp=20)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    df.assign(p_click=noise).to_parquet(a / "predictions.parquet")
    df.iloc[:-1].assign(p_click=noise[:-1]).to_parquet(b / "predictions.parquet")
    with pytest.raises(ValueError):
        load_pair(a, b)
