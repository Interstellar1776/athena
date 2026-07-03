#!/usr/bin/env python3
"""embeddings.py — the minimal embedding surface for the semantic-retrieval arm (Build Sequence §19 step 8).

Step 8 runs the deferred RAG-vs-filtering bake-off (docs/open_questions.md, decisions_log BS7) against
realistic messy transcripts. To do it honestly we need a *real* semantic arm — this module is it: a thin
Ollama-embeddings call plus a numpy cosine ranker. No heavy deps (no faiss/torch/sentence-transformers);
just `httpx` + `numpy`, matching the project's local-first stance.

Env-driven, same shape as `llm_client` (§16): the endpoint is `LLM_ENDPOINT` (local Ollama by default)
and the model is `LLM_EMBED_MODEL` (e.g. `nomic-embed-text`) — never hardcoded. Failures halt loudly
with context (§17). The ranking core (`cosine_rank`) is pure numpy so tests can inject deterministic
vectors and stay hermetic (no Ollama needed).
"""

from __future__ import annotations

import os

import numpy as np

DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"


class EmbeddingError(RuntimeError):
    """An embedding request failed — carries context for a loud, actionable halt (§17)."""


# ===========================================================================
# 1. Embed — Ollama /api/embeddings (local-first; env-configured model/endpoint)
# ===========================================================================
def embed_texts(texts: list[str], *, model: str | None = None, endpoint: str | None = None) -> np.ndarray:
    """Embed each text into a row of a float matrix via Ollama's native embeddings API.

    ``model`` falls back to ``LLM_EMBED_MODEL``; ``endpoint`` to ``LLM_ENDPOINT`` then local Ollama.
    Returns an ``(len(texts), dim)`` array; an empty input yields an empty ``(0, 0)`` array."""
    if not texts:
        return np.empty((0, 0))

    import httpx

    resolved_model = model or os.getenv("LLM_EMBED_MODEL")
    if not resolved_model:
        raise EmbeddingError("LLM_EMBED_MODEL is not set and no model was passed — set the env var or "
                             "pass model= (e.g. 'nomic-embed-text').")
    base = (endpoint or os.getenv("LLM_ENDPOINT") or DEFAULT_OLLAMA_ENDPOINT).rstrip("/")

    vectors: list[list[float]] = []
    try:
        for text in texts:
            resp = httpx.post(f"{base}/api/embeddings",
                              json={"model": resolved_model, "prompt": text}, timeout=120.0)
            resp.raise_for_status()
            vectors.append(resp.json()["embedding"])
    except Exception as exc:                                  # noqa: BLE001 — fail loud, with context
        raise EmbeddingError(f"embed_texts: Ollama embeddings request failed "
                             f"(model={resolved_model!r}, endpoint={base!r}): {exc}") from exc
    return np.asarray(vectors, dtype=float)


# ===========================================================================
# 2. Rank — cosine similarity (pure numpy; injectable, hermetically testable)
# ===========================================================================
def cosine_rank(query_vec: np.ndarray, matrix: np.ndarray) -> list[tuple[int, float]]:
    """Rank the rows of ``matrix`` by cosine similarity to ``query_vec``, most-similar first.

    Returns ``[(row_index, score), …]``. Zero-norm vectors score 0 (never a divide-by-zero)."""
    if matrix.size == 0:
        return []
    q = np.asarray(query_vec, dtype=float).ravel()
    q_norm = np.linalg.norm(q)
    row_norms = np.linalg.norm(matrix, axis=1)
    denom = row_norms * q_norm
    scores = np.divide(matrix @ q, denom, out=np.zeros(len(matrix)), where=denom != 0)
    order = np.argsort(-scores)
    return [(int(i), float(scores[i])) for i in order]
