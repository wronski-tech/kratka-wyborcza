"""End-to-end ``POST /ask`` on a built mart with the model replaced by a fake OpenAI-compatible server.

Everything but the model is real: the OpenAI SDK talks HTTP to the fake server (``OPENAI_BASE_URL``), and
the router, entity resolution, prompt, SQL guard, read-only role, repair, number check and response go
through the production code. Needs a built mart: ``PLUJKA_RUN_MART_TESTS=1`` (``make test-mart``); CI runs
it after building the mart.
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from typing import Any
from unittest.mock import patch

from scripts import fake_openai

_RUN = os.environ.get("PLUJKA_RUN_MART_TESTS", "").strip().lower() in ("1", "true", "yes")

TURNOUT_2023 = "SELECT year, turnout_pct FROM mart.v_turnout WHERE election = 'sejm' AND level = 'country' AND year = 2023 LIMIT 5"


class FakeOpenAI:
    """Replies of the fake model: ``replies`` for SQL calls, ``answers`` for the answer text; every chat
    request body is kept in ``requests``."""

    replies: list[dict] = []
    answers: list[str] = []
    requests: list[dict] = []

    @staticmethod
    def reply(body: dict) -> str:
        FakeOpenAI.requests.append(body)
        system = body["messages"][0]["content"]
        if system.startswith("You write SQL") and "ambiguous between these options" not in system:
            return json.dumps(FakeOpenAI.replies.pop(0))
        if system.startswith("You write SQL"):
            return json.dumps({"interpretations": []})
        return json.dumps({"answer": FakeOpenAI.answers.pop(0) if FakeOpenAI.answers else ""})


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_MART_TESTS=1 (built mart) to run end-to-end tests")
class TestAskEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from api.main import app
        from api.services.mart.safe_sql import run_readonly

        if not run_readonly("SELECT 1 FROM mart.elections WHERE election = 'sejm' AND year IN (2019, 2023) HAVING count(*) = 2"):
            raise AssertionError("the mart has no Sejm 2019 and 2023; build it first")
        # No OpenSearch: examples come from the curated file and the question log is skipped.
        for name in ("opensearch", "api.services.opensearch_store"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        cls.server, cls.base_url = fake_openai.start(FakeOpenAI.reply)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        from api.services import answer, config, dynamic_sql, embeddings

        FakeOpenAI.replies, FakeOpenAI.answers, FakeOpenAI.requests = [], [], []
        dynamic_sql.SQL_CACHE._data.clear()
        dynamic_sql.SEMANTIC_CACHE._data.clear()
        self.patches = [
            patch.object(config, "LLM_PROVIDER", "openai"),
            patch.object(config, "OPENAI_BASE_URL", self.base_url),
            patch.object(config, "OPENAI_API_KEY", "sk-fake"),
            patch.object(dynamic_sql, "OPENAI_API_KEY", "sk-fake"),
            patch.object(answer, "OPENAI_API_KEY", "sk-fake"),
            patch.object(embeddings, "OPENAI_API_KEY", "sk-fake"),
            patch.object(embeddings, "_llm_down_until", 0.0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()

    def ask(self, question: str, **extra: Any) -> dict:
        response = self.client.post("/ask", json={"question": question, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def sql_requests(self) -> list[dict]:
        return [r for r in FakeOpenAI.requests if r["messages"][0]["content"].startswith("You write SQL")]

    def test_answer_from_the_mart_with_a_checked_text_and_a_follow_up(self) -> None:
        from api.services import config

        FakeOpenAI.replies = [{"plan": "Sejm 2023 turnout, country level", "sql": TURNOUT_2023}]
        FakeOpenAI.answers = ["Frekwencja w wyborach do Sejmu w 2023 roku wyniosła 74,38%."]
        body = self.ask("Jaka była frekwencja w wyborach do Sejmu w 2023?")
        self.assertEqual(body["intent"], "dynamic_sql")
        self.assertEqual(body["result"], [{"year": 2023, "turnout_pct": 74.38}])  # PKW: 74.38%, a JSON number
        self.assertEqual(body["answer"]["source"], "llm")
        self.assertEqual(body["answer"]["text"], "Frekwencja w wyborach do Sejmu w 2023 roku wyniosła 74,38%.")
        self.assertEqual(body["answer"]["confidence"]["level"], "high")
        self.assertEqual(body["params"]["_dynamic_sql_meta"]["status"], "first_pass")
        [request] = self.sql_requests()
        self.assertEqual(request["model"], config.OPENAI_CHAT_MODEL)
        self.assertIn("v_turnout", request["messages"][0]["content"])  # the turnout view was described
        self.assertEqual(request["messages"][1]["content"], "Jaka była frekwencja w wyborach do Sejmu w 2023?")

        # The follow-up carries the previous turn into the prompt.
        FakeOpenAI.replies = [{"sql": TURNOUT_2023.replace("year = 2023", "year = 2019")}]
        FakeOpenAI.answers = ["W 2019 roku frekwencja wyniosła 61,74%."]
        follow = self.ask("A w 2019?", previous={"question": body["question"], "sql": body["sql"]})
        self.assertEqual(follow["result"], [{"year": 2019, "turnout_pct": 61.74}])
        self.assertIn("Previous question: \"Jaka była frekwencja", self.sql_requests()[-1]["messages"][0]["content"])

    def test_invented_number_falls_back_to_the_summary(self) -> None:
        FakeOpenAI.replies = [{"sql": TURNOUT_2023}]
        FakeOpenAI.answers = ["Frekwencja wyniosła 80,1%, o 6 punktów więcej niż w 2019."]
        body = self.ask("Frekwencja w wyborach do Sejmu 2023")
        self.assertEqual(body["answer"]["source"], "fallback")
        self.assertIn("74,38", body["answer"]["text"])
        self.assertNotIn("80,1", body["answer"]["text"])
        self.assertEqual(body["answer"]["confidence"],
                         {"level": "medium", "reasons": ["first_pass", "text_rejected"]})

    def test_failed_query_is_repaired_by_the_repair_model(self) -> None:
        from api.services import config

        FakeOpenAI.replies = [{"sql": TURNOUT_2023.replace("turnout_pct", "turnout_percent")}, {"sql": TURNOUT_2023}]
        body = self.ask("Frekwencja w wyborach do Sejmu 2023 w kraju")
        self.assertEqual(body["result"], [{"year": 2023, "turnout_pct": 74.38}])
        self.assertEqual(body["params"]["_dynamic_sql_meta"]["status"], "repaired")
        self.assertEqual(body["answer"]["confidence"]["level"], "medium")
        first, repair = self.sql_requests()
        self.assertEqual(repair["model"], config.OPENAI_REPAIR_MODEL)
        self.assertIn("The previous query failed", repair["messages"][0]["content"])
        self.assertIn("turnout_percent", repair["messages"][0]["content"])

    def test_unsafe_query_never_reaches_the_database(self) -> None:
        from api.services.mart.safe_sql import run_readonly

        before = run_readonly("SELECT count(*) AS n FROM mart.persons")[0]["n"]
        for sql in ("DELETE FROM mart.persons", "SELECT usename FROM pg_catalog.pg_user LIMIT 5"):
            with self.subTest(sql=sql):
                # Presidential questions never fall back to the Sejm templates.
                FakeOpenAI.replies = [{"sql": sql}]
                body = self.ask("Frekwencja w wyborach prezydenta w 2020")
                self.assertEqual(body["result"], [])
                self.assertEqual(body["params"]["_dynamic_sql_meta"]["status"], "rejected")
        self.assertEqual(run_readonly("SELECT count(*) AS n FROM mart.persons")[0]["n"], before)


if __name__ == "__main__":
    unittest.main()
