"""Contract tests of the API endpoints on a built mart (``KRATKA_WYBORCZA_RUN_MART_TESTS=1``, ``make test-mart``).

Every operation in the OpenAPI schema has a case here or a reason in ``EXCLUDED``, so a new endpoint fails
until it gets one. A case runs the endpoint's fixed SQL on the real mart (unit tests stub the database)
and checks the fields its consumers (the UI, agents, the PDF and CSV exports) read: present, filled, and
JSON numbers where they are counts or percentages.
"""

from __future__ import annotations

import logging
import os
import re
import unittest
from typing import Any

_RUN = os.environ.get("KRATKA_WYBORCZA_RUN_MART_TESTS", "").strip().lower() in ("1", "true", "yes")

# (method, OpenAPI path) -> (URL, query parameters or JSON body, fields of the first item).
# ``{person}`` and ``{report}`` are filled in from the mart (Jarosław Wałęsa, the first report).
CASES: dict[tuple[str, str], tuple[str, dict[str, Any], list[str]]] = {
    ("get", "/health"): ("/health", {"details": "true"}, ["status", "data_ready", "mart_elections"]),
    ("get", "/persons"): ("/persons", {"name": "jarosław wałęsa"}, ["person_id", "full_name", "candidacies", "years"]),
    ("get", "/persons/{person_id}"): ("/persons/{person}", {}, ["person", "candidacies"]),
    ("get", "/persons/{person_id}/gminas"): ("/persons/{person}/gminas", {"election": "sejm", "year": 2023},
                                             ["teryt_gmina", "votes", "pct"]),
    ("get", "/elections"): ("/elections", {}, ["election", "year", "round", "election_date"]),
    ("get", "/elections/{election}/{year}"): ("/elections/sejm/2023", {}, ["turnout", "seats", "thresholds_pct"]),
    ("get", "/candidates/stats"): ("/candidates/stats", {"chamber": "sejm"}, ["year", "candidates", "women_pct", "elected"]),
    # Sejm candidates have an age in 1991-1997 only (docs/MART.md).
    ("get", "/candidates/ages"): ("/candidates/ages", {"chamber": "sejm", "year": 1997}, ["full_name", "age"]),
    ("get", "/mayors/terms"): ("/mayors/terms", {}, ["histogram", "top"]),
    ("get", "/mayors/gmina/{teryt_gmina}"): ("/mayors/gmina/126101", {}, ["year", "full_name", "pct"]),
    ("get", "/rankings/persons"): ("/rankings/persons", {}, ["person_id", "full_name", "candidacies", "votes"]),
    ("get", "/rankings/party-switches"): ("/rankings/party-switches", {}, ["from_family", "to_family", "persons"]),
    ("get", "/elected"): ("/elected", {"chamber": "sejm", "year": 2023, "district_no": 13},
                          ["district_no", "party", "full_name", "votes"]),
    ("get", "/data-dictionary"): ("/data-dictionary", {}, ["relation", "kind", "comment", "columns"]),
    ("get", "/known-gaps"): ("/known-gaps", {}, ["markdown", "caveats"]),
    ("get", "/sources"): ("/sources", {"election": "sejm", "year": 2023}, ["path", "url", "bytes"]),
    ("get", "/checks/{election}/{year}"): ("/checks/sejm/2023", {}, ["check_no", "label", "failing_rows"]),
    ("get", "/reports"): ("/reports", {}, ["id", "question", "family"]),
    ("get", "/reports/{report_id}"): ("/reports/{report}", {}, ["question", "sql", "rows"]),
    ("get", "/data-version"): ("/data-version", {}, ["mart_loaded_at", "elections", "manifest_sha256"]),
    ("get", "/turnout"): ("/turnout", {"election": "sejm", "year": 2023, "level": "wojewodztwo"},
                          ["wojewodztwo", "eligible_voters", "valid_ballots", "turnout_pct"]),
    ("get", "/results"): ("/results", {"election": "sejm", "year": 2023}, ["name", "votes", "pct"]),
    ("get", "/runoff"): ("/runoff", {"year": 2025}, ["name", "votes_round1", "pct_round1", "votes", "pct"]),
    ("get", "/wasted"): ("/wasted", {"year": 2023}, ["wojewodztwo", "valid_votes", "wasted_votes", "wasted_pct"]),
    ("get", "/change"): ("/change", {"election": "sejm", "year": 2023}, ["name", "prev_year", "votes", "prev_votes", "pct"]),
    ("get", "/districts"): ("/districts", {"election": "sejm", "year": 2023}, ["teryt_gmina", "district_no"]),
    ("get", "/precincts"): ("/precincts", {"election": "sejm", "year": 2023, "teryt_gmina": "126101"},
                            ["precinct_no", "address", "eligible_voters", "valid_votes", "turnout_pct"]),
    ("get", "/precincts/find"): ("/precincts/find", {"teryt_gmina": "126101", "street": "Floriańska"},
                                 ["precinct_no", "address", "boundary_description"]),
    ("get", "/units"): ("/units", {"name": "Kraków"}, ["level", "unit", "name"]),
    ("get", "/units/{unit}"): ("/units/126101", {}, ["level", "name", "turnout", "winners"]),
    ("get", "/seats/simulate"): ("/seats/simulate", {"year": 2023}, ["committee_name", "votes", "seats_official", "seats_simulated"]),
    ("get", "/packages/{election}/{year}"): ("/packages/sejm/2023", {}, []),
    ("get", "/pdf/units/{unit}"): ("/pdf/units/126101", {}, []),
    ("get", "/pdf/elections/{election}/{year}"): ("/pdf/elections/sejm/2023", {}, []),
    ("post", "/query"): ("/query", {"sql": "SELECT year, turnout_pct FROM mart.v_turnout WHERE election = 'sejm' AND level = 'country'"},
                         ["year", "turnout_pct"]),
}

