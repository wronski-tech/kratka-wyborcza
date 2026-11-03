"""PDF reports of a territorial unit or an election, built from the fixed-SQL cards of the API.

Every number in a report comes from the card rows passed in; nothing is computed here except bar
lengths. The text is Polish; DejaVu Sans covers Polish letters (``REPORT_FONT_DIR``).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from fpdf import FPDF

from api.services.answer import ELECTION_NAMES
from api.services.config import REPORT_FONT_DIR

# fontTools logs every font subset at INFO.
logging.getLogger("fontTools").setLevel(logging.WARNING)

LEVEL_NAMES = {"gmina": "gmina", "powiat": "powiat", "wojewodztwo": "województwo"}
SOURCES = ("Źródła: oficjalne wyniki PKW i KBW (danewyborcze.kbw.gov.pl), statystyki GUS, Bank Danych "
           "Lokalnych (CC BY 4.0). Każda liczba pochodzi z zapytania SQL do bazy Kratki Wyborczej, bez modelu językowego.")
BAR_COLOR = (99, 102, 241)


def number(value: Any, decimals: int | None = None) -> str:
    """Polish formatting: thousands separated by spaces, decimal comma; '—' for missing values."""
    if value is None:
        return "—"
    if decimals is None and float(value).is_integer():  # counts (votes, voters) may arrive as Decimal or float
        return f"{int(value):,}".replace(",", " ")
    return f"{float(value):,.{2 if decimals is None else decimals}f}".replace(",", " ").replace(".", ",")


class _Report(FPDF):
    def __init__(self, title: str) -> None:
        super().__init__(format="A4")
        folder = Path(REPORT_FONT_DIR)
        self.add_font("DejaVu", "", str(folder / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(folder / "DejaVuSans-Bold.ttf"))
        self.set_title(title)
        self.set_author("Kratka Wyborcza")
        self.set_auto_page_break(True, margin=18)
        self.alias_nb_pages()
        self.add_page()
        self.set_font("DejaVu", "B", 16)
        self.multi_cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.set_font("DejaVu", "", 9)
        self.set_text_color(100, 116, 139)
        self.cell(0, 6, f"Wygenerowano {date.today().isoformat()} · Kratka Wyborcza", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("DejaVu", "", 7)
        self.set_text_color(100, 116, 139)
        self.multi_cell(0, 3.5, f"{SOURCES}  Strona {self.page_no()}/{{nb}}")
        self.set_text_color(0, 0, 0)

    def section(self, title: str) -> None:
        self.ln(3)
        self.set_font("DejaVu", "B", 12)
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.set_font("DejaVu", "", 9)

    def facts(self, items: list[tuple[str, str]]) -> None:
        """Label: value lines (missing values left out)."""
        for label, value in items:
            if value in ("—", "", None):
                continue
            self.set_font("DejaVu", "B", 9)
            self.cell(75, 5.5, label)
            self.set_font("DejaVu", "", 9)
            self.cell(0, 5.5, value, new_x="LMARGIN", new_y="NEXT")

    def fit(self, text: str, width: float) -> str:
        """``text`` shortened with an ellipsis to fit ``width`` (mm) with a little padding."""
        if self.get_string_width(text) <= width - 2:
            return text
        while text and self.get_string_width(text + "…") > width - 2:
            text = text[:-1]
        return text + "…"

    def bar_table(self, headings: list[str], rows: list[list[str]], widths: list[float],
              bars: list[Any] | None = None, bar_max: float = 100.0) -> None:
        """A table (headings repeated after a page break); ``bars`` draws a horizontal bar per row in the last
        column (values 0..bar_max)."""
        def heading_row() -> None:
            self.set_font("DejaVu", "B", 8.5)
            self.set_fill_color(241, 245, 249)
            for heading, width in zip(headings, widths):
                self.cell(width, 6, heading, border="B", fill=True)
            self.ln()
            self.set_font("DejaVu", "", 8.5)

        heading_row()
        bar_width = self.epw - sum(widths[:-1]) if bars is not None else 0
        for i, row in enumerate(rows):
            if self.will_page_break(5.5):
                self.add_page()
                heading_row()
            for j, (text, width) in enumerate(zip(row, widths)):
                last = j == len(row) - 1
                if bars is not None and last:
                    x, y = self.get_x(), self.get_y()
                    length = max(0.0, min(bars[i] / bar_max, 1.0)) * (bar_width - 16) if bars[i] is not None else 0
                    self.set_fill_color(*BAR_COLOR)
                    self.rect(x, y + 1.3, length, 3, style="F")
                    self.set_xy(x + length + 1.5, y)
                    self.cell(14, 5.5, text)
                else:
                    self.cell(width, 5.5, self.fit(text, width or self.epw), align="R" if text[:1].isdigit() or text[:1] == "-" else "L")
            self.ln()


def _election_label(election: str, year: int, round_: int, rounds: set[int] | None = None) -> str:
    label = f"{ELECTION_NAMES.get(election, election)} {year}"
    return label + (f", tura {round_}" if round_ > 1 or (rounds and len(rounds) > 1) else "")


def unit_report(card: dict[str, Any]) -> bytes:
    """Report of one gmina, powiat or voivodeship: GUS statistics, turnout and the winner of each election."""
    level = LEVEL_NAMES.get(card["level"], card["level"])
    code = f", TERYT {card['unit']}" if card["level"] != "wojewodztwo" else ""
    pdf = _Report(f"{card['name']} ({level}{code}) — wybory w danych")
    gus = card.get("gus")
    if gus:
        pdf.section(f"Dane GUS ({gus.get('year')})")
        pdf.facts([
            ("Ludność", number(gus.get("population"))),
            ("Powierzchnia (km²)", number(gus.get("area_km2"))),
            ("Gęstość zaludnienia (os./km²)", number(gus.get("density"), 1)),
            ("Ludność miejska (%)", number(gus.get("urban_pct"), 1)),
            ("Bezrobotni w wieku produkcyjnym (%)", number(gus.get("unemployed_share_working_age"), 1)),
            ("Stopa bezrobocia (%)", number(gus.get("unemployment_rate"), 1)),
            ("Przeciętne wynagrodzenie (zł)", number(gus.get("avg_salary"))),
        ])
    turnout = card.get("turnout") or []
    if turnout:
        pdf.section("Frekwencja")
        rounds: dict[tuple[str, int], set[int]] = {}
        for r in turnout:
            rounds.setdefault((r["election"], r["year"]), set()).add(r["round"])
        pdf.bar_table(["Wybory", "Frekwencja (%)"],
                  [[_election_label(r["election"], r["year"], r["round"], rounds[(r["election"], r["year"])]),
                    number(r.get("turnout_pct"), 2)] for r in turnout],
                  [90, 0], bars=[r.get("turnout_pct") for r in turnout])
    winners = card.get("winners") or []
    if winners:
        pdf.section("Zwycięzca w jednostce (najwięcej głosów)")
        pdf.bar_table(["Wybory", "Komitet / kandydat", "Głosy", "Poparcie (%)"],
                  [[_election_label(w["election"], w["year"], w["round"]), str(w.get("name") or "—"),
                    number(w.get("votes")), number(w.get("pct"), 2)] for w in winners],
                  [62, 58, 24, 0], bars=[w.get("pct") for w in winners])
    return bytes(pdf.output())


def election_report(rounds: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> bytes:
    """Report of one election: per round its card (date, turnout, units, seats, method) and national results."""
    first = rounds[0][0]
    pdf = _Report(f"{ELECTION_NAMES.get(first['election'], first['election'])} {first['year']} — wyniki w kraju")
    for card, results in rounds:
        turnout = card.get("turnout") or {}
        pdf.section(f"Tura {card['round']}" if len(rounds) > 1 else "Najważniejsze dane")
        pdf.facts([
            ("Data", card["election_date"].isoformat() if card.get("election_date") else "—"),
            ("Frekwencja (%)", number(turnout.get("turnout_pct"), 2)),
            ("Uprawnieni", number(turnout.get("eligible_voters"))),
            ("Karty ważne", number(turnout.get("valid_ballots"))),
            ("Obwody", number(card.get("precincts"))),
            ("Okręgi", number(card.get("districts"))),
            ("Mandaty", number(card.get("seats"))),
            ("Metoda podziału mandatów", card.get("seat_method") or "—"),
            ("Progi (%)", " / ".join(number(t, 0) for t in card["thresholds_pct"]) if card.get("thresholds_pct") else "—"),
        ])
        if results:
            pdf.ln(2)
            pdf.bar_table(["Komitet / kandydat", "Głosy", "Poparcie (%)"],
                      [[str(r.get("name") or "—"), number(r.get("votes")), number(r.get("pct"), 2)] for r in results],
                      [70, 30, 0], bars=[r.get("pct") for r in results],
                      bar_max=max((r.get("pct") or 0) for r in results) or 100.0)
    return bytes(pdf.output())
