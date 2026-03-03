"""ChromaDB-backed vector memory for project summaries and feedback."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


class VectorStore:
    """Thin wrapper around ChromaDB for storing and retrieving project embeddings."""

    def __init__(self, persist_dir: str | Path = "./data/chroma"):
        self._dir = Path(persist_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._client = None
        self._collection = None
        self._init()

    def _init(self) -> None:
        try:
            import chromadb
            self._client = chromadb.PersistentClient(path=str(self._dir))
            self._collection = self._client.get_or_create_collection(
                name="project_memory",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            print(f"[VectorStore] ChromaDB unavailable: {e} — vector search disabled")
            self._client = None
            self._collection = None

    @property
    def available(self) -> bool:
        return self._collection is not None

    def add(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.available:
            return
        try:
            self._collection.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[metadata or {}],
            )
        except Exception as e:
            print(f"[VectorStore] add failed: {e}")

    def query(
        self,
        text: str,
        n_results: int = 3,
        where: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k similar docs with metadata."""
        if not self.available:
            return []
        try:
            kwargs: dict[str, Any] = {"query_texts": [text], "n_results": n_results}
            if where:
                kwargs["where"] = where
            results = self._collection.query(**kwargs)
            hits = []
            for i, doc_id in enumerate(results["ids"][0]):
                hits.append(
                    {
                        "id": doc_id,
                        "document": results["documents"][0][i],
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "distance": results["distances"][0][i] if results["distances"] else 0.0,
                    }
                )
            return hits
        except Exception as e:
            print(f"[VectorStore] query failed: {e}")
            return []

    def delete(self, doc_id: str) -> None:
        if not self.available:
            return
        try:
            self._collection.delete(ids=[doc_id])
        except Exception:
            pass
