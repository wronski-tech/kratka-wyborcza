"""Property tests of ``mart.allocate_seats`` on PostgreSQL (``PLUJKA_RUN_DB_TESTS=1``; needs ``pip install hypothesis``).

Hypothesis draws vote lists and seat counts; each case runs the SQL function and checks it against an
exact reference written here with fractions, and against properties every allocation must have: all
seats are given out, no votes means no seat, a divisor method never takes a seat away when one is added
(house monotonicity; Hare-Niemeyer may, the Alabama paradox), Hare-Niemeyer stays within the quota,
and scaling all votes changes nothing.
"""

from __future__ import annotations

import math
import os
import unittest
from fractions import Fraction
from typing import Any

_RUN = os.environ.get("PLUJKA_RUN_DB_TESTS", "").strip().lower() in ("1", "true", "yes")

DIVISOR_METHODS = ("dhondt", "sainte_lague", "sainte_lague_modified", "sainte_lague_modified_whole")
METHODS = (*DIVISOR_METHODS, "hare_niemeyer")


def reference(votes: list[int], seats: int, method: str) -> list[int]:
    """Exact allocation: quotients as fractions; ties to more votes, then to the earlier list."""
    if method == "hare_niemeyer":
        total = sum(votes)
        quotas = [Fraction(seats * v, total) if total else Fraction(0) for v in votes]
        won = [math.floor(q) for q in quotas]
        order = sorted((i for i, v in enumerate(votes) if v > 0), key=lambda i: (-(quotas[i] - won[i]), -votes[i], i))
        for i in order[: seats - sum(won)]:
            won[i] += 1
        return won

    def divisor(k: int) -> Fraction:
        if method == "dhondt":
            return Fraction(k)
        if method != "sainte_lague" and k == 1:
            return Fraction(7, 5)
        return Fraction(2 * k - 1)

    quotients = []
    for i, v in enumerate(votes):
        for k in range(1, seats + 1):
            q = Fraction(v) / divisor(k)
            quotients.append((math.floor(q) if method == "sainte_lague_modified_whole" else q, v, i))
    won = [0] * len(votes)
    for _, _, i in sorted((q for q in quotients if q[1] > 0), key=lambda q: (-q[0], -q[1], q[2]))[:seats]:
        won[i] += 1
    return won


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestAllocateSeatsProperties(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        from api.services.config import DATABASE_URL
        from api.services.mart import schema

        cls.conn = psycopg.connect(DATABASE_URL, autocommit=True)
        schema.ensure_schema(cls.conn)  # CI runs these tests before building the mart

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def allocate(self, votes: list[int], seats: int, method: str) -> list[int]:
        return self.conn.execute("SELECT mart.allocate_seats(%s::numeric[], %s, %s)", (votes, seats, method)).fetchone()[0]

    def settings(self) -> Any:
        from hypothesis import settings

        return settings(max_examples=150, deadline=None, database=None)

    def strategies(self) -> Any:
        from hypothesis import strategies as st

        # Up to 10 lists with up to a million votes; small vote counts make ties likely.
        votes = st.lists(st.one_of(st.integers(0, 20), st.integers(0, 1_000_000)), min_size=1, max_size=10)
        return votes, st.integers(0, 40)

    def test_matches_the_exact_reference(self) -> None:
        from hypothesis import given
        from hypothesis import strategies as st

        votes, seats = self.strategies()

        @self.settings()
        @given(votes=votes, seats=seats, method=st.sampled_from(METHODS))
        def check(votes: list[int], seats: int, method: str) -> None:
            self.assertEqual(self.allocate(votes, seats, method), reference(votes, seats, method))

        check()

    def test_every_seat_is_given_and_no_votes_get_none(self) -> None:
        from hypothesis import given
        from hypothesis import strategies as st

        votes, seats = self.strategies()

        @self.settings()
        @given(votes=votes, seats=seats, method=st.sampled_from(METHODS))
        def check(votes: list[int], seats: int, method: str) -> None:
            won = self.allocate(votes, seats, method)
            self.assertEqual(len(won), len(votes))
            self.assertEqual(sum(won), seats if any(votes) else 0)
            self.assertTrue(all(w == 0 for w, v in zip(won, votes) if v == 0))

        check()

    def test_divisor_methods_never_take_a_seat_away(self) -> None:
        from hypothesis import given
        from hypothesis import strategies as st

        votes, seats = self.strategies()

        @self.settings()
        @given(votes=votes, seats=seats, method=st.sampled_from(DIVISOR_METHODS))
        def check(votes: list[int], seats: int, method: str) -> None:
            before, after = self.allocate(votes, seats, method), self.allocate(votes, seats + 1, method)
            self.assertTrue(all(a >= b for a, b in zip(after, before)), (before, after))

        check()

    def test_hare_niemeyer_stays_within_the_quota(self) -> None:
        from hypothesis import given

        votes, seats = self.strategies()

        @self.settings()
        @given(votes=votes, seats=seats)
        def check(votes: list[int], seats: int) -> None:
            total = sum(votes)
            for won, v in zip(self.allocate(votes, seats, "hare_niemeyer"), votes):
                quota = Fraction(seats * v, total) if total else 0
                self.assertTrue(math.floor(quota) <= won <= math.ceil(quota), (votes, seats))

        check()

    def test_scaling_the_votes_changes_nothing(self) -> None:
        from hypothesis import given
        from hypothesis import strategies as st

        votes, seats = self.strategies()

        # Not the whole-number variant: cutting quotients to integers depends on their size.
        @self.settings()
        @given(votes=votes, seats=seats, factor=st.integers(2, 1000),
               method=st.sampled_from(("dhondt", "sainte_lague", "sainte_lague_modified", "hare_niemeyer")))
        def check(votes: list[int], seats: int, factor: int, method: str) -> None:
            self.assertEqual(self.allocate([v * factor for v in votes], seats, method), self.allocate(votes, seats, method))

        check()


if __name__ == "__main__":
    unittest.main()
