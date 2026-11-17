"""Gemini embeddings (thin wrapper; excluded from unit-test coverage, exercised in the eval)."""

from __future__ import annotations

from google import genai
from google.genai import types


class GeminiEmbedder:
    """Embed documents and queries with a Gemini embedding model."""

    def __init__(self, model: str, api_key: str | None = None, dim: int = 768) -> None:
        """Create a client; ``api_key`` defaults to GEMINI_API_KEY / GOOGLE_API_KEY."""
        self.model = model
        self.dim = dim
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 50):
            res = self._client.models.embed_content(
                model=self.model,
                contents=texts[i : i + 50],
                config=types.EmbedContentConfig(
                    task_type=task_type, output_dimensionality=self.dim
                ),
            )
            out.extend(list(e.values or []) for e in res.embeddings or [])
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed corpus chunks."""
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query."""
        return self._embed([text], "RETRIEVAL_QUERY")[0]
