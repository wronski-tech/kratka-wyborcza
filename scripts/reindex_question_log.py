"""Rebuild the OpenSearch ``question_logs`` index from the question log in Postgres.

Postgres ``question_log`` is the durable copy (in the nightly backup); OpenSearch is derived from it
for question hints and dashboards. Run after a restore, after losing the OpenSearch volume, or after
a mapping change (delete the index first). Documents keep their Postgres id as ``_id``, so a rerun
replaces them instead of adding duplicates.

    DATABASE_URL=... OPENSEARCH_URL=... python3 scripts/reindex_question_log.py [--since 2026-11-01]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from opensearchpy import helpers  # noqa: E402

from api.services import db, opensearch_store  # noqa: E402


def main() -> None:
    """Index every question log entry (from ``--since``) and print the count."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="only entries created at or after this timestamp")
    args = parser.parse_args()
    opensearch_store.ensure_index()
    actions = (
        {"_index": opensearch_store.INDEX_NAME, "_id": str(log_id), "_source": entry}
        for log_id, entry in db.question_log_entries(args.since)
    )
    indexed, _ = helpers.bulk(opensearch_store._client(), actions, chunk_size=500)
    print(f"indexed {indexed} question log entries into {opensearch_store.INDEX_NAME}")


if __name__ == "__main__":
    main()
