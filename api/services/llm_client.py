"""The chat, embedding and transcription client of the configured LLM provider (``LLM_PROVIDER``).

``openai``: the OpenAI API, or an OpenAI-compatible endpoint given by ``OPENAI_BASE_URL``. ``azure``: Azure OpenAI
(for example a resource in an EU region) at ``AZURE_OPENAI_ENDPOINT``; the model settings (``OPENAI_CHAT_MODEL``,
``OPENAI_REPAIR_MODEL``, ``OPENAI_EMBEDDING_MODEL``, ``OPENAI_TRANSCRIBE_MODEL``) are then deployment names.
The key is ``OPENAI_API_KEY`` for both.
"""

from __future__ import annotations

import openai

from api.services import config


def llm_client() -> openai.OpenAI:
    """An API client of the configured provider."""
    if config.LLM_PROVIDER == "azure":
        return openai.AzureOpenAI(api_key=config.OPENAI_API_KEY, azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                                  api_version=config.AZURE_OPENAI_API_VERSION)
    return openai.OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL or None)
