#!/usr/bin/env python3
"""Compare a database restored from a backup with its source.

Both must have the same tables, indexes, views, sequences and functions in the given schemas, the same number of
rows in every table and the same sequence positions; the source must not be empty, so an empty backup
cannot pass. Exits with an error listing every difference.

  python3 scripts/check_restore.py SOURCE_URL RESTORED_URL [--schemas public mart]

CI runs it after restoring the nightly backup (``BACKUP_SCHEMAS=public scripts/backup_db.sh``) into an
empty database by the procedure in docs/DEPLOYMENT.md; use it the same way for a restore drill.
"""

from __future__ import annotations

import argparse
from typing import Any

import psycopg
from psycopg import sql

INVENTORY_SQL = """
SELECT n.nspname || '.' || c.relname AS name, c.relkind::text AS kind
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%(schemas)s) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'i')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')  -- not owned by an extension
UNION ALL
SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', 'f'
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = ANY(%(schemas)s)
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
"""


def inventory(url: str, schemas: list[str]) -> dict[str, Any]:
    """``{object: kind}`` plus ``rows`` per table and ``position`` per sequence."""
    with psycopg.connect(url) as conn:
        objects = dict(conn.execute(INVENTORY_SQL, {"schemas": schemas}).fetchall())
        counts: dict[str, Any] = {}
        for name, kind in sorted(objects.items()):
            if kind in ("r", "p", "S"):
                ident = sql.Identifier(*name.split(".", 1))
                query = sql.SQL("SELECT count(*) FROM {}" if kind != "S" else "SELECT last_value FROM {}").format(ident)
                counts[name] = conn.execute(query).fetchone()[0]  # type: ignore[index]
    return {"objects": objects, "counts": counts}


def compare(source: dict[str, Any], restored: dict[str, Any]) -> list[str]:
    """Differences between the two inventories, one line each."""
    problems = [f"missing after restore: {name}" for name in sorted(source["objects"].keys() - restored["objects"].keys())]
    problems += [f"not in the source: {name}" for name in sorted(restored["objects"].keys() - source["objects"].keys())]
    for name, value in sorted(source["counts"].items()):
        if name in restored["counts"] and restored["counts"][name] != value:
            problems.append(f"{name}: {value} in the source, {restored['counts'][name]} after restore")
    if not any(v for n, v in source["counts"].items() if source["objects"][n] in ("r", "p")):
        problems.append("the source has no rows: nothing was tested")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source")
    parser.add_argument("restored")
    parser.add_argument("--schemas", nargs="+", default=["public"])
    args = parser.parse_args()
    source, restored = inventory(args.source, args.schemas), inventory(args.restored, args.schemas)
    problems = compare(source, restored)
    tables = [n for n, k in source["objects"].items() if k in ("r", "p")]
    print(f"{len(source['objects'])} objects, {len(tables)} tables, {sum(source['counts'][t] for t in tables)} rows "
          f"in {', '.join(args.schemas)}")
    if problems:
        raise SystemExit("restore differs from the source:\n" + "\n".join(problems))
    print("restore matches the source")


if __name__ == "__main__":
    main()
