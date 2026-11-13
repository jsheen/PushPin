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
                               "run pushpin-embed / pushpin-profile first")
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


class ExactStore:
    """In-memory exact cosine search over unit vectors, with ArticleStore's `nearest` interface.

    Only the first `size` vectors are searchable, so a catalog sorted by publish time can be
    searched as of any moment by moving `size` forward.
    """

    def __init__(self, ids: np.ndarray, vectors: np.ndarray):
        self.ids, self.vectors, self.size = np.asarray(ids), vectors, len(ids)

    def nearest(self, queries: np.ndarray, k: int) -> list[list[str]]:
        """IDs of the k nearest searchable vectors to each query, closest first."""
        V, out = self.vectors[:self.size], []
        k = min(k, len(V))
        for i in range(0, len(queries), 256):
            sims = queries[i:i + 256] @ V.T
            top = np.argpartition(-sims, k - 1, axis=1)[:, :k]
            order = np.take_along_axis(-sims, top, axis=1).argsort(axis=1, kind="stable")
            out += [self.ids[row].tolist() for row in np.take_along_axis(top, order, axis=1)]
        return out
