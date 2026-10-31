"""Natural-language answer for SQL rows, with every number checked against the rows.

The LLM may phrase the result in Polish, but it may not introduce facts. ``compose_answer`` therefore
extracts every number from the generated text and accepts the text only when each number appears in
the result rows (allowing rounding to 0-2 decimals and Polish number formatting) or in the question.
Otherwise it returns a deterministic summary built directly from the rows.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any

import sqlglot
from openai import OpenAI
from sqlglot import exp

from api.services.config import ANSWER_SUMMARY_ENABLED, MART_MAX_ROWS, OPENAI_API_KEY, OPENAI_CHAT_MODEL
from api.services.dynamic_sql import split_parts

log = logging.getLogger(__name__)

MAX_ROWS_IN_PROMPT = 30

# A number in Polish or English formatting: "7 640 854", "7,640,854", "35,38", "35.4", "2023".
_NUMBER_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:,\d+)?(?![\d])|(?<![\w])\d+(?:[.,]\d+)*")

ANSWER_PROMPT = """You describe the result of a SQL query over official Polish election data.
Write 1-3 short sentences that answer the question, in the question's language (Polish unless it is
asked in English).
Rules:
- Use only facts and numbers that appear in the rows. Do not compute new numbers (no differences,
  sums or ratios that are not in the rows) and do not add knowledge from outside the rows.
- Copy numbers exactly; you may round percentages to one decimal. In Polish write thousands with
  spaces (7 640 854) and decimals with a comma (35,38%); in English 7,640,854 and 35.38%.
