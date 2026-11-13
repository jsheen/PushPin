"""Embed MIND article abstracts and store them in the local vector database."""

import argparse
from pathlib import Path

import yaml
from sentence_transformers import SentenceTransformer

from pushpin.data import load_news
from pushpin.vectordb import ArticleStore


def embed(config: dict) -> None:
    data_dir = Path(config["data"]["dir"])
    cfg = config["embeddings"]

    news = load_news(data_dir / config["data"]["news"])
    abstracts = news["abstract"].fillna("").str.strip()
    text = abstracts.where(abstracts != "", news["title"].fillna(""))  # ~5% have no abstract
    print(f"embedding {len(news):,} abstracts with {cfg['model']}")

    model = SentenceTransformer(cfg["model"], cache_folder=str(data_dir / "models"))
    vectors = model.encode(text.tolist(), batch_size=cfg["batch_size"], normalize_embeddings=True,
                           convert_to_numpy=True, show_progress_bar=True)

    store = ArticleStore(data_dir / cfg["chroma_dir"], cfg["collection"], reset=True)
    meta = news[["category", "subcategory", "title"]].fillna("").to_dict("records")
    store.add(news["news_id"].tolist(), vectors, text.tolist(), meta)
    print(f"stored {store.col.count():,} vectors in {data_dir / cfg['chroma_dir']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/train.yml")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    embed(config)


if __name__ == "__main__":
    main()
