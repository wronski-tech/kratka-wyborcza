"""Which chart the UI draws for a result and from which columns: measures versus codes, the label and axes
of a scatter plot, the series of a line chart, and Polish axis titles."""

from __future__ import annotations

# Numeric columns that identify or order rows rather than measure something.
CODE_COLUMNS = ("year", "round", "prev_year", "list_position", "list_group", "rank", "place")
# Text columns naming a territorial unit, finest unit first (the hover label of a scatter point).
TERRITORIAL_LABELS = ("unit_name", "gmina", "powiat", "wojewodztwo", "area")


def numeric_columns(rows: list[dict]) -> list[str]:
    """Columns with numbers that are values, not codes (years, rounds, list numbers, ids, TERYT)."""
    first = rows[0]
    return [
        name for name, value in first.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and name not in CODE_COLUMNS and not name.endswith(("_no", "_id"))
        and not name.startswith("teryt")
    ]


def scatter_columns(rows: list[dict]) -> tuple[str, str, str] | None:
    """(label, x, y) when each row is one territorial unit with exactly two measures, else None.

    A unit is territorial when the label is a unit name column (gmina, powiat, ...) or the rows carry a
    TERYT code. Rows repeating a unit (e.g. one row per year) are not plotted.
    """
    if len(rows) < 3:
        return None
    measures = numeric_columns(rows)
    if len(measures) != 2:
        return None
    texts = [n for n, v in rows[0].items() if isinstance(v, str)]
    labels = [n for n in TERRITORIAL_LABELS if n in texts]
    if not labels and any(n.startswith("teryt") for n in rows[0]):
        # e.g. v_gus_units: teryt + name
        labels = [n for n in texts if not n.startswith("teryt")]
    if not labels:
        return None
    unit_columns = [n for n in rows[0] if n == labels[0] or n in TERRITORIAL_LABELS or n.startswith("teryt")]
    units = [tuple(row.get(n) for n in unit_columns) for row in rows]
    if len(set(units)) != len(units):
        return None
    return labels[0], measures[0], measures[1]


def line_series(rows: list[dict], max_series: int = 12) -> tuple[str | None] | None:
    """(series column or None,) when rows form a time series: two or more years, at most one varying text
    column (e.g. party) with up to ``max_series`` values, and one row per year and series; else None."""
    if not rows or "year" not in rows[0] or not numeric_columns(rows):
        return None
    if len({row.get("year") for row in rows}) < 2:
        return None
    texts = [n for n, v in rows[0].items() if isinstance(v, str) and not n.startswith("teryt")]
    varying = [n for n in texts if len({row.get(n) for row in rows}) > 1]
    if len(varying) > 1:
        return None
    series = varying[0] if varying else None
    keys = [(row.get("year"), row.get(series) if series else None) for row in rows]
    if len(set(keys)) != len(keys) or (series and len({k[1] for k in keys}) > max_series):
        return None
    return (series,)


# Polish axis titles of common result columns; others are shown by name.
AXIS_TITLES = {
    "pct": "poparcie (%)", "votes": "głosy", "seats": "mandaty", "turnout_pct": "frekwencja (%)",
    "change_pp": "zmiana (pkt proc.)", "prev_pct": "poparcie wcześniej (%)", "valid_votes": "głosy ważne",
    "eligible_voters": "uprawnieni", "year": "rok", "party": "partia", "wojewodztwo": "województwo",
    "committee_name": "komitet", "candidate": "kandydat", "r": "korelacja (r)",
}
# Chart types the model may pick (dynamic_sql.CHART_TYPES); the rows must still fit the type.
CHART_KINDS = ("bar", "line", "scatter", "none")


def axis_title(column: str) -> str:
    return AXIS_TITLES.get(column, column)


def value_column(rows: list[dict]) -> str | None:
    """Column to chart: a change in points first, then a percentage, votes, seats, then any other number."""
    numeric = numeric_columns(rows)
    for pattern in ("_pp", "pct", "votes", "seats", "mandat"):
        for name in numeric:
            if pattern in name:
                return name
    return numeric[0] if numeric else None


def bar_columns(rows: list[dict], max_rows: int) -> tuple[str, str] | None:
    """(label, value) when each of 2..max_rows rows is one distinctly labelled item with a number, else None."""
    if not 2 <= len(rows) <= max_rows:
        return None
    value = value_column(rows)
    labels = [n for n, v in rows[0].items() if isinstance(v, str) and not n.startswith("teryt")]
    if not value or not labels:
        return None
    names = [row.get(labels[0]) for row in rows if row.get(value) is not None]
    if len(set(names)) != len(names):
        return None
    return labels[0], value


def chart_kind(rows: list[dict], preferred: str | None, max_bar_rows: int) -> tuple[str, tuple] | None:
    """(kind, columns) of the chart to draw: the model's ``preferred`` type when the rows fit it ("none": no
    chart), otherwise the first that fits of scatter, line and bar (the deterministic rule); None for no chart."""
    if preferred == "none":
        return None
    fits: dict[str, tuple | None] = {"scatter": scatter_columns(rows), "line": line_series(rows), "bar": bar_columns(rows, max_bar_rows)}
    order = ([preferred] if preferred in fits else []) + list(fits)
    return next(((kind, columns) for kind in order if (columns := fits[kind])), None)