- If the rows do not answer the question, say so briefly.
Return JSON: {"answer": "..."}"""


def _number_variants(token: str) -> set[Decimal]:
    """All plausible values of a number token (``1,234`` may be 1234 or 1.234)."""
    compact = re.sub(r"[ \u00a0\u202f]", "", token)
    variants: set[Decimal] = set()
    candidates = {compact.replace(",", "."), compact.replace(",", ""), compact.replace(".", "").replace(",", "."),
                  compact.replace(".", "")}
    for candidate in candidates:
        try:
            variants.add(Decimal(candidate))
        except ArithmeticError:
            continue
    return variants


def numbers_in_text(text: str) -> list[str]:
    """Number tokens in ``text`` (thousand separators and decimal commas kept together)."""
    return [m.group(0).rstrip(".,") for m in _NUMBER_RE.finditer(text) if m.group(0).rstrip(".,")]


def allowed_numbers(rows: list[dict[str, Any]], question: str) -> set[Decimal]:
    """Numbers an answer may contain: row values (and their roundings), numbers in the question, row ranks."""
    allowed: set[Decimal] = {Decimal(n) for n in range(1, len(rows) + 1)}
    for row in rows:
        for value in row.values():
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float, Decimal)):
                number = Decimal(str(value))
                allowed.add(number)
                allowed.update(round(number, digits) for digits in (0, 1, 2))
            else:
                for token in numbers_in_text(str(value)):
                    allowed.update(_number_variants(token))
    for token in numbers_in_text(question):
        allowed.update(_number_variants(token))
    return allowed


def unsupported_numbers(text: str, rows: list[dict[str, Any]], question: str) -> list[str]:
    """Number tokens of ``text`` that are not backed by the rows or the question."""
    allowed = allowed_numbers(rows, question)
    return [token for token in numbers_in_text(text) if not (_number_variants(token) & allowed)]


# Columns holding codes rather than quantities: printed without thousand separators.
_CODE_COLUMNS = ("year", "round", "prev_year")
_CODE_SUFFIXES = ("_no", "_id")


def format_number(value: Any, column: str = "") -> str:
    """Polish formatting: thousands separated by spaces, decimal comma; years and ids stay plain."""
    if isinstance(value, bool):
        return "tak" if value else "nie"
    if isinstance(value, int) and (column in _CODE_COLUMNS or column.endswith(_CODE_SUFFIXES)):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, (float, Decimal)):
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    return str(value)


# Words of English questions that Polish ones do not use.
_ENGLISH_WORDS = frozenset(
    "the what who which how many much did does were is are in of and won win get got votes turnout election "
    "elections party results result".split()
)


def is_english(question: str) -> bool:
    """Whether the question is asked in English (at least two common English words)."""
    return len(_ENGLISH_WORDS.intersection(re.findall(r"[a-z]+", question.lower()))) >= 2


def fallback_answer(rows: list[dict[str, Any]], english: bool = False) -> str:
    """Deterministic summary taken directly from the rows (in English for English questions)."""
    if not rows:
        return "No results in the data for this question." if english else "Brak wyników w danych dla tego pytania."
    first = "; ".join(f"{column}: {format_number(value, column)}" for column, value in rows[0].items() if value is not None)
    if len(rows) == 1:
        return f"Result: {first}." if english else f"Wynik: {first}."
    spread = _extremes(rows)
    if spread:
        label, measure, top, bottom = spread
        # A time series names the year of each extreme.
        when = (lambda r: f", {r['year']}") if len(_values(rows, "year")) > 1 else (lambda r: "")
        top_text = f"{top[label]}{when(top)} ({format_number(top[measure], measure)})"
        bottom_text = f"{bottom[label]}{when(bottom)} ({format_number(bottom[measure], measure)})"
        if english:
            return f"Found {format_number(len(rows))} rows. Highest {measure}: {top_text}; lowest: {bottom_text}."
        return f"Znaleziono {format_number(len(rows))} wierszy. Najwyższa wartość {measure}: {top_text}; najniższa: {bottom_text}."
    if english:
        return f"Found {format_number(len(rows))} rows. First: {first}."
    return f"Znaleziono {format_number(len(rows))} wierszy. Pierwszy: {first}."


def _measure(rows: list[dict[str, Any]]) -> str | None:
    """Main number of the rows: a change in points first, then pct, another percentage, votes, seats, then any
    other value (no codes)."""
    numeric = [c for c, v in rows[0].items() if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
               and c not in _CODE_COLUMNS and not c.endswith(_CODE_SUFFIXES) and not c.startswith("teryt")]
    preferences = (lambda c: c.endswith("_pp"), lambda c: c == "pct", lambda c: "pct" in c,
                   lambda c: "votes" in c, lambda c: "seats" in c)
    return next((c for prefer in preferences for c in numeric if prefer(c)), numeric[0] if numeric else None)


def _extremes(rows: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any], dict[str, Any]] | None:
    """(label, measure, highest row, lowest row) of three or more rows labelled by a text column, else None."""
    if len(rows) < 3:
        return None
    measure = _measure(rows)
    label = next((c for c, v in rows[0].items() if isinstance(v, str) and not c.startswith("teryt")), None)
    if not measure or not label:
        return None
    valued = [r for r in rows if isinstance(r.get(measure), (int, float, Decimal)) and r.get(label) is not None]
    if len(valued) < 3:
        return None
    return label, measure, max(valued, key=lambda r: r[measure]), min(valued, key=lambda r: r[measure])


def _values(rows: list[dict[str, Any]], column: str) -> set[Any]:
    """Distinct values of ``column`` in the rows."""
    return {row.get(column) for row in rows if column in row}


def _about(rows: list[dict[str, Any]], sql: str, election: str, year: int) -> bool:
    """Whether the rows (``election``/``year`` columns) or the SQL text concern one election-year."""
    sql_l = sql.lower()
    elections = _values(rows, "election") or {e for e in ("prezydent", "pe", "sejm", "senat") if f"'{e}'" in sql_l or f"v_{e}_" in sql_l}
    years = _values(rows, "year") or {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", sql)}
    return election in elections and year in years


def _pre1999_units(rows: list[dict[str, Any]], sql: str) -> bool:
    """Whether 1990-1997 results are shown by gmina or powiat (through mart.gmina_codes_pre1999)."""
    years = _values(rows, "year") or {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", sql)}
    by_unit = bool(_values(rows, "level") & {"gmina", "powiat"}) or "gmina_codes_pre1999" in sql.lower()
    return by_unit and any(isinstance(y, int) and 1990 <= y <= 1997 for y in years)


def _about_rada_1994(rows: list[dict[str, Any]], sql: str) -> bool:
    """Whether council results of 1994 (pre-1999 division) are shown."""
    years = _values(rows, "year") or {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", sql)}
    return 1994 in years and "rada_" in sql.lower()


# Columns with the vote or voter base of a percentage; below SMALL_BASE a few votes move it by whole points.
_BASE_COLUMNS = ("unit_valid_votes", "valid_votes", "valid_ballots", "eligible_voters", "ballots_issued")
SMALL_BASE = 1000


def _small_units(rows: list[dict[str, Any]], sql: str) -> bool:
    """Percentages of which at least one rests on fewer than SMALL_BASE votes or voters."""
    if not any(c == "pct" or c.endswith("_pct") for c in rows[0]):
        return False
    return any(isinstance(row.get(c), (int, float, Decimal)) and not isinstance(row.get(c), bool) and row[c] < SMALL_BASE
               for row in rows for c in _BASE_COLUMNS)


def _warsaw_districts(rows: list[dict[str, Any]], sql: str) -> bool:
    """Rows of gminas that include Warsaw's districts (TERYT 1465xx other than the city, 146501)."""
    return len(rows) > 1 and any(str(row.get("teryt_gmina") or "").startswith("1465")
                                 and row.get("teryt_gmina") != "146501" for row in rows)


