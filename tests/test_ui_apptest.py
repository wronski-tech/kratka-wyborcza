"""The Streamlit UI run headless by ``streamlit.testing`` (AppTest), with the API replaced by canned responses.

Covers what a user does on the main page: ask, read the answer with its checks, pick a clarification
option, open "Przeglądaj wyniki" in the emergency mode, see the limit messages, and the English labels.
Needs ``streamlit_app/requirements.txt`` (CI's unit job installs it); skipped without Streamlit.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # the UI's requirements are not installed
    AppTest = None

APP = Path(__file__).resolve().parents[1] / "streamlit_app" / "app.py"
QUESTION = "Jaka była frekwencja w wyborach do Sejmu w 2023?"
ANSWER = {
    "question": QUESTION, "intent": "dynamic_sql", "sql": "SELECT year, turnout_pct FROM mart.v_turnout", "params": {},
    "result": [{"year": 2023, "turnout_pct": 74.38}],
    "answer": {"text": "Frekwencja wyniosła 74,38%.", "source": "llm", "caveats": [],
               "confidence": {"level": "medium", "reasons": ["repaired"]}},
}


def _response(status: int, body: Any) -> MagicMock:
    import requests

    response = MagicMock(status_code=status, ok=status < 400, text=str(body), headers={"content-type": "application/json"})
    response.json.return_value = body
    response.raise_for_status.side_effect = requests.HTTPError(response=response) if status >= 400 else None
    return response


@unittest.skipIf(AppTest is None, "streamlit is not installed (pip install -r streamlit_app/requirements.txt)")
class TestMainPage(unittest.TestCase):
    def setUp(self) -> None:
        self.asked: list[dict] = []
        self.replies: list[tuple[int, Any]] = []
        get = lambda url, **kw: _response(200, {"status": "ok", "data_ready": True} if url.endswith("/health") else {})  # noqa: E731
        self.patches = [patch("requests.get", get), patch("requests.post", self._post)]
        for p in self.patches:
            p.start()
        sys.path.insert(0, str(APP.parent))  # streamlit run puts the script's directory on the path
        self.at = AppTest.from_file(str(APP), default_timeout=60)

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        sys.path.remove(str(APP.parent))

    def _post(self, url: str, json: dict | None = None, headers: dict | None = None, **kw: Any) -> MagicMock:
        self.asked.append({"url": url, "json": json, "headers": headers or {}})
        return _response(*self.replies.pop(0))

    def ask(self, question: str = QUESTION) -> None:
        self.at.text_input(key="kratka_wyborcza_question_input").input(question)
        next(b for b in self.at.button if b.label in ("Wyślij zapytanie", "Ask")).click().run()
        self.assertFalse(self.at.exception)

    def captions(self) -> list[str]:
        return [c.value for c in self.at.caption]

    def test_answer_with_its_checks_and_one_session_per_browser(self) -> None:
        self.at.run()
        self.replies = [(200, ANSWER), (200, ANSWER)]
        self.ask()
        self.assertIn("**Frekwencja wyniosła 74,38%.**", [m.value for m in self.at.markdown])
        self.assertIn("✓ Każda liczba w tej odpowiedzi występuje w wyniku zapytania SQL.", self.captions())
        self.assertIn("🟡 Pewność zapytania: **średnia** — pierwsza wersja zapytania nie zadziałała i została poprawiona",
                      self.captions())
        self.assertEqual(self.asked[0]["json"], {"question": QUESTION, "style": "journalistic"})
        self.ask("Frekwencja w 2019")
        first, second = (a["headers"]["X-Session-Id"] for a in self.asked)
        self.assertEqual(first, second)  # the session limit counts one browser session
        self.assertEqual(len(first), 32)

    def test_clarification_option_re_asks_with_the_chosen_entity(self) -> None:
        self.at.run()
        clarification = {**ANSWER, "intent": "clarification", "result": [], "answer": None, "clarification": {
            "prompt": "Którą gminę masz na myśli?", "kind": "unit",
            "options": [{"label": "Biała (opolskie)", "kind": "unit", "ref": "160201"},
                        {"label": "Biała Podlaska", "kind": "unit", "ref": "066101"}]}}
        self.replies = [(200, clarification), (200, ANSWER)]
        self.ask("Frekwencja w Białej")
        self.assertIn("Którą gminę masz na myśli?", [i.value for i in self.at.info])
        self.at.button(key="clarify_1_066101").click().run()
        self.assertEqual(self.asked[1]["json"]["entity_refs"], [{"kind": "unit", "ref": "066101"}])
        self.assertEqual(self.asked[1]["json"]["question"], "Frekwencja w Białej")
        self.assertIn("**Frekwencja wyniosła 74,38%.**", [m.value for m in self.at.markdown])

    def test_emergency_mode_opens_the_matching_browse_view(self) -> None:
        self.at.run()
        browse = {"wybory": "prezydent", "rok": 2020, "tura": 2, "pokaz": "Frekwencja", "poziom": "gmina"}
        self.replies = [(200, {**ANSWER, "intent": "llm_unavailable", "result": [], "sql": "",
                               "answer": {"text": "Model jest niedostępny.", "source": "browse", "browse": browse}})]
        self.ask("Frekwencja w gminach w II turze 2020")
        self.assertIn("Model jest niedostępny.", [w.value for w in self.at.warning])
        next(b for b in self.at.button if b.label.startswith("📊 Otwórz „Przeglądaj wyniki”")).click().run()
        self.assertEqual({k: self.at.query_params[k] for k in browse}, {k: [str(v)] for k, v in browse.items()})

    def test_limits_are_explained(self) -> None:
        self.at.run()
        self.replies = [(429, {"detail": "Session question limit (100) reached; try again tomorrow."}),
                        (429, {"detail": "Too many questions; try again in a minute."}),
                        (400, {"detail": "Question is too long: at most 500 characters."})]
        expected = ["Wykorzystano limit pytań w tej sesji. Spróbuj ponownie jutro albo skorzystaj z „Przeglądaj wyniki”.",
                    "Za dużo pytań w krótkim czasie. Spróbuj ponownie za minutę.",
                    "Pytanie jest za długie albo puste. (max 500)"]
        for text in expected:
            with self.subTest(text=text):
                self.ask()
                self.assertIn(text, [w.value for w in self.at.warning])
        self.assertEqual(self.at.text_input(key="kratka_wyborcza_question_input").max_chars, 500)

    def test_english_labels(self) -> None:
        self.at.query_params["lang"] = "en"
        self.at.run()
        self.assertIn("Your question (in Polish)", [t.label for t in self.at.text_input])
        self.replies = [(200, ANSWER)]
        self.ask()
        self.assertIn("🟡 Query confidence: **medium** — the first query failed and was repaired", self.captions())


if __name__ == "__main__":
    unittest.main()
