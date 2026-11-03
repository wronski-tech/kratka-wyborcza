"""Prometheus metrics of the API, served on ``GET /metrics`` (scraped inside the compose network, not public).

With several uvicorn workers set ``PROMETHEUS_MULTIPROC_DIR`` to an empty directory before start (production
uses a tmpfs): each worker writes its counters there and ``/metrics`` sums them, whichever worker answers.
"""

from __future__ import annotations

import os
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest, multiprocess

REQUESTS = Counter("plujka_http_requests_total", "HTTP requests by route template and status code",
                   ["method", "route", "status"])
LATENCY = Histogram("plujka_http_request_duration_seconds", "HTTP request duration by route template",
                    ["method", "route"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40))
ASKS = Counter("plujka_ask_total", "Answered /ask requests by SQL status and answer source",
               ["status", "answer_source"])
TOKENS = Counter("plujka_llm_tokens_total", "Model tokens spent on /ask", ["kind"])
COST = Counter("plujka_llm_cost_usd_total", "Model cost of /ask in USD (LLM_PRICES_USD_PER_1M)")


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    """Count one HTTP request; ``route`` is the path template (``/views/{view_id}``), so ids add no series."""
    REQUESTS.labels(method, route, str(status)).inc()
    LATENCY.labels(method, route).observe(seconds)


def observe_ask(telemetry: dict[str, Any]) -> None:
    """Count one answered question from its question-log telemetry (``api.main._telemetry``)."""
    ASKS.labels(telemetry["status"], telemetry["answer_source"] or "none").inc()
    TOKENS.labels("prompt").inc(telemetry["prompt_tokens"])
    TOKENS.labels("completion").inc(telemetry["completion_tokens"])
    COST.inc(telemetry["cost_usd"])


def render() -> tuple[bytes, str]:
    """The exposition text and its content type; summed over the workers in multiprocess mode."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