# Caveats of the data that a reader must know to interpret the rows. Each rule looks only at the rows
# and the SQL, never at outside facts, so the text contains no numbers of its own. Details of each
# gap are in docs/MART.md ("Known gaps").
CAVEATS: tuple[tuple[str, Any], ...] = (
    # How the result reads (its shape, not a data gap).
    (
        "Wynik osiągnął limit wierszy, więc pokazano tylko jego część; zawęź pytanie (np. do województwa) "
        "albo zapytaj o ranking.",
        lambda rows, sql: len(rows) >= MART_MAX_ROWS,
    ),
    (
        "W wyniku są jednostki z niewielką liczbą głosów lub uprawnionych: w nich kilka głosów zmienia odsetek "
        "o wiele punktów, więc skrajne wartości bywają przypadkowe.",
        _small_units,
    ),
    (
        "Warszawa występuje w wynikach według gmin jako osobne dzielnice, nie jako jedna gmina.",
        _warsaw_districts,
    ),
    (
        "To symulacja: przyjęto te same głosy, a zmieniono tylko sposób przeliczania ich na mandaty "
        "(próg, metodę, listy lub okręgi); wyborcy mogliby wtedy głosować inaczej.",
        lambda rows, sql: re.search(r"allocate_seats\s*\(|seats_(dhondt|sainte_lague)", sql, re.IGNORECASE) is not None,
    ),
    (
        "Korelacja opisuje, jak wartości współwystępują w jednostkach terytorialnych; nie dowodzi związku "
        "przyczynowego i nie mówi, jak głosowali poszczególni wyborcy.",
        lambda rows, sql: re.search(r"\b(corr|regr_\w+)\s*\(", sql, re.IGNORECASE) is not None,
    ),
    (
        "Porównanie dotyczy różnych komitetów tej samej rodziny partyjnej (np. PO → KO, PSL → Trzecia Droga); "
        "zmiana poparcia nie jest porównaniem tego samego komitetu.",
        lambda rows, sql: False in _values(rows, "comparable"),
    ),
    (
        "W części okręgów rad PKW nie opublikowała kandydatów skreślonych po druku kart, więc głosy na listy "
        "są tam sumą głosów pozostałych kandydatów (dane przybliżone).",
        lambda rows, sql: False in _values(rows, "votes_exact"),
    ),
    (
        "Frekwencja w wyborach do PE 2014 nie obejmuje pakietów wyborczych (głosowanie korespondencyjne), "
        "więc może być nieznacznie niższa niż podana przez PKW.",
        lambda rows, sql: _about(rows, sql, "pe", 2014) and ("turnout_pct" in sql.lower() or bool(_values(rows, "turnout_pct"))),
    ),
    (
        "Frekwencja w wyborach do Sejmu i Senatu 1993 liczona z bazy jest nieznacznie wyższa od oficjalnej: "
        "pliki KBW podają w dwóch okręgach mniej uprawnionych niż PKW, a w wyborach do Senatu województwo "
        "skierniewickie nie ma protokołów obwodowych.",
        lambda rows, sql: (_about(rows, sql, "sejm", 1993) or _about(rows, sql, "senat", 1993))
        and ("turnout_pct" in sql.lower() or bool(_values(rows, "turnout_pct"))),
    ),
    (
        "Wyniki kandydatów na prezydenta są sumą wyników obwodów; kilka obwodów za granicą nie opublikowało "
        "wyników, więc sumy krajowe mogą być nieco niższe od oficjalnych (oficjalne są w prezydent_candidates).",
        lambda rows, sql: "v_prezydent_results" in sql.lower() and "country" in _values(rows, "level"),
    ),
    (
        "Wyniki z lat 1990–1997 przypisano do dzisiejszych gmin i powiatów przez dawne kody gmin (klucz "
        "nieoficjalny, dopasowany po nazwie); przed 1995 r. gmina obejmuje też gminy wydzielone z niej później, "
        "a w 1991 r. część gmin wiejskich jest liczona razem z miastem o tej samej nazwie.",
        _pre1999_units,
    ),
    (
        "Rady gmin z 1994 r. są w ówczesnym podziale kraju: dawne województwa (nieporównywalne z dzisiejszymi), "
        "bez powiatów i z kodami gmin sprzed 1999 r.; KBW nie publikuje danych z wyborów samorządowych 1990 r.",
        _about_rada_1994,
    ),
    (
        "Referenda lokalne w danych to tylko referenda w sprawie odwołania organów gminy lub powiatu od kadencji "
        "2014–2018; PKW nie publikuje wyników referendów lokalnych w innych sprawach ani wcześniejszych.",
        lambda rows, sql: "local_referenda" in sql.lower(),
    ),
    (
        "PKW nie publikuje identyfikatorów osób: kandydatury łączone są po imieniu, nazwisku, płci "
        "(i roku urodzenia w wyborach samorządowych), więc imiennicy mogą być połączeni lub rozdzieleni błędnie.",
        lambda rows, sql: "v_person_history" in sql.lower() or "persons" in sql.lower(),
    ),
)


