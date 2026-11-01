#!/usr/bin/env python3
"""Load test of the API on a built mart with the model replaced by a fake OpenAI server (no key, no cost).

Starts the API (uvicorn, in this process) and the fake server, then sends ``--requests`` requests from
``--concurrency`` threads: questions to ``POST /ask`` (each a new wording, so the SQL cache does not answer
them; the fake model returns a turnout query of the year in the question) mixed with the browse endpoints
the UI calls. Prints latency percentiles per endpoint as a Markdown table and fails when a request fails or
the 95th percentile of a group is above ``--max-p95-ms``. Rate limits are off for the run.

  DATABASE_URL=... MART_DATABASE_URL=... python3 scripts/load_test.py --requests 300 --concurrency 16

The model's own latency is not part of the numbers; add it with ``--model-delay-ms``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import statistics
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import fake_openai  # noqa: E402

TURNOUT_SQL = "SELECT year, turnout_pct FROM mart.v_turnout WHERE election = 'sejm' AND level = 'country' AND year = {year} LIMIT 5"
WORDINGS = ("Jaka była frekwencja w wyborach do Sejmu w {year}?", "Frekwencja w wyborach do Sejmu {year} w kraju",
            "Ilu wyborców głosowało w wyborach do Sejmu w {year}?", "Sejm {year}: frekwencja w całej Polsce")


def model_reply(delay: float) -> Callable[[dict], str]:
    """Fake model: the turnout query of the question's year; an answer text without numbers."""
    def reply(body: dict) -> str:
        time.sleep(delay)
        system, question = body["messages"][0]["content"], body["messages"][-1]["content"]
        if system.startswith("You write SQL"):
            year = re.search(r"\b(19|20)\d{2}\b", question)
            return json.dumps({"plan": "turnout", "sql": TURNOUT_SQL.format(year=year.group(0) if year else 2023)})
        return json.dumps({"answer": "Wynik jest w tabeli."})
    return reply


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--ask-share", type=float, default=0.5, help="Share of requests that are questions")
    parser.add_argument("--model-delay-ms", type=int, default=0, help="Latency of each fake model call")
    parser.add_argument("--max-p95-ms", type=int, default=3000, help="Fail above this 95th percentile in any group")
    args = parser.parse_args()

    _, base_url = fake_openai.start(model_reply(args.model_delay_ms / 1000))
    # Before the API is imported: its configuration is read at import time.
    os.environ.update({"OPENAI_API_KEY": "sk-fake", "OPENAI_BASE_URL": base_url, "LLM_PROVIDER": "openai",
                       "ASK_RATE_LIMIT_PER_MINUTE": "0", "SESSION_QUESTION_LIMIT": "0", "DAILY_TOKEN_LIMIT_PER_CLIENT": "0",
                       "SEMANTIC_CACHE_MIN_SIMILARITY": "0"})
    import logging

    import requests
    import uvicorn

    from api.main import app
    from api.services.mart.safe_sql import run_readonly

    for name in ("opensearch", "api.services.opensearch_store", "api.services.mart.examples", "httpx", "plujka.api"):
        logging.getLogger(name).setLevel(logging.CRITICAL)  # no OpenSearch or public tables needed for the run
    years = [r["year"] for r in run_readonly("SELECT DISTINCT year FROM mart.elections WHERE election = 'sejm' ORDER BY 1")]
    if not years:
        raise SystemExit("the mart has no Sejm elections; build it first")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    api = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    rng = random.Random(1)
    browse = [("GET /results", "/results", {"election": "sejm", "level": "wojewodztwo"}),
              ("GET /turnout", "/turnout", {"election": "sejm", "level": "powiat"}),
              ("GET /elections/{e}/{y}", "/elections/sejm/{year}", {}),
              ("GET /units/{unit}", "/units/126101", {})]
    jobs = []
    for n in range(args.requests):
        year = rng.choice(years)
        if rng.random() < args.ask_share:
            # A new wording each time (number suffix), so no question is answered from the SQL cache.
            question = rng.choice(WORDINGS).format(year=year) + f" (#{n})"
            jobs.append(("POST /ask", "post", f"{api}/ask", {"question": question}))
        else:
            label, path, params = rng.choice(browse)
            jobs.append((label, "get", api + path.format(year=year), {**params, "year": year} if params else {}))

    def run(job: tuple) -> tuple[str, float, int | str]:
        label, method, url, data = job
        started = time.monotonic()
        try:
            response = requests.post(url, json=data, timeout=60) if method == "post" else requests.get(url, params=data, timeout=60)
            outcome: int | str = response.status_code
        except requests.RequestException as exc:
            outcome = type(exc).__name__
        return label, (time.monotonic() - started) * 1000, outcome

    started = time.monotonic()
    with ThreadPoolExecutor(args.concurrency) as pool:
        results = list(pool.map(run, jobs))
    elapsed = time.monotonic() - started
    server.should_exit = True

    groups: dict[str, list[float]] = defaultdict(list)
    failures = [r for r in results if r[2] != 200]
    for label, ms, _ in results:
        groups[label].append(ms)
    groups["all"] = [ms for _, ms, _ in results]
    lines = [f"Load test: {len(results)} requests, {args.concurrency} concurrent, {elapsed:.1f} s "
             f"({len(results) / elapsed:.1f} requests/s), {len(failures)} failed; fake model delay {args.model_delay_ms} ms.", "",
             "| endpoint | requests | p50 ms | p95 ms | max ms |", "| --- | ---: | ---: | ---: | ---: |"]
    slow = []
    for label, values in sorted(groups.items(), key=lambda g: g[0] == "all"):
        p95 = statistics.quantiles(values, n=20)[-1] if len(values) > 1 else values[0]
        lines.append(f"| {label} | {len(values)} | {statistics.median(values):.0f} | {p95:.0f} | {max(values):.0f} |")
        if p95 > args.max_p95_ms:
            slow.append(label)
    print("\n".join(lines))
    for label, _, outcome in failures[:10]:
        print(f"failed: {label}: {outcome}")
    if failures or slow:
        raise SystemExit(f"{len(failures)} failed request(s); p95 above {args.max_p95_ms} ms: {', '.join(slow) or 'none'}")


if __name__ == "__main__":
    main()
