"""Pydantic response models for FastAPI (no server startup required)."""

from __future__ import annotations

import io
import unittest
import zipfile
from pathlib import Path
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import openai

from fastapi.testclient import TestClient

from api.services import config, report_pdf, speech

from api.main import (
    AskResponse,
    FeedbackOkResponse,
    HealthResponse,
    KbwCatalogSummaryResponse,
    QuestionHintsResponse,
    app,
)


class TestApiResponseModels(unittest.TestCase):
    def test_health_response_minimal(self) -> None:
        h = HealthResponse.model_validate({"status": "ok", "data_ready": False})
        self.assertIsNone(h.kbw_stats)

    def test_health_response_with_stats(self) -> None:
        h = HealthResponse.model_validate(
            {"status": "ok", "data_ready": True, "kbw_stats": {"kbw_facts": 42}}
        )
        self.assertEqual(h.kbw_stats["kbw_facts"], 42)

    def test_ask_response_with_geo_meta(self) -> None:
        r = AskResponse.model_validate(
            {
                "question": "q",
                "intent": "kbw_candidate_geo_votes_detail",
                "entity": "Kowalski",
                "year": 2099,
                "years": [],
                "sql": "SELECT 1",
                "params": {},
                "result": [{"votes": 1}],
                "candidate_geo_source": "kbw_facts",
            }
        )
        self.assertEqual(r.candidate_geo_source, "kbw_facts")

    def test_ask_response_mandate_meta(self) -> None:
        r = AskResponse.model_validate(
            {
                "question": "q",
                "intent": "kbw_sejm_mandate_vote_extremes",
                "entity": None,
                "year": 2023,
                "years": [],
                "sql": "SELECT 1",
                "params": {},
                "result": [],
                "mandate_extremes_source": "kbw_fallback",
            }
        )
        self.assertEqual(r.mandate_extremes_source, "kbw_fallback")

    def test_feedback_ok(self) -> None:
        self.assertTrue(FeedbackOkResponse().ok)

    def test_question_hints_empty(self) -> None:
        h = QuestionHintsResponse(text_hits=[], semantic_hits=[])
        self.assertEqual(h.text_hits, [])

    def test_catalog_summary_shape(self) -> None:
        s = KbwCatalogSummaryResponse(
            total_files=0, by_year={}, by_file_kind={}
        )
        self.assertEqual(s.total_files, 0)