def caveats(rows: list[dict[str, Any]], sql: str) -> list[str]:
    """Polish caveats that apply to these rows and this SQL (empty when there are no rows)."""
    if not rows:
        return []
    return [text for text, applies in CAVEATS if applies(rows, sql or "")]


# Polish labels for the query explanation; anything not listed is shown by its SQL name.
_RELATION_LABELS = {
    "v_party_results": "wyniki rodzin partii w jednostkach", "v_party_change": "zmiana poparcia od poprzednich wyborów",
    "v_turnout": "frekwencja", "v_gus_units": "statystyki GUS", "v_seat_methods": "mandaty różnymi metodami",
    "persons": "osoby", "v_person_history": "starty osób", "unit_attributes": "cechy gmin",
    "elections": "lista wyborów", "party_families": "rodziny partii", "v_gmina_continuity": "ciągłość gmin",
    "sejm_committees": "komitety w wyborach do Sejmu", "sejm_district_lists": "listy w okręgach do Sejmu",
    "sejm_districts": "okręgi do Sejmu",
}
_COLUMN_LABELS = {
    "election": "wybory", "year": "rok", "round": "tura", "level": "poziom", "party": "partia",
    "wojewodztwo": "województwo", "powiat": "powiat", "gmina": "gmina", "teryt_gmina": "kod gminy",
    "teryt_powiat": "kod powiatu", "teryt": "kod TERYT", "person_id": "osoba", "committee_name": "komitet",
    "committee_id": "komitet", "aliases": "skrót komitetu", "district_no": "okręg", "votes": "głosy",
    "pct": "poparcie (%)", "seats": "mandaty", "turnout_pct": "frekwencja (%)", "change_pp": "zmiana (pkt proc.)",
    "family": "rodzina partii", "alias": "skrót komitetu",
}
_FUNCTION_LABELS = {
    "sum": "suma", "count": "liczba", "avg": "średnia", "min": "minimum", "max": "maksimum", "corr": "korelacja",
    "regr_slope": "nachylenie regresji", "regr_r2": "R²", "allocate_seats": "podział mandatów",
    "rank": "miejsce w rankingu", "dense_rank": "miejsce w rankingu", "row_number": "numer wiersza",
    "lag": "wartość z poprzedniego wiersza", "stddev": "odchylenie standardowe", "percentile_cont": "mediana/percentyl",
}
_OPERATORS = {exp.EQ: "=", exp.NEQ: "≠", exp.GT: ">", exp.GTE: "≥", exp.LT: "<", exp.LTE: "≤"}


def _column_label(column: exp.Expression, select: exp.Expression | None = None) -> str:
    """'rok', or 'rok (b)' for a qualified column of a self-join; ORDER/GROUP BY 2 names output column 2."""
    if isinstance(column, exp.Literal) and column.is_int and isinstance(select, exp.Select) \
            and 0 < int(column.this) <= len(select.expressions):
        name = select.expressions[int(column.this) - 1].alias_or_name
        return _COLUMN_LABELS.get(name, name)
    label = _COLUMN_LABELS.get(column.name, column.name) if isinstance(column, exp.Column) else column.sql("postgres")
    return f"{label} ({column.table})" if isinstance(column, exp.Column) and column.table else label


