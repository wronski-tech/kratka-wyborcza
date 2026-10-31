"""Personal data out of user text before an LLM reads it (feedback triage, examples taken from feedback).

Replaces e-mail addresses, links, IP addresses, account and card numbers, PESEL numbers and phone numbers with
placeholders. Election data stays: years, dates, TERYT codes, vote counts written with spaces ("7 640 854") and
percentages do not match. Names are left alone: questions name candidates, who are public persons, and a name
cannot be told apart from a private one by its form.
"""

from __future__ import annotations

import re

# Order matters: longer digit runs (accounts, cards, PESEL) go before phone numbers. A digit run must not touch
# other digits or a decimal part ("35,38"); a comma or full stop after it is punctuation.
_PATTERNS = (
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[e-mail]"),
    (re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE), "[link]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # IBAN (PL and 26 digits, spaces allowed) or a bare 26-digit Polish account number.
    (re.compile(r"\b(?:[A-Z]{2}\s?)?\d{2}(?:\s?\d{4}){6}\b"), "[konto]"),
    (re.compile(r"\b\d{4}(?:[ -]?\d{4}){3}\b"), "[karta]"),
    (re.compile(r"(?<!\d)(?<!\d[.,])\d{11}(?!\d|[.,]\d)"), "[PESEL]"),
    # +48 / 0048 and nine digits, or nine digits as 3-3-3 / 2-3-2-2 groups or in one run.
    (re.compile(r"(?:\+|\b00)48[ -]?\d{3}[ -]?\d{3}[ -]?\d{3}\b"), "[telefon]"),
    (re.compile(r"(?<!\d)(?<!\d[.,])(?:\d{3}[ -]\d{3}[ -]\d{3}|\d{2}[ -]\d{3}[ -]\d{2}[ -]\d{2}|\d{9})(?!\d|[.,]\d|[ -]\d)"),
     "[telefon]"),
)


def anonymize(text: str) -> str:
    """``text`` with personal data replaced by placeholders such as ``[e-mail]`` and ``[telefon]``."""
    for pattern, placeholder in _PATTERNS:
        text = pattern.sub(placeholder, text)
    return text
