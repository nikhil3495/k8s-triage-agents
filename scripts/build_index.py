"""Build the runbook RAG index (run at Docker build time).

With GEMINI_API_KEY set, chunks are embedded with EMBEDDING_MODEL and the index
supports hybrid retrieval; without it the index is lexical-only (BM25 + re-rank)
and says so (``embedding_model: null``).
"""

from __future__ import annotations

import json
import os
import sys

from triage.config import get_settings
from triage.rag.chunking import load_corpus
from triage.rag.retriever import build_index, corpus_hash, save_index


def main() -> None:
    s = get_settings()
    if "--keep-if-fresh" in sys.argv and s.rag_index_path.is_file():
        existing = json.loads(s.rag_index_path.read_text(encoding="utf-8"))
        if existing.get("corpus_hash") == corpus_hash(load_corpus(s.runbooks_dir)):
            print(f"keeping fresh index (embedding_model={existing.get('embedding_model')})")
            return
    key = (
        s.gemini_api_key.get_secret_value()
        if s.gemini_api_key
        else os.environ.get("GOOGLE_API_KEY")
    )
    embedder = None
    if key:
        from triage.rag.embeddings import GeminiEmbedder

        embedder = GeminiEmbedder(s.embedding_model, key)
    index = build_index(s.runbooks_dir, embedder)
    save_index(index, s.rag_index_path)
    print(
        f"indexed {len(index['chunks'])} chunks from {s.runbooks_dir.name}/ "
        f"embedding_model={index['embedding_model']} -> {s.rag_index_path}"
    )


if __name__ == "__main__":
    main()
