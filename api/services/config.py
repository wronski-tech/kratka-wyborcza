from __future__ import annotations

import json
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://plujka:plujka@localhost:5432/plujka")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# LLM provider (api/services/llm_client.py): "openai" (or an OpenAI-compatible endpoint at OPENAI_BASE_URL) or
# "azure" (Azure OpenAI at AZURE_OPENAI_ENDPOINT; the OPENAI_*_MODEL settings are then deployment names).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21").strip()
if LLM_PROVIDER not in ("openai", "azure"):
    raise ValueError(f"LLM_PROVIDER must be openai or azure, not {LLM_PROVIDER!r}.")
if LLM_PROVIDER == "azure" and not AZURE_OPENAI_ENDPOINT:
    raise ValueError("LLM_PROVIDER=azure needs AZURE_OPENAI_ENDPOINT (https://<resource>.openai.azure.com).")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
# Stronger model for the second SQL attempt, after the first query failed (the cheap model writes first).
OPENAI_REPAIR_MODEL = os.getenv("OPENAI_REPAIR_MODEL", "gpt-4o")
# Speech to text for spoken questions (POST /transcribe).
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
# Largest accepted recording (bytes). The browser records WAV at about 0.18 MB/s, so this is about two
# minutes; OpenAI accepts files up to 25 MB.
TRANSCRIBE_MAX_BYTES = int(os.getenv("TRANSCRIBE_MAX_BYTES", str(24 * 1024 * 1024)))
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "128"))
# Read-only login role for LLM-generated SQL over the `mart` schema (created by scripts/build_mart.py).
MART_DATABASE_URL = os.getenv(
    "MART_DATABASE_URL",
    "postgresql://plujka_mart_reader:plujka_mart_reader@localhost:5432/plujka",
)
# Server-side limit for one LLM-generated query (milliseconds).
MART_STATEMENT_TIMEOUT_MS = int(os.getenv("MART_STATEMENT_TIMEOUT_MS", "5000"))
# Upper bound on rows returned by LLM-generated SQL (a larger or missing LIMIT is replaced).
MART_MAX_ROWS = int(os.getenv("MART_MAX_ROWS", "500"))
# Planner cost above which a generated query is not run (0 disables). The heaviest reference query of the
# evaluation set costs about 360k (0.5 s); a query timing out at 5 s costs tens of millions or more.
MART_MAX_PLAN_COST = float(os.getenv("MART_MAX_PLAN_COST", "5000000"))
# Password of the read-only role that ships in docker-compose; refused when PLUJKA_ENV=production.
MART_DEFAULT_READER_PASSWORD = "plujka_mart_reader"
# "development" or "production". Production refuses to start with default credentials.
PLUJKA_ENV = os.getenv("PLUJKA_ENV", "development").strip().lower()
# Folder with DejaVuSans.ttf and DejaVuSans-Bold.ttf for the PDF reports (Polish letters); Debian's
# fonts-dejavu-core installs them here (docker/api.Dockerfile).
REPORT_FONT_DIR = os.getenv("REPORT_FONT_DIR", "/usr/share/fonts/truetype/dejavu")
# Validated question -> SQL pairs are reused for this long (seconds); 0 disables the cache.
SQL_CACHE_TTL_SECONDS = int(os.getenv("SQL_CACHE_TTL_SECONDS", "3600"))
SQL_CACHE_MAX_ENTRIES = int(os.getenv("SQL_CACHE_MAX_ENTRIES", "512"))
# Ceiling of the OpenAI cost of one question in USD (prices: LLM_PRICES_USD_PER_1M): no call that could cross it
# (the repair, clarification queries or the answer text are skipped then); 0 disables.
QUESTION_COST_LIMIT_USD = float(os.getenv("QUESTION_COST_LIMIT_USD", "0.10"))
# Semantic SQL cache: a new question reuses the validated SQL of an earlier one at this cosine similarity of their
# embeddings (and the same entities, numbers, elections and meaning words); 0 disables it.
SEMANTIC_CACHE_MIN_SIMILARITY = float(os.getenv("SEMANTIC_CACHE_MIN_SIMILARITY", "0.92"))
# OpenSearch index of verified question -> SQL examples used as few-shot prompts.
SQL_EXAMPLES_INDEX = os.getenv("SQL_EXAMPLES_INDEX", "sql_examples")
SQL_EXAMPLES_K = int(os.getenv("SQL_EXAMPLES_K", "4"))
# Natural-language answer written by the LLM from SQL rows (numbers are checked against the rows).
ANSWER_SUMMARY_ENABLED = os.getenv("ANSWER_SUMMARY_ENABLED", "1").strip().lower() in ("1", "true", "yes")
# Requests per minute and client IP for POST /ask; 0 disables the limit.
ASK_RATE_LIMIT_PER_MINUTE = int(os.getenv("ASK_RATE_LIMIT_PER_MINUTE", "30"))
# Longest question /ask accepts (the request model's hard cap is 1000), and questions per session (X-Session-Id,
# one per browser session of the UI) and UTC day; 0 disables the session limit.
QUESTION_MAX_CHARS = int(os.getenv("QUESTION_MAX_CHARS", "500"))
SESSION_QUESTION_LIMIT = int(os.getenv("SESSION_QUESTION_LIMIT", "100"))
# Optional Redis (redis://host:6379/0) that shares the rate limit across API workers and replicas.
REDIS_URL = os.getenv("REDIS_URL", "").strip()
# LLM tokens (prompt + completion) per API key or client IP and UTC day for POST /ask; 0 disables.
DAILY_TOKEN_LIMIT_PER_CLIENT = int(os.getenv("DAILY_TOKEN_LIMIT_PER_CLIENT", "0"))
# Optional comma-separated API keys; when set, POST /ask and POST /feedback require X-API-Key.
API_KEYS = frozenset(k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip())
# Keys that may read GET /costs (token usage of every key); without them only an API without API_KEYS shows it.
ADMIN_API_KEYS = frozenset(k.strip() for k in os.getenv("ADMIN_API_KEYS", "").split(",") if k.strip())
# USD per million tokens [input, cached input, output] per model, for the cost estimate of GET /costs.
LLM_PRICES_USD_PER_1M: dict[str, list[float]] = json.loads(
    os.getenv("LLM_PRICES_USD_PER_1M") or '{"gpt-4o-mini": [0.15, 0.075, 0.6], "gpt-4o": [2.5, 1.25, 10.0]}')
SEED_SAMPLE_CSV = os.getenv("SEED_SAMPLE_CSV", "data/sample/sejm_results_sample_1000.csv")
FEEDBACK_JSONL_PATH = os.getenv("FEEDBACK_JSONL_PATH", "data/feedback/feedback.jsonl")
# Skip semantic (embedding) hints for very short fragments to save latency / OpenAI calls.
QUESTION_HINTS_SEMANTIC_MIN_CHARS = int(os.getenv("QUESTION_HINTS_SEMANTIC_MIN_CHARS", "6"))