class TestAskEndpoint(unittest.TestCase):
    """``POST /ask`` with the router stubbed (no lifespan, no database)."""

    ROUTED = {
        "question": "q", "intent": "dynamic_sql", "sql": "SELECT 1", "params": {},
        "result": [{"party": "PiS", "pct": Decimal("34.27"), "votes": Decimal("7640854"), "year": 2023}],
    }

    def setUp(self) -> None:
        self.patches = [
            patch("api.main.router.route_question", return_value=dict(self.ROUTED)),
            patch("api.main.answer.compose_answer", return_value={"text": "t", "source": "fallback"}),
            patch("api.main.opensearch_store.log_question"),
            patch("api.main.dynamic_sql.loaded_years", return_value={"sejm": [2019, 2023]}),
            patch("api.main.db.save_view", return_value="0123456789ab"),
        ]
        self.route = self.patches[0].start()
        for p in self.patches[1:]:
            p.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_numbers_are_json_numbers_and_previous_turn_is_passed(self) -> None:
        previous = {"question": "Frekwencja w Krakowie w 2023?", "sql": "SELECT 2"}
        response = self.client.post("/ask", json={"question": "A w 2019?", "previous": previous})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], [{"party": "PiS", "pct": 34.27, "votes": 7640854, "year": 2023}])
        self.assertEqual(self.route.call_args.kwargs["previous"], {**previous, "entity_refs": None})

    def test_answers_are_saved_as_views_that_rerun_through_the_guard(self) -> None:
        previous = {"question": "Frekwencja w Krakowie w 2023?", "sql": "SELECT 2"}
        with patch("api.main.db.save_view", return_value="0123456789ab") as save:
            body = self.client.post("/ask", json={"question": "A w 2019?", "previous": previous}).json()
            self.assertEqual(body["view_id"], "0123456789ab")
            save.assert_called_once_with("Frekwencja w Krakowie w 2023? → A w 2019?", "SELECT 1")
            self.route.return_value = {**self.ROUTED, "result": []}
            self.assertIsNone(self.client.post("/ask", json={"question": "q"}).json()["view_id"])
            self.route.return_value = {**self.ROUTED, "intent": "kbw_turnout"}
            self.assertIsNone(self.client.post("/ask", json={"question": "q"}).json()["view_id"])
            self.assertEqual(save.call_count, 1)
        stored = {"question": "Wynik PiS?", "sql": "SELECT party, pct FROM mart.v_party_results WHERE year = 2023"}
        with patch("api.main.db.saved_view", return_value=stored), \
                patch("api.main.safe_sql.run_readonly", return_value=[{"party": "PiS", "pct": Decimal("35.38")}]) as run:
            view = self.client.get("/views/0123456789ab").json()
            self.assertEqual((view["question"], view["rows"]), ("Wynik PiS?", [{"party": "PiS", "pct": 35.38}]))
            self.assertIn("LIMIT", run.call_args.args[0])  # validated (the guard adds a row limit), not the raw text
            stored["sql"] = "SELECT * FROM public.saved_views"
            self.assertEqual(self.client.get("/views/0123456789ab").status_code, 410)
            self.assertEqual(run.call_count, 1)
        with patch("api.main.db.saved_view", return_value=None):
            self.assertEqual(self.client.get("/views/0123456789ab").status_code, 404)
        self.assertEqual(self.client.get("/views/DROP").status_code, 422)

    def test_dynamic_answers_suggest_follow_ups(self) -> None:
        self.route.return_value = {**self.ROUTED, "sql": "SELECT party, pct FROM mart.v_party_results WHERE election = 'sejm' AND year = 2023 AND level = 'country'"}
        answer = self.client.post("/ask", json={"question": "Wynik PiS w 2023?"}).json()["answer"]
        self.assertEqual(answer["follow_ups"], ["A w 2019?", "A w podziale na województwa?", "Jak to się zmieniło w porównaniu z 2019?"])

    def test_transcribe_returns_text_only(self) -> None:
        wav = b"RIFF....WAVEfmt "
        post = lambda body: self.client.post("/transcribe", content=body, headers={"Content-Type": "audio/wav"})  # noqa: E731
        with patch("api.main.config.OPENAI_API_KEY", ""):
            self.assertEqual(post(wav).status_code, 503)
        with patch("api.main.config.OPENAI_API_KEY", "k"), patch("api.main.config.TRANSCRIBE_MAX_BYTES", 32), \
                patch("api.main.speech.transcribe", return_value="Frekwencja w Krakowie w 2023?") as speech:
            response = post(wav)
            self.assertEqual(response.json(), {"text": "Frekwencja w Krakowie w 2023?"})
            self.assertEqual(speech.call_args.args, (wav, "audio/wav"))
            self.assertEqual(post(b"").status_code, 400)
            self.assertEqual(post(b"x" * 33).status_code, 413)
            self.route.assert_not_called()  # the transcript is not asked: the client shows it first
        with patch("api.main.config.OPENAI_API_KEY", "k"), \
                patch("api.main.speech.transcribe", side_effect=openai.APIConnectionError(request=MagicMock())):
            self.assertEqual(post(wav).status_code, 502)

    def test_speech_asks_the_transcription_model(self) -> None:
        client = MagicMock()
        client.audio.transcriptions.create.return_value = MagicMock(text="  Ile mandatów\nmiał PiS?  ")
        with patch("api.services.speech.OpenAI", return_value=client):
            self.assertEqual(speech.transcribe(b"wav", "audio/wav"), "Ile mandatów miał PiS?")
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["file"]), (config.OPENAI_TRANSCRIBE_MODEL, ("question.wav", b"wav", "audio/wav")))
        self.assertIn("Konfederacja", kwargs["prompt"])

    def test_empty_result_reasons(self) -> None:
        sql = "SELECT pct FROM mart.v_party_results WHERE election = 'sejm' AND year = 2021"
        failed = {**self.ROUTED, "sql": sql, "result": [], "params": {"_dynamic_sql_meta": {"status": "failed"}}}
        with patch("api.main.answer.compose_answer", side_effect=lambda *a, **k: {"text": "t", "source": "fallback"}):
            self.route.return_value = {**self.ROUTED, "sql": sql, "result": []}
            answer = self.client.post("/ask", json={"question": "Wynik PiS w 2021?"}).json()["answer"]
            self.assertEqual(answer["empty_reasons"], ["W bazie nie ma wyborów: Sejm 2021. Wczytane lata: 2019, 2023."])
            self.route.return_value = failed
            self.assertNotIn("empty_reasons", self.client.post("/ask", json={"question": "q"}).json()["answer"])

    def test_answer_style_is_passed_and_checked(self) -> None:
        self.route.side_effect = lambda *a, **k: dict(self.ROUTED)
        with patch("api.main.answer.compose_answer", return_value={"text": "t", "source": "fallback"}) as compose:
            self.client.post("/ask", json={"question": "q", "style": "technical"})
            self.assertEqual(compose.call_args.kwargs["style"], "technical")
            self.client.post("/ask", json={"question": "q"})
            self.assertEqual(compose.call_args.kwargs["style"], "journalistic")
        self.assertEqual(self.client.post("/ask", json={"question": "q", "style": "poetic"}).status_code, 422)

    def test_api_keys_when_configured(self) -> None:
        with patch("api.main.config.API_KEYS", frozenset({"secret-1"})):
            self.assertEqual(self.client.post("/ask", json={"question": "q"}).status_code, 401)
            bad = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "nope"})
            self.assertEqual(bad.status_code, 401)
            good = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "secret-1"})
            self.assertEqual(good.status_code, 200)
            self.assertEqual(self.client.post("/feedback", json={"rating": "thumbs_up", "question": "q"}).status_code, 401)
        self.assertEqual(self.client.post("/ask", json={"question": "q"}).status_code, 200)

    def test_daily_token_budget_per_key(self) -> None:
        from api.services.rate_limit import TokenBudget

        routed = {**self.ROUTED, "params": {"_dynamic_sql_meta": {"tokens": {"prompt_tokens": 60, "completion_tokens": 50}}}}
        self.route.return_value = routed
        with patch("api.main.config.API_KEYS", frozenset({"k1", "k2"})), patch("api.main.token_budget", TokenBudget(100)):
            first = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k1"})
            second = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k1"})
            other = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k2"})
        # The first question may use the budget up (110 >= 100); the next one from that key is refused.
        self.assertEqual((first.status_code, second.status_code, other.status_code), (200, 429, 200))
        self.assertIn("token", second.json()["detail"])


