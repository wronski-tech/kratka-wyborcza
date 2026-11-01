"""Entity resolution on the real lexicon of a built mart (``PLUJKA_RUN_MART_TESTS=1``, ``make test-mart``).

Unit tests use small hand-made lexicons; this runs the questions against ``mart.entity_lexicon`` as built,
so a rebuild, a new alias or a new election that makes a question match the wrong place, person or
committee fails here. Expected codes are official TERYT codes, not read from the lexicon.
"""

from __future__ import annotations

import os
import unittest

_RUN = os.environ.get("PLUJKA_RUN_MART_TESTS", "").strip().lower() in ("1", "true", "yes")

# question -> (refs that must be matched, refs that must not, whether it asks to clarify).
# Refs are "kind:ref" (TERYT codes, committee names) or "kind:name" for persons, whose ids the build assigns;
# a kind alone ("person") means no match of that kind.
CASES: dict[str, tuple[set[str], set[str], bool]] = {
    # Declined town names.
    "Frekwencja w Białymstoku w 2023": ({"gmina:206101"}, {"person"}, False),
    "Frekwencja w Jeleniej Górze": ({"gmina:026101"}, {"person"}, False),
    # A surname matching a word of the place name adds no persons.
    "Wynik PiS w Dąbrowie Górniczej w 2023": ({"gmina:246501", "committee:KOMITET WYBORCZY PRAWO I SPRAWIEDLIWOŚĆ"}, {"person"}, False),
    # A person named in full beats places like the given name or surname (Przemyśl, Czarne).
    "Ile głosów dostał Przemysław Czarnek w 2023?": ({"person:Przemysław CZARNEK"}, {"gmina", "powiat"}, False),
    # The closer place name beats the look-alike: Szczecin, not Szczecinek; Bielsko-Biała, not Biała.
    "Frekwencja w Szczecinie w 2019": ({"gmina:326201"}, {"gmina:321501", "gmina:321506"}, False),
    "Frekwencja w Bielsku-Białej": ({"gmina:246101"}, {"gmina:101701", "gmina:161001"}, False),
    # „między”, „różnica” and „mieli” are words, not the gminas Miedzna/Miedźna, Mielec or the surname Różniak.
    "Różnica między PiS a KO w 2023": ({"committee:KOALICYJNY KOMITET WYBORCZY KOALICJA OBYWATELSKA"}, {"gmina", "person"}, False),
    "Ile mandatów mieli posłowie PiS w 2019?": (set(), {"gmina", "person"}, False),
    # „w mieście X” picks the town gmina instead of asking.
    "Frekwencja w mieście Krasnystaw w 2023": ({"gmina:060601"}, {"gmina:060605"}, False),
    # A capitalized voivodeship is not dropped for a look-alike surname (PODLASKA).
    "Wynik PiS w województwie Podlaskie w 2023": ({"wojewodztwo:podlaskie"}, {"person"}, False),
    # English names come from entity_aliases.csv.
    "Turnout in Warsaw in 2023": ({"powiat:1465"}, {"person"}, False),
    # Town and rural gmina of one name are a real ambiguity.
    "Kto wygrał w Mielcu w 2023?": ({"gmina:181101", "gmina:181105"}, set(), True),
}


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_MART_TESTS=1 (built mart) to run entity tests")
class TestEntitiesOnTheLexicon(unittest.TestCase):
    def test_tricky_questions(self) -> None:
        from api.services import dynamic_sql

        for question, (wanted, unwanted, clarify) in CASES.items():
            with self.subTest(question=question):
                matches, clarification = dynamic_sql.resolve_question_entities(question, None, None)
                found = {f"{m.kind}:{m.ref}" for m in matches} | {f"{m.kind}:{m.name}" for m in matches} | {m.kind for m in matches}
                self.assertEqual(wanted - found, set(), "not matched")
                self.assertEqual(unwanted & found, set(), "matched")
                self.assertEqual(bool(clarification), clarify, clarification and clarification["options"])

    def test_evaluation_questions_clarify_only_when_expected(self) -> None:
        # run_eval.py --check only warns about these; here a new false clarification fails.
        import importlib.util
        from pathlib import Path

        from api.services import dynamic_sql

        spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py")
        run_eval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_eval)
        wrong = []
        for item in run_eval.load_items():
            if "definition" in item or item.get("expect") == "refuse":
                continue
            clarification = dynamic_sql.resolve_question_entities(item["question"], None, item.get("previous"))[1]
            if bool(clarification) != (item.get("expect") == "clarify"):
                wrong.append(f"{item['id']}: {item['question']}")
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
