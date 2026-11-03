"""Local Chroma vector database of article abstract embeddings."""

from pathlib import Path

import chromadb
import numpy as np


class ArticleStore:
    """One Chroma collection of unit-length article vectors, searched by cosine distance."""

    def __init__(self, path: Path, collection: str, reset: bool = False):
        self.client = chromadb.PersistentClient(path=str(path))
        if reset and collection in [c.name for c in self.client.list_collections()]:
            self.client.delete_collection(collection)
        self.col = self.client.get_or_create_collection(collection, metadata={"hnsw:space": "cosine"})
        self.batch = self.client.get_max_batch_size()

    def add(self, ids: list[str], vectors: np.ndarray, documents: list[str], metadatas: list[dict]) -> None:
        for i in range(0, len(ids), self.batch):
            j = i + self.batch
            self.col.add(ids=ids[i:j], embeddings=vectors[i:j], documents=documents[i:j],
                         metadatas=metadatas[i:j])

    def iter_vectors(self, ids: list[str]):
        """(start, block) pairs: stored vectors for ids[start:start + len(block)], in order.

        Fetches one batch at a time, so a caller can copy each block where it belongs
        without holding every vector twice."""
        for i in range(0, len(ids), self.batch):
            chunk = ids[i:i + self.batch]
            res = self.col.get(ids=chunk, include=["embeddings"])
            found = dict(zip(res["ids"], res["embeddings"]))
            if len(found) < len(chunk):
                raise KeyError(f"{len(chunk) - len(found)} ids are not in the vector database; "
                               "run pushnyt-embed / pushnyt-profile first")
            yield i, np.asarray([found[x] for x in chunk], dtype=np.float32)

    def vectors(self, ids: list[str]) -> np.ndarray:
        """Stored vectors in the order of `ids`."""
        blocks = [b for _, b in self.iter_vectors(ids)]
        return np.concatenate(blocks) if blocks else np.asarray([], dtype=np.float32)

    def nearest(self, queries: np.ndarray, k: int) -> list[list[str]]:
        """IDs of the k nearest articles to each query vector, closest first."""
        out = []
        for i in range(0, len(queries), 500):
            out += self.col.query(query_embeddings=queries[i:i + 500], n_results=k, include=[])["ids"]
        return out