def _value(node: exp.Expression) -> str:
    return node.this if isinstance(node, exp.Literal) else node.sql("postgres")


def _condition(node: exp.Expression) -> str:
    """One WHERE conjunct in Polish where it is a plain comparison, else its SQL."""
    if isinstance(node, exp.Binary) and type(node) in _OPERATORS and isinstance(node.left, exp.Column) and isinstance(node.right, (exp.Literal, exp.Column)):
        right = _column_label(node.right) if isinstance(node.right, exp.Column) else _value(node.right)
        return f"{_column_label(node.left)} {_OPERATORS[type(node)]} {right}"
    if isinstance(node, exp.EQ) and isinstance(node.left, exp.Literal) and isinstance(node.right, exp.Any):
        return f"{_column_label(node.right.this.unnest())} zawiera {_value(node.left)}"
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column) and node.expressions:
        return f"{_column_label(node.this)}: {', '.join(_value(v) for v in node.expressions)}"
    if isinstance(node, exp.Column):
        return f"{_column_label(node)} = tak"
    return node.sql("postgres")


def _explain_select(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return []
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    relations = list(dict.fromkeys(t.name.lower() for t in tree.find_all(exp.Table)
                                   if t.name and t.name.lower() not in ctes))
    if not relations or not any((t.db or "").lower() == "mart" for t in tree.find_all(exp.Table)):
        return []
    # The guard wraps some queries as SELECT * FROM (...) q LIMIT n: describe the inner query.
    outer = tree
    while isinstance(outer, exp.Select) and outer.is_star and isinstance(outer.args.get("from"), exp.From) \
            and isinstance(outer.args["from"].this, exp.Subquery):
        outer = outer.args["from"].this.this
    lines = ["Dane: " + ", ".join(f"{r} ({_RELATION_LABELS[r]})" if r in _RELATION_LABELS else r for r in relations) + "."]
    conditions = list(dict.fromkeys(
        _condition(c) for where in tree.find_all(exp.Where)
        for c in (where.this.flatten() if isinstance(where.this, exp.And) else [where.this])
    ))
    if conditions:
        lines.append("Warunki: " + "; ".join(conditions) + ".")
    functions = list(dict.fromkeys(
        _FUNCTION_LABELS[name] for f in tree.find_all(exp.Func)
        if (name := (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()) in _FUNCTION_LABELS
    ))
    if functions:
        lines.append("Obliczenia: " + ", ".join(functions) + ".")
    if isinstance(outer, exp.Select) and outer.args.get("group"):
        lines.append("Grupowanie: " + ", ".join(_column_label(g, outer) for g in outer.args["group"].expressions) + ".")
    order = outer.args.get("order") if isinstance(outer, exp.Select) else None
    one_row = isinstance(outer, exp.Select) and not outer.args.get("group") \
        and any(e.find(exp.AggFunc) and not e.find(exp.Window) for e in outer.expressions)
    if order and order.expressions and not one_row:  # the first key only; the rest break ties
        first = order.expressions[0]
        lines.append(f"Kolejność: {_column_label(first.this, outer)} {'malejąco' if first.args.get('desc') else 'rosnąco'}.")
    limit = tree.args.get("limit") or (outer.args.get("limit") if isinstance(outer, exp.Select) else None)
    if limit is not None and isinstance(limit.expression, exp.Literal) and int(limit.expression.this) < MART_MAX_ROWS:
        lines.append(f"Najwyżej {limit.expression.this} wierszy.")
    return lines


_FINER_LEVEL = {"country": "wojewodztwo", "wojewodztwo": "powiat", "powiat": "gmina"}
_LEVEL_WORDS = {"wojewodztwo": ("województwa", "voivodeship"), "powiat": ("powiaty", "powiat"), "gmina": ("gminy", "gmina")}


def _sql_values(sql: str, column: str) -> set[str]:
    """Literal values compared with ``column`` in the SQL (``year = 2023``, ``r.election = 'sejm'``)."""
    return {m.group(1) for m in re.finditer(rf"\b{column}\s*=\s*'?(\w+)'?", sql, re.IGNORECASE)}


def follow_ups(question: str, rows: list[dict[str, Any]], sql: str, loaded: dict[str, list[int]]) -> list[str]:
    """Up to three follow-up questions on this result, asked with it as the previous turn: the previous loaded
    election of the same kind, a finer territorial level, the change against that election. Built from the
    rows, the SQL and the loaded years only; empty when the result is not about one election-year."""
    if not rows:
        return []
    elections = {str(e) for e in _values(rows, "election")} or _sql_values(sql, "election")
    years = {int(y) for y in _values(rows, "year") if isinstance(y, int)} or {int(y) for y in _sql_values(sql, "year") if y.isdigit()}
    if len(elections) != 1 or len(years) != 1:
        return []
    election, year = elections.pop(), years.pop()
    earlier = max((y for y in loaded.get(election, []) if y < year), default=None)
    english = is_english(question)
    out = []
    if earlier:
        out.append(f"And in {earlier}?" if english else f"A w {earlier}?")
    levels = {str(v) for v in _values(rows, "level")} or _sql_values(sql, "level")
    finer = _FINER_LEVEL.get(levels.pop()) if len(levels) == 1 else None
    if finer:
        polish, eng = _LEVEL_WORDS[finer]
        out.append(f"And by {eng}?" if english else f"A w podziale na {polish}?")
    if earlier and ("party" in rows[0] or "pct" in rows[0]) and "change" not in sql.lower():
        out.append(f"How did it change compared with {earlier}?" if english else f"Jak to się zmieniło w porównaniu z {earlier}?")
    return out[:3]


def explain_sql(sql: str) -> list[str]:
    """Polish description of what a mart query does (sources, conditions, calculations, grouping, order),
    read from the SQL itself, so it never differs from the query that produced the rows. Empty when the
    query does not read the mart. Parts of a multi-part query are prefixed with their titles."""
    lines = []
    for title, select in split_parts(sql or ""):
        part = _explain_select(select)
        lines += [f"{title}: {line}" for line in part] if title else part
    return lines


def _rows_for_prompt(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | dict[str, Any]:
    """All rows of a small result; of a large one its first and last rows and its extremes, all copied from
    the rows (no computed numbers), so the summary covers the whole result rather than its first rows."""
    if len(rows) <= MAX_ROWS_IN_PROMPT:
        return rows
    digest: dict[str, Any] = {"row_count": len(rows), "first_rows": rows[:20], "last_rows": rows[-5:]}
    spread = _extremes(rows)
    if spread:
        _, measure, top, bottom = spread
        digest["highest"], digest["lowest"] = {measure: top}, {measure: bottom}
    return digest


def _llm_answer(question: str, rows: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    """Ask the chat model for a short answer; return (text, token usage)."""
    payload = json.dumps(_rows_for_prompt(rows), ensure_ascii=False, default=str)
    note = ""
    if len(rows) > MAX_ROWS_IN_PROMPT:
        note = (f"\n(The result has {len(rows)} rows in the order of the query: shown are the first and last rows "
                "and the rows with the highest and lowest main value. Summarize the whole result from them.)")
    response = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": f"Question: {question}\nRows: {payload}{note}"},
        ],
    )
    usage = response.usage
    tokens = {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens} if usage else {}
    text = (json.loads(response.choices[0].message.content or "{}").get("answer") or "").strip()
    return text, tokens


def compose_answer(question: str, rows: list[dict[str, Any]], sql: str = "") -> dict[str, Any]:
    """Answer text for the rows: LLM text when every number checks out, otherwise the fallback summary.

    Returns ``{"text", "source": "llm" | "fallback", "verified": bool, "caveats": [...], ...}``;
    ``rejected_numbers`` lists the numbers that made an LLM text fail the check; ``caveats`` are fixed
    Polish notes about data gaps that apply to these rows (see ``CAVEATS``); ``explanation`` describes the
    query in Polish (``explain_sql``).
    """
    result: dict[str, Any] = {"text": fallback_answer(rows, is_english(question)), "source": "fallback", "verified": True,
                              "caveats": caveats(rows, sql), "explanation": explain_sql(sql)}
    if not rows or not ANSWER_SUMMARY_ENABLED or not OPENAI_API_KEY:
        return result
    try:
        text, tokens = _llm_answer(question, rows)
    except Exception as exc:  # the answer text is optional; rows are still returned
        log.warning("answer generation failed: %s", exc)
        return {**result, "error": str(exc)}
    result["tokens"] = tokens
    if not text:
        return result
    rejected = unsupported_numbers(text, rows, question)
    if rejected:
        log.info("answer rejected: numbers %s are not in the SQL result", rejected)
        return {**result, "rejected_numbers": rejected}
    return {**result, "text": text, "source": "llm"}