class TestAgentEndpoints(unittest.TestCase):
    """``POST /mcp`` and ``POST /query``: agent SQL meets the same guard before the read-only role."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def rpc(self, method: str, params: dict | None = None, **headers: str) -> dict:
        return self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}},
                                headers=headers).json()

    def test_protocol_handshake(self) -> None:
        init = self.rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual((init["protocolVersion"], init["serverInfo"]["name"]), ("2025-03-26", "plujka"))
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual({t["name"] for t in self.rpc("tools/list")["result"]["tools"]}, {"ask", "schema", "query"})
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)
        notification = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((notification.status_code, notification.content), (202, b""))

    def test_queries_pass_the_guard_before_the_database(self) -> None:
        with patch("api.main.dynamic_sql.check_parts_cost"), \
                patch("api.main.safe_sql.run_readonly", return_value=[{"year": 2023, "turnout_pct": Decimal("74.38")}]) as run:
            ok = self.rpc("tools/call", {"name": "query", "arguments": {
                "sql": "SELECT year, turnout_pct FROM mart.v_turnout WHERE election = 'sejm' AND level = 'country'"}})["result"]
            self.assertFalse(ok["isError"])
            self.assertEqual(ok["structuredContent"]["rows"], [{"year": 2023, "turnout_pct": 74.38}])
            self.assertIn("LIMIT", run.call_args.args[0])
            for sql in ("DELETE FROM mart.elections", "SELECT * FROM public.saved_views", "SELECT pg_sleep(30)"):
                bad = self.rpc("tools/call", {"name": "query", "arguments": {"sql": sql}})["result"]
                self.assertTrue(bad["isError"], sql)
                self.assertTrue(bad["content"][0]["text"].startswith("Query rejected"), sql)
            rest = self.client.post("/query", json={"sql": "UPDATE mart.elections SET year = 1"})
            self.assertEqual(rest.status_code, 400)
            self.assertEqual(run.call_count, 1)

    def test_api_keys_when_configured(self) -> None:
        with patch("api.main.config.API_KEYS", frozenset({"secret-1"})):
            self.assertEqual(self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code, 401)
            self.assertEqual(self.client.post("/query", json={"sql": "SELECT 1"}).status_code, 401)
            self.assertEqual(self.rpc("ping", **{"X-API-Key": "secret-1"})["result"], {})


class TestPersonEndpoints(unittest.TestCase):
    """``GET /persons`` and ``GET /persons/{id}`` with the read-only mart query stubbed."""

    PERSON = {"person_id": 7, "full_name": "Jan KOWALSKI", "last_name": "KOWALSKI", "first_names": "Jan",
              "gender": "Mężczyzna", "match_rule": "name+gender", "candidacies": Decimal("2")}
    CANDIDACIES = [
        {"year": 2019, "election_date": date(2019, 10, 13), "chamber": "sejm", "round": 1, "district_no": 4,
         "area": None, "committee_name": "KW A", "list_no": 1, "list_position": 3, "votes": Decimal("12345"),
         "elected": False, "age": None},
        {"year": 2024, "election_date": date(2024, 4, 7), "chamber": "wbp", "round": 2, "district_no": None,
         "area": "gm. Gdów", "committee_name": "KWW B", "list_no": None, "list_position": None, "votes": 4374,
         "elected": True, "age": 64},
    ]

    def setUp(self) -> None:
        self.run = patch("api.main.safe_sql.run_readonly", side_effect=self._rows).start()
        self.addCleanup(patch.stopall)
        self.client = TestClient(app)

    def _rows(self, sql: str, params: dict) -> list[dict]:
        if "FROM mart.persons WHERE" in sql:
            return [dict(self.PERSON)] if params["person_id"] == 7 else []
        if "v_person_history" in sql:
            return [dict(row) for row in self.CANDIDACIES]
        hit = {"person_id": 7, "full_name": "Jan KOWALSKI", "gender": "Mężczyzna", "match_rule": "name+gender",
               "candidacies": 2, "detail": "sejm 2019 (district 4)", "years": [2019, 2024]}
        return [{**hit, "person_id": i} for i in range(1, 100)]

    def test_person_timeline_numbers_and_dates(self) -> None:
        response = self.client.get("/persons/7")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["person"]["candidacies"], 2)
        self.assertEqual((body["person"]["first_year"], body["person"]["last_year"]), (2019, 2024))
        self.assertEqual(body["person"]["elected_count"], 1)
        first = body["candidacies"][0]
        self.assertIs(type(first["votes"]), int)
        self.assertEqual((first["votes"], first["election_date"]), (12345, "2019-10-13"))
        self.assertEqual(body["candidacies"][1]["age"], 64)
        # Fixed queries are parameterized, never formatted with the id.
        self.assertEqual(self.run.call_args.kwargs["params"], {"person_id": 7})
        self.assertNotIn("7", self.run.call_args.args[0])

    def test_unknown_or_invalid_person(self) -> None:
        self.assertEqual(self.client.get("/persons/8").status_code, 404)
        self.assertEqual(self.client.get("/persons/abc").status_code, 422)
        self.assertEqual(self.client.get("/persons/0").status_code, 422)

    def test_search_limit_and_tokens(self) -> None:
        response = self.client.get("/persons", params={"name": "Jan Kowal", "limit": 5})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["persons"]), 5)
        self.assertEqual(self.run.call_args.kwargs["params"], {"tokens": ["Jan", "Kowal"], "limit": 5})
        self.assertEqual(len(self.client.get("/persons", params={"name": "Kowal"}).json()["persons"]), 20)
        self.assertEqual(self.client.get("/persons", params={"name": "Kowal", "limit": 51}).status_code, 422)
        self.assertEqual(self.client.get("/persons", params={"name": "K"}).status_code, 422)
        # LIKE wildcards and digits are not name words: no query at all.
        self.run.reset_mock()
        self.assertEqual(self.client.get("/persons", params={"name": "%_ 12"}).json(), {"persons": []})
        self.run.assert_not_called()

    def test_api_keys_and_database_errors(self) -> None:
        import psycopg

        with patch("api.main.config.API_KEYS", frozenset({"secret-1"})):
            self.assertEqual(self.client.get("/persons/7").status_code, 401)
            self.assertEqual(self.client.get("/persons", params={"name": "Kowal"}).status_code, 401)
            self.assertEqual(self.client.get("/persons/7", headers={"X-API-Key": "secret-1"}).status_code, 200)
        self.run.side_effect = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        self.assertEqual(self.client.get("/persons/7").status_code, 503)


if __name__ == "__main__":
    unittest.main()


class TestBrowseEndpoints(unittest.TestCase):
    """Browse endpoints (no LLM) with the read-only mart query stubbed: unit filters and response shape."""

    def setUp(self) -> None:
        self.calls: list[dict] = []
        patch("api.main.safe_sql.run_readonly", side_effect=self._rows).start()
        self.addCleanup(patch.stopall)
        self.client = TestClient(app)

    def _rows(self, sql: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        self.calls.append(params)
        if "FROM mart.v_turnout" in sql and params.get("teryt_gmina") == "999999":
            return []
        if "FROM mart.v_turnout" in sql:
            return [{"election": "sejm", "year": 2023, "round": 1, "unit_name": "gm. Gdów", "turnout_pct": Decimal("71.50")}]
        if "obj_description" in sql:
            return [{"relation": "v_turnout", "kind": "materialized view", "comment": "Turnout.", "column_name": c,
                     "column_type": "text", "column_comment": None} for c in ("election", "level")]
        if "max(loaded_at)" in sql:
            return [{"mart_loaded_at": None, "elections": 0, "latest_election_date": None}]
        if "v_gus_units" in sql:
            return [{"year": 2024, "name": "Gdów", "population": Decimal("21000")}]
        return [{"election": "sejm", "year": 2023, "round": 1, "name": "KO", "votes": Decimal("100"), "pct": Decimal("40.5")}]

    @unittest.skipUnless((Path(config.REPORT_FONT_DIR) / "DejaVuSans.ttf").exists(), "DejaVu fonts not installed")
    def test_unit_pdf_report(self) -> None:
        response = self.client.get("/pdf/units/121903")
        self.assertEqual((response.status_code, response.headers["content-type"]), (200, "application/pdf"))
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn('filename="plujka-121903.pdf"', response.headers["content-disposition"])
        self.assertEqual(self.client.get("/pdf/units/999999").status_code, 404)
        self.assertEqual(report_pdf.number(Decimal("7640753")), "7 640 753")
        self.assertEqual(report_pdf.number(39.0, 2), "39,00")

    def test_unit_level_from_key(self) -> None:
        for unit, level, column in [("121903", "gmina", "teryt_gmina"), ("1219", "powiat", "teryt_powiat"),
                                    ("małopolskie", "wojewodztwo", "wojewodztwo")]:
            self.calls.clear()
            card = self.client.get(f"/units/{unit}").json()
            self.assertEqual(card["level"], level)
            others = {"teryt_gmina", "teryt_powiat", "wojewodztwo"} - {column}
            self.assertEqual(self.calls[0][column], unit)
            self.assertTrue(all(self.calls[0][c] is None for c in others))
        self.assertEqual(card["name"], "Gdów")
        self.assertEqual(card["turnout"], [{"election": "sejm", "year": 2023, "round": 1, "turnout_pct": 71.5}])
        self.assertEqual(card["winners"][0]["votes"], 100)

    def test_data_package_zips_every_level_as_plain_csv(self) -> None:
        response = self.client.get("/packages/sejm/2023")
        self.assertEqual(response.headers["content-type"], "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(sorted(archive.namelist()), ["README.txt", "results.csv", "turnout.csv"])
        results = archive.read("results.csv").decode().splitlines()
        self.assertEqual(results[0], "election,year,round,name,votes,pct")
        self.assertEqual(results[1:], ["sejm,2023,1,KO,100,40.5"] * 4)  # country, voivodeship, powiat, gmina
        self.assertEqual(self.client.get("/packages/sejm/2019").status_code, 404)

    def test_sources_of_one_chamber_in_the_shared_directory(self) -> None:
        def names(election: str, year: int) -> set[str]:
            files = self.client.get("/sources", params={"election": election, "year": year, "limit": 1000}).json()
            return {f["path"].rsplit("/", 1)[-1] for f in files}
        sejm, senat = names("sejm", 2011), names("senat", 2011)
        self.assertIn("2011-kand-sejm.xls", sejm)
        self.assertNotIn("2011-kand-sen.zip", sejm)
        self.assertIn("2011-kand-sen.zip", senat)
        self.assertNotIn("2011-kand-sejm.xls", senat)
        self.assertIn("komitety.zip", sejm & senat)
        self.assertIn("sejmsenat2023", names("sejm", 2023))  # PKW bundle page

    def test_reports_cover_every_election_family(self) -> None:
        reports = self.client.get("/reports").json()
        self.assertEqual({r["family"] for r in reports} - {"inne"},
                         {"sejm", "senat", "prezydent", "pe", "referendum", "sejmik", "wbp", "rada"})
        report = self.client.get(f"/reports/{reports[0]['id']}").json()
        self.assertEqual(report["rows"][0]["votes"], 100)
        self.assertEqual(self.client.get("/reports/000000000000").status_code, 404)

    def test_data_version_reads_the_source_manifest(self) -> None:
        version = self.client.get("/data-version").json()
        self.assertGreater(version["source_files"], 0)
        self.assertEqual(len(version["manifest_sha256"]), 64)
        self.assertIsNotNone(version["sources_last_modified"])

    def test_data_dictionary_groups_columns_by_relation(self) -> None:
        relations = self.client.get("/data-dictionary").json()
        self.assertEqual([(r["relation"], [c["name"] for c in r["columns"]]) for r in relations], [("v_turnout", ["election", "level"])])
        self.assertIn("v_turnout", self.calls[-1]["relations"])

    def test_known_gaps_and_sources_read_the_shipped_files(self) -> None:
        gaps = self.client.get("/known-gaps").json()
        self.assertIn("1991", gaps["markdown"])
        self.assertNotIn("### Adding an election year", gaps["markdown"])
        self.assertTrue(gaps["caveats"])
        files = self.client.get("/sources", params={"limit": 3}).json()
        self.assertEqual(len(files), 3)
        self.assertGreaterEqual(files[0]["last_modified"], files[-1]["last_modified"])

    def test_unknown_unit_and_unsupported_election(self) -> None:
        self.assertEqual(self.client.get("/units/999999").status_code, 404)
        self.assertEqual(self.client.get("/results", params={"election": "senat", "year": 2023}).status_code, 422)
        self.assertEqual(self.client.get("/change", params={"election": "prezydent", "year": 2025}).status_code, 422)
        self.assertEqual(self.client.get("/candidates/stats", params={"chamber": "rada"}).status_code, 422)
        self.assertEqual(self.client.get("/rankings/persons", params={"by": "age"}).status_code, 422)
        self.assertEqual(self.client.get("/elections/rada/2024").status_code, 422)
