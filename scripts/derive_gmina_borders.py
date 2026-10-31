#!/usr/bin/env python3
"""Derive which gminas lie on the national border or the sea coast (``api/services/mart/gmina_borders.csv``).

Reads the map boundaries ``streamlit_app/geo/gminy.json`` (PRG 2021, current gminas; Kamienica and
Supraśl as their pre-2025 extents, whose edges match their neighbours). Neighbouring gminas share
their border vertices exactly, so an edge used by one gmina only is the national border or the coast.
Each such edge is assigned by its midpoint to the Baltic coast or to a neighbouring country, with
coordinate rules around the tripoints (DE/CZ at Zittau, CZ/SK at Jaworzynka, SK/UA at Krzemieniec,
UA/BY at the Bug, BY/LT, LT/RU at Wisztyniec, RU and the coast at the Vistula Spit). The rules are
approximate within a few kilometres of a tripoint; lagoon shores are inside gmina polygons and do not
count as coast.

  python3 scripts/derive_gmina_borders.py            # rewrite the CSV
  python3 scripts/derive_gmina_borders.py --check    # fail when the CSV is stale
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEO = ROOT / "streamlit_app" / "geo" / "gminy.json"
OUT = ROOT / "api" / "services" / "mart" / "gmina_borders.csv"


def side(lon: float, lat: float) -> str:
    """Neighbour of a border edge at (lon, lat): an ISO country code or "sea"."""
    if lat >= 54.3 and lon >= 19.6:
        return "LT" if lon >= 22.79 else "RU"
    if lat >= 53.9 and lon >= 22.5:
        return "LT"
    if lon >= 22.55 and lat >= 51.5:
        return "BY"
    if lon >= 22.55 and lat >= 49.1:
        return "UA"
    if lat < 49.75 and lon >= 18.85:
        return "SK"
    if (51.05 <= lat < 53.88 and lon <= 15.05) or (50.87 <= lat < 51.05 and lon <= 14.93) or (lon <= 14.28 and 50.87 <= lat < 53.935):
        return "DE"
    if lat >= 53.88:
        return "sea"
    return "CZ"


def derive(path: Path = GEO) -> list[tuple[str, str, str]]:
    """Rows (teryt_gmina, name, borders joined by ";") of every gmina with a border edge."""
    features = [f for f in json.loads(path.read_text(encoding="utf-8"))["features"]
                if f["properties"].get("years", [0, 9999])[1] >= 2024]
    used: Counter = Counter()
    edges = defaultdict(list)
    for feature in features:
        geometry = feature["geometry"]
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        for ring in (ring for polygon in polygons for ring in polygon):
            for a, b in zip(ring, ring[1:]):
                edge = tuple(sorted((tuple(a), tuple(b))))
                used[edge] += 1
                edges[feature["properties"]["teryt"], feature["properties"]["name"]].append(edge)
    rows = []
    for (teryt, name), own in sorted(edges.items()):
        sides = Counter(side((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in own if used[(a, b)] == 1)
        if sides:
            rows.append((teryt, name, ";".join(sorted(sides))))
    return rows


def render(rows: list[tuple[str, str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["teryt_gmina", "name", "borders"])
    writer.writerows(rows)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail when the CSV differs from the boundaries")
    args = parser.parse_args()
    text = render(derive())
    if args.check:
        if OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is stale; run scripts/derive_gmina_borders.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {text.count(chr(10)) - 1} gminas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
