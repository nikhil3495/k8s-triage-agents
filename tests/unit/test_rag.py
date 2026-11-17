from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from triage.config import REPO_ROOT, Settings
from triage.rag.chunking import load_corpus, parse_runbook
from triage.rag.retriever import (
    HybridRetriever,
    build_index,
    load_or_build_retriever,
    retriever_from_index,
    save_index,
    tokenize,
)

RUNBOOKS = REPO_ROOT / "runbooks"


class HashEmbedder:
    """Deterministic bag-of-hashed-tokens embedder (stands in for Gemini embeddings)."""

    model = "hash-embedder"

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(256)
        for t in tokenize(text):
            v[int(hashlib.md5(t.encode()).hexdigest(), 16) % 256] += 1  # noqa: S324
        return list(v)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def test_corpus_has_all_runbooks_with_metadata() -> None:
    chunks = load_corpus(RUNBOOKS)
    ids = {c.runbook_id for c in chunks}
    assert len(ids) >= 13
    assert {"oomkilled", "imagepullbackoff", "service-no-endpoints"} <= ids
    for c in chunks:
        assert c.category != "unknown"
        assert c.section
        assert c.chunk_id.startswith(c.runbook_id + "#")


def test_parse_splits_long_sections(tmp_path: Path) -> None:
    body = "\n\n".join(f"para {i} " + "x" * 300 for i in range(10))
    p = tmp_path / "big.md"
    p.write_text(f"---\nid: big\ntitle: Big\ncategory: oom\n---\n## Summary\n\n{body}\n## Empty\n")
    chunks = parse_runbook(p, max_chars=1000)
    assert len(chunks) > 2
    assert chunks[0].chunk_id == "big#summary-0"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Back-off pulling image ImagePullBackOff ErrImagePull", "imagepullbackoff"),
        ("container OOMKilled exit code 137 memory limit", "oomkilled"),
        ("CrashLoopBackOff Back-off restarting failed container exit code 1", "crashloopbackoff"),
        (
            "Readiness probe failed: HTTP probe failed with statuscode: 404",
            "readiness-probe-failure",
        ),
        ("CreateContainerConfigError configmap not found", "missing-configmap-secret"),
        (
            "FailedScheduling 0/1 nodes are available: Insufficient cpu",
            "pending-pods-insufficient-resources",
        ),
        ("service has no endpoints selector does not match pod labels", "service-no-endpoints"),
        ("CPU throttling high latency container at cpu limit", "cpu-throttling-high-latency"),
        ("x509: certificate has expired", "cert-expiry"),
        ("HPA unable to get metrics, not scaling", "hpa-not-scaling"),
        ("PersistentVolumeClaim stuck Pending no persistent volumes available", "pvc-binding"),
        ("node NotReady kubelet stopped posting node status", "node-notready"),
        ("nslookup fails, no such host, CoreDNS", "dns-failure"),
    ],
)
def test_bm25_hit_at_3(retriever: HybridRetriever, query: str, expected: str) -> None:
    top = [r.runbook_id for r in retriever.search(query, k=3)]
    assert expected in top, top


def test_hybrid_mode_with_embeddings() -> None:
    chunks = load_corpus(RUNBOOKS)
    emb = HashEmbedder()
    r = HybridRetriever(chunks, emb.embed_documents([c.search_text() for c in chunks]), emb)
    assert r.mode == "hybrid"
    res = r.search("OOMKilled exit code 137", k=5)
    assert res[0].runbook_id == "oomkilled"
    assert any(x.dense_rank is not None for x in res)
    assert all(x.snippet for x in res)
    per = [x.runbook_id for x in r.search("pod failing", k=10, per_runbook=1)]
    assert len(per) == len(set(per))


def test_dense_failure_falls_back_to_bm25() -> None:
    chunks = load_corpus(RUNBOOKS)

    class Broken(HashEmbedder):
        def embed_query(self, text: str) -> list[float]:
            raise RuntimeError("quota")

    emb = Broken()
    r = HybridRetriever(chunks, emb.embed_documents([c.search_text() for c in chunks]), emb)
    assert r.search("OOMKilled", k=3)[0].runbook_id == "oomkilled"


def test_index_roundtrip_and_staleness(tmp_path: Path) -> None:
    idx = build_index(RUNBOOKS, HashEmbedder())
    path = tmp_path / "index.json"
    save_index(idx, path)
    assert retriever_from_index(idx, HashEmbedder()).mode == "hybrid"
    s = Settings(_env_file=None, rag_index_path=path, runbooks_dir=RUNBOOKS)  # type: ignore[call-arg]
    # Index has vectors but no API key -> lexical mode, still loads.
    assert load_or_build_retriever(s).mode == "bm25"
    path.write_text(path.read_text().replace(idx["corpus_hash"], "stale"))
    assert len(load_or_build_retriever(s).chunks) == len(idx["chunks"])
    missing = Settings(_env_file=None, rag_index_path=tmp_path / "none.json", runbooks_dir=RUNBOOKS)  # type: ignore[call-arg]
    assert load_or_build_retriever(missing).mode == "bm25"


def test_empty_corpus_rejected() -> None:
    with pytest.raises(ValueError, match="empty runbook corpus"):
        HybridRetriever([])
