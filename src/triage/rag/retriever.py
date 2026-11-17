"""Hybrid runbook retrieval: BM25 + dense embeddings, fused with RRF, then re-ranked.

The index (chunks + embedding vectors) is built once by ``scripts/build_index.py``
(at Docker build time) and loaded at startup. Without embeddings the retriever
degrades to BM25 + re-ranking and reports ``mode="bm25"``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from pydantic import BaseModel
from rank_bm25 import BM25Okapi

from triage.rag.chunking import Chunk, load_corpus

if TYPE_CHECKING:
    from triage.config import Settings

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-_/][a-z0-9]+)*")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "was",
        "with",
        "when",
        "why",
        "how",
        "what",
        "my",
        "our",
        "pod",
        "pods",
    ]
)
SECTION_PRIOR = {
    "symptoms": 1.0,
    "likely causes": 0.9,
    "diagnosis": 0.85,
    "remediation": 0.85,
    "summary": 0.8,
    "prevention": 0.4,
    "related": 0.2,
}
RRF_K = 60


class Embedder(Protocol):
    """Anything that can embed documents and queries."""

    model: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed corpus texts."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed one query."""
        ...


class SearchResult(BaseModel):
    """One cited chunk."""

    chunk_id: str
    runbook_id: str
    title: str
    section: str
    category: str
    score: float
    bm25_rank: int | None
    dense_rank: int | None
    snippet: str


def tokenize(text: str) -> list[str]:
    """Lower-case word/number tokens, keeping things like ``1.27``, ``exit-code``, ``/healthz``."""
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def corpus_hash(chunks: list[Chunk]) -> str:
    """Stable hash of the chunk texts, used to detect a stale index."""
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.chunk_id.encode())
        h.update(c.text.encode())
    return h.hexdigest()[:16]


class HybridRetriever:
    """BM25 + dense retrieval with reciprocal-rank fusion and a heuristic re-ranker."""

    def __init__(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]] | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        """Index ``chunks``; dense search is enabled when both vectors and embedder are given."""
        if not chunks:
            raise ValueError("empty runbook corpus")
        self.chunks = chunks
        self._bm25 = BM25Okapi([tokenize(c.search_text()) for c in chunks])
        self._embedder = embedder if vectors is not None else None
        self._matrix: np.ndarray[Any, Any] | None = None
        if vectors is not None and embedder is not None:
            m = np.asarray(vectors, dtype=np.float32)
            self._matrix = m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-9, None)

    @property
    def mode(self) -> str:
        """``hybrid`` when dense search is active, else ``bm25``."""
        return "hybrid" if self._matrix is not None else "bm25"

    def _bm25_ranking(self, query: str, n: int) -> list[int]:
        scores = self._bm25.get_scores(tokenize(query))
        order = np.argsort(-scores)[:n]
        return [int(i) for i in order if scores[i] > 0]

    def _dense_ranking(self, query: str, n: int) -> list[int]:
        if self._matrix is None or self._embedder is None:
            return []
        try:
            q = np.asarray(self._embedder.embed_query(query), dtype=np.float32)
        except Exception:  # degrade gracefully; lexical results still returned
            log.warning("query embedding failed; falling back to BM25", exc_info=True)
            return []
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        sims = self._matrix @ q
        return [int(i) for i in np.argsort(-sims)[:n]]

    def _rerank_bonus(self, query: str, chunk: Chunk) -> float:
        q = query.lower()
        bonus = 0.0
        # Exact error strings from the runbook front matter are the strongest signal.
        for s in chunk.symptoms:
            if len(s) >= 4 and s.lower() in q:
                bonus += 0.02
        if chunk.category.replace("_", " ") in q or chunk.runbook_id.replace("-", " ") in q:
            bonus += 0.01
        return bonus * SECTION_PRIOR.get(chunk.section.lower(), 0.6) + 0.004 * SECTION_PRIOR.get(
            chunk.section.lower(), 0.6
        )

    def search(self, query: str, k: int = 5, per_runbook: int = 2) -> list[SearchResult]:
        """Return the top ``k`` chunks with citations and component ranks."""
        n = max(20, k * 4)
        bm25 = self._bm25_ranking(query, n)
        dense = self._dense_ranking(query, n)
        fused: dict[int, float] = defaultdict(float)
        for rank, idx in enumerate(bm25):
            fused[idx] += 1.0 / (RRF_K + rank + 1)
        for rank, idx in enumerate(dense):
            fused[idx] += 1.0 / (RRF_K + rank + 1)
        scored = sorted(
            ((s + self._rerank_bonus(query, self.chunks[i]), i) for i, s in fused.items()),
            reverse=True,
        )
        per: dict[str, int] = defaultdict(int)
        out: list[SearchResult] = []
        for score, idx in scored:
            c = self.chunks[idx]
            if per[c.runbook_id] >= per_runbook:
                continue
            per[c.runbook_id] += 1
            out.append(
                SearchResult(
                    chunk_id=c.chunk_id,
                    runbook_id=c.runbook_id,
                    title=c.title,
                    section=c.section,
                    category=c.category,
                    score=round(score, 5),
                    bm25_rank=bm25.index(idx) + 1 if idx in bm25 else None,
                    dense_rank=dense.index(idx) + 1 if idx in dense else None,
                    snippet=c.text[:700],
                )
            )
            if len(out) >= k:
                break
        return out


# --------------------------------------------------------------------------- index I/O
def build_index(runbooks_dir: Path, embedder: Embedder | None) -> dict[str, Any]:
    """Chunk the corpus and (optionally) embed it. Returns the JSON-serialisable index."""
    chunks = load_corpus(runbooks_dir)
    vectors = embedder.embed_documents([c.search_text() for c in chunks]) if embedder else None
    return {
        "version": 1,
        "built_at": datetime.now(UTC).isoformat(),
        "corpus_hash": corpus_hash(chunks),
        "embedding_model": embedder.model if embedder else None,
        "chunks": [c.model_dump() for c in chunks],
        "vectors": vectors,
    }


def save_index(index: dict[str, Any], path: Path) -> None:
    """Write the index as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index), encoding="utf-8")


def retriever_from_index(index: dict[str, Any], embedder: Embedder | None) -> HybridRetriever:
    """Instantiate a retriever from a loaded index dict."""
    chunks = [Chunk.model_validate(c) for c in index["chunks"]]
    vectors = index.get("vectors")
    return HybridRetriever(chunks, vectors, embedder if vectors else None)


def load_or_build_retriever(settings: Settings) -> HybridRetriever:
    """Load the prebuilt index if it matches the corpus; otherwise build BM25-only in memory."""
    chunks = load_corpus(settings.runbooks_dir)
    path = settings.rag_index_path
    if path.is_file():
        index = json.loads(path.read_text(encoding="utf-8"))
        if index.get("corpus_hash") == corpus_hash(chunks):
            embedder: Embedder | None = None
            if index.get("vectors") and settings.gemini_api_key:
                from triage.rag.embeddings import GeminiEmbedder

                embedder = GeminiEmbedder(
                    index["embedding_model"], settings.gemini_api_key.get_secret_value()
                )
            return retriever_from_index(index, embedder)
        log.warning("RAG index %s is stale; using in-memory BM25 index", path)
    return HybridRetriever(chunks)
