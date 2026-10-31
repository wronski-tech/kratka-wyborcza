from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import List

import openai

from api.services.config import EMBEDDING_DIM, OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL
from api.services.llm_client import llm_client


log = logging.getLogger(__name__)

# Emergency mode: after an OpenAI error (outage, rate limit, bad key) no OpenAI call for this long, so the
# following questions get their fallbacks at once instead of each waiting for retries and timeouts.
LLM_OUTAGE_SECONDS = 60
_llm_down_until = 0.0


def llm_available() -> bool:
    """False during the emergency mode that follows an OpenAI error."""
    return time.monotonic() >= _llm_down_until


# Errors that mean the service is unreachable or refuses every call; a bad request (unknown model, too long
# input) fails only that call.
OUTAGE_ERRORS = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError,
                 openai.AuthenticationError, openai.PermissionDeniedError)


def llm_failed(exc: Exception) -> None:
    """Start the emergency mode after an OpenAI error that means an outage (``OUTAGE_ERRORS``)."""
    global _llm_down_until
    if not isinstance(exc, OUTAGE_ERRORS):
        log.warning("OpenAI request failed: %s", exc)
        return
    _llm_down_until = time.monotonic() + LLM_OUTAGE_SECONDS
    log.warning("OpenAI error, emergency mode for %ss: %s", LLM_OUTAGE_SECONDS, exc)


def _normalize(vector: list[float]) -> list[float]:
    """Normalize."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _deterministic_local_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic local embedding."""
    vector = [0.0] * dim
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for idx in range(dim):
            byte_value = digest[idx % len(digest)]
            signed = (byte_value - 127.5) / 127.5
            vector[idx] += signed
    return _normalize(vector)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """OpenAI embeddings of several texts in one request (unit length); needs ``OPENAI_API_KEY`` and no emergency mode."""
    if not llm_available():
        raise RuntimeError("OpenAI is unavailable (emergency mode).")
    try:
        response = llm_client().embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=texts)
    except openai.OpenAIError as exc:
        llm_failed(exc)
        raise
    return [_normalize(item.embedding) for item in sorted(response.data, key=lambda item: item.index)]


def embed_text(text: str) -> List[float]:
    """OpenAI embedding of ``text``; the local deterministic one without a key or in the emergency mode."""
    if OPENAI_API_KEY and llm_available():
        try:
            response = llm_client().embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=text)
            return response.data[0].embedding
        except openai.OpenAIError as exc:
            llm_failed(exc)
    return _deterministic_local_embedding(text)
