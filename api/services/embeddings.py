from __future__ import annotations

import hashlib
import math
from typing import List

from openai import OpenAI

from api.services.config import EMBEDDING_DIM, OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL


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
    """OpenAI embeddings of several texts in one request (unit length); needs ``OPENAI_API_KEY``."""
    response = OpenAI(api_key=OPENAI_API_KEY).embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=texts)
    return [_normalize(item.embedding) for item in sorted(response.data, key=lambda item: item.index)]


def embed_text(text: str) -> List[float]:
    """Embed text."""
    if OPENAI_API_KEY:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.embeddings.create(
            model=OPENAI_EMBEDDING_MODEL,
            input=text,
        )
        return response.data[0].embedding
    return _deterministic_local_embedding(text)
