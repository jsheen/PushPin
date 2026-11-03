"""Loaders for the MIND news-recommendation dataset."""

import csv

import numpy as np
import pandas as pd

SLOTS = ("morning", "afternoon", "evening")


def time_slot(hours: pd.Series) -> np.ndarray:
    """0 = morning (05-11h), 1 = afternoon (12-16h), 2 = evening (17-04h)."""
    return np.select([(hours >= 5) & (hours < 12), (hours >= 12) & (hours < 17)], [0, 1], 2)


def load_behaviors(path) -> pd.DataFrame:
    cols = ["impression_id", "user_id", "time", "history", "impressions"]
    df = pd.read_csv(path, sep="\t", header=None, names=cols)
    df["time"] = pd.to_datetime(df["time"], format="%m/%d/%Y %I:%M:%S %p")
    df["history"] = df["history"].fillna("").str.split()
    df["slot"] = time_slot(df["time"].dt.hour)
    return df.sort_values("time", kind="stable").reset_index(drop=True)


def load_news(path) -> pd.DataFrame:
    cols = ["news_id", "category", "subcategory", "title", "abstract", "url",
            "title_entities", "abstract_entities"]
    # Titles contain bare quotes, so disable CSV quoting
    return pd.read_csv(path, sep="\t", header=None, names=cols, quoting=csv.QUOTE_NONE)


def load_entity_embeddings(path) -> dict[str, np.ndarray]:
    df = pd.read_csv(path, sep="\t", header=None, index_col=0).dropna(axis=1, how="all")
    return dict(zip(df.index, df.to_numpy(np.float32)))


def explode_impressions(behaviors: pd.DataFrame, news_idx: dict[str, int]) -> pd.DataFrame:
    """One row per (impression, candidate article) with its click label.

    Parsed 20k impressions at a time: splitting all of them at once creates millions of
    temporary Python strings (over 1 GB), while the parsed output is only a few numeric columns.
    """
    parts, offset = [], 0
    for s in range(0, len(behaviors), 20_000):
        items = behaviors["impressions"].iloc[s:s + 20_000].str.split()
        flat = pd.Series(np.concatenate(items.to_numpy()))
        flat.index += offset  # index = position among all candidates, as if parsed in one go
        offset += len(flat)
        part = pd.DataFrame({
            "row": np.repeat(np.arange(s, s + len(items)), items.str.len().to_numpy()),
            "news": flat.str[:-2].map(news_idx),
            "label": (flat.str[-1] == "1").astype(np.int8),
        })
        parts.append(part.dropna(subset=["news"]).astype({"news": np.int32}))
    if not parts:
        return pd.DataFrame({"row": np.array([], np.int64), "news": np.array([], np.int32),
                             "label": np.array([], np.int8)})
    return pd.concat(parts)