# Operations covered elsewhere, with the reason.
EXCLUDED = {
    ("post", "/ask"): "end to end in tests/test_e2e_stubbed_llm.py",
    ("post", "/transcribe"): "needs the speech model; tests/test_api_contracts.py",
    ("post", "/mcp"): "protocol and guard in tests/test_api_contracts.py; its query tool is POST /query",
    ("post", "/feedback"): "writes the feedback file; tests/test_api_contracts.py",
    ("post", "/feedback/person"): "writes the feedback file",
    ("post", "/question-hints"): "OpenSearch question log, not the mart",
    ("get", "/kbw/catalog/summary"): "public.kbw_dane_files (loader), not the mart; tests/test_api_contracts.py",
    ("get", "/costs"): "public.token_usage, admin key; tests/test_api_contracts.py",
    ("get", "/views/{view_id}"): "public.saved_views; tests/test_api_contracts.py",
}

# Fields that are counts or percentages must arrive as JSON numbers (main._json_value), never strings.
_NUMERIC = re.compile(r"votes|pct|voters|ballots|seats|candidates|candidacies|persons|failing_rows|bytes|^age$")


def _first(body: Any) -> dict:
    """The first item of a list response, of ``rows`` / ``persons`` / ``candidacies``, or the object itself."""
    if isinstance(body, list):
        return body[0] if body else {}
    for key in ("rows", "persons"):
        if isinstance(body.get(key), list) and len(body) <= 2:
            return body[key][0] if body[key] else {}
    return body


@unittest.skipUnless(_RUN, "Set KRATKA_WYBORCZA_RUN_MART_TESTS=1 (built mart) to run contract tests")
class TestEndpointContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from api.main import app

        cls.app = app
        cls.client = TestClient(app)
        logging.getLogger("opensearch").setLevel(logging.ERROR)  # /health and the question log without OpenSearch
        persons = cls.client.get("/persons", params={"name": "jarosław wałęsa"}).json()["persons"]
        found = [p for p in persons if p["full_name"].startswith("Jarosław ") and 2023 in p["years"]]
        if not found:
            raise AssertionError("the mart has no Jarosław WAŁĘSA in 2023; build it first")
        cls.ids = {"person": found[0]["person_id"], "report": cls.client.get("/reports").json()[0]["id"]}

    def test_every_operation_has_a_case_or_a_reason(self) -> None:
        operations = {(method, path) for path, ops in self.app.openapi()["paths"].items() for method in ops}
        self.assertEqual(sorted(operations - CASES.keys() - EXCLUDED.keys()), [], "endpoints without a contract case")
        self.assertEqual(sorted((CASES.keys() | EXCLUDED.keys()) - operations), [], "cases of removed endpoints")

    def test_endpoints_return_the_fields_consumers_read(self) -> None:
        for (method, _), (url, params, fields) in CASES.items():
            url = url.format(**self.ids)
            with self.subTest(url=url):
                response = (self.client.post(url, json=params) if method == "post" else self.client.get(url, params=params))
                self.assertEqual(response.status_code, 200, response.text[:300])
                kind = response.headers["content-type"]
                if url.startswith("/pdf/"):
                    self.assertEqual((kind, response.content[:5]), ("application/pdf", b"%PDF-"))
                    continue
                if url.startswith("/packages/"):
                    self.assertEqual((kind, response.content[:2]), ("application/zip", b"PK"))
                    continue
                item = _first(response.json())
                self.assertTrue(item, "no rows on the built mart")
                for field in fields:
                    self.assertIn(field, item)
                    self.assertIsNotNone(item[field], field)
                    if _NUMERIC.search(field) and not isinstance(item[field], (dict, list)):
                        self.assertIsInstance(item[field], (int, float), field)

    def test_bad_input_is_a_client_error(self) -> None:
        expected = {
            "/elections/xyz/2023": 422,  # unknown election kind
            "/elections/sejm/2022": 404,  # not an election year
            "/results?election=sejm&year=2023&level=bogus": 422,
            "/precincts?election=sejm&year=2023&teryt_gmina=abc": 422,
            "/units/999999": 404,
            "/persons/abc": 422,
            "/reports/000000000000": 404,
        }
        for url, status in expected.items():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status)
        rejected = self.client.post("/query", json={"sql": "DELETE FROM mart.persons"})
        self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
