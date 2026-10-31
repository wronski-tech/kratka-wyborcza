"""USD cost of model calls from ``LLM_PRICES_USD_PER_1M``: the cost panel and the per-question cost limit."""

from __future__ import annotations

from api.services import config


def usd(model: str, used: dict[str, int]) -> float | None:
    """Cost of ``used`` tokens (prompt, cached, completion) of ``model``; None for a model without a price."""
    price = config.LLM_PRICES_USD_PER_1M.get(model)
    if not price:
        return None
    prompt, cached = used.get("prompt_tokens", 0), used.get("cached_tokens", 0)
    return ((prompt - cached) * price[0] + cached * price[1] + used.get("completion_tokens", 0) * price[2]) / 1_000_000


def spent(by_model: dict[str, dict[str, int]]) -> float:
    """Cost of a question's calls so far (models without a price count 0)."""
    return sum(usd(model, used) or 0.0 for model, used in by_model.items())


def within_limit(by_model: dict[str, dict[str, int]], model: str, prompt: str, max_completion: int) -> bool:
    """Whether one more call of ``model`` keeps the question within ``QUESTION_COST_LIMIT_USD`` (0: no limit).

    The call is priced from above: about three characters per prompt token (Polish text and SQL), no cached
    tokens, and the full ``max_completion`` the call may return.
    """
    limit = config.QUESTION_COST_LIMIT_USD
    if limit <= 0:
        return True
    call = usd(model, {"prompt_tokens": len(prompt) // 3 + 1, "completion_tokens": max_completion}) or 0.0
    return spent(by_model) + call <= limit
