#!/usr/bin/env bash
# Monthly restore drill on a new server (runbook: docs/DEPLOYMENT.md, "Restore drill"): fetch the newest
# backups from the Storage Box, check they are fresh and intact, restore the public schema and the feedback,
# rebuild the OpenSearch question log, and report what came back. With a base URL it ends with the smoke test.
#
# Run on the new server (stack up, mart deployed) as deploy, from /srv/plujka, with deploy/drill.env
# (STORAGE_BOX, STORAGE_BOX_KEY, STORAGE_BOX_DIR as in deploy/backup.env; never create backup.env on a drill
# server, or its nightly backup lands next to production's) and AGE_IDENTITY, the backup's age private key from
# the password manager (a file; delete it after the drill). Optional: MAX_AGE_HOURS (default 36),
# FEEDBACK_DIR (default /mnt/data/feedback), DRILL_DIR (default /mnt/data/restore-drill).
#
# Usage: deploy/restore_drill.sh [base-url [api-key]]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f deploy/drill.env ]; then
  set -a
  # shellcheck disable=SC1091
  source deploy/drill.env
  set +a
fi
: "${STORAGE_BOX:?set STORAGE_BOX=user@host of the Storage Box (deploy/drill.env)}"
remote_dir="${STORAGE_BOX_DIR:-plujka}"
work="${DRILL_DIR:-/mnt/data/restore-drill}"
ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new ${STORAGE_BOX_KEY:+-i "$STORAGE_BOX_KEY"})
box() { ssh -p 23 "${ssh_opts[@]}" "$STORAGE_BOX" "$@"; }
dc=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)
psql() { "${dc[@]}" exec -T postgres psql -U plujka -d plujka -v ON_ERROR_STOP=1 -tA "$@"; }
fail() { echo "restore drill failed: $*" >&2; exit 1; }

# The restore replaces the public schema: refuse on a server with history (production), allow a new one
# whose question log holds at most today's smoke-test questions.
old=0
if [ "$(psql -c "SELECT to_regclass('public.question_log') IS NOT NULL")" = t ]; then
  old=$(psql -c "SELECT count(*) FROM question_log WHERE created_at < now() - interval '1 day'")
fi
[ "$old" = 0 ] || fail "this server has $old questions logged before today; run the drill on a new server"

# The newest backup of each kind, by the UTC stamp in its name.
listing=$(box ls -1 "$remote_dir")
dump=$(grep -E '^plujka-[0-9]{8}-[0-9]{6}\.dump(\.age)?$' <<<"$listing" | sort | tail -1) || fail "no dump on the box"
files=$(grep -E '^plujka-files-[0-9]{8}-[0-9]{6}\.tar\.gz(\.age)?$' <<<"$listing" | sort | tail -1) \
  || fail "no files archive on the box"
for name in "$dump" "$files"; do
  stamp=$(grep -oE '[0-9]{8}-[0-9]{6}' <<<"$name")
  taken=$(date -u -d "${stamp:0:8} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s)
  age_h=$(( ($(date -u +%s) - taken) / 3600 ))
  echo "newest backup: $name (${age_h} h old)"
  [ "$age_h" -le "${MAX_AGE_HOURS:-36}" ] || fail "$name is older than ${MAX_AGE_HOURS:-36} h: is the nightly backup running?"
done

mkdir -p "$work"
rm -rf "${work:?}"/*
for name in "$dump" "$files"; do
  scp -P 23 "${ssh_opts[@]}" -q "$STORAGE_BOX:$remote_dir/$name" "$work/$name"
  [ "$(sha256sum "$work/$name" | cut -d' ' -f1)" = "$(box sha256sum "$remote_dir/$name" | cut -d' ' -f1)" ] \
    || fail "checksum of the downloaded $name differs from the box"
  if [[ $name == *.age ]]; then
    [ -r "${AGE_IDENTITY:-}" ] || fail "$name is encrypted: set AGE_IDENTITY to the file with its private key"
    age -d -i "$AGE_IDENTITY" -o "$work/${name%.age}" "$work/$name" || fail "decrypting $name"
  fi
done
dump=${dump%.age}
files=${files%.age}

# The documented restore (docs/DEPLOYMENT.md, Backups), with the API stopped so nothing writes meanwhile.
"${dc[@]}" stop api
"${dc[@]}" exec -T postgres pg_restore --clean --if-exists --no-owner -U plujka -d plujka < "$work/$dump" \
  || fail "pg_restore of $dump"
"${dc[@]}" up -d api

tar -xzf "$work/$files" -C "$work"
feedback_dir="${FEEDBACK_DIR:-/mnt/data/feedback}"
mkdir -p "$feedback_dir"
cp -a "$work/feedback/." "$feedback_dir/"
# Configuration drift: variables the backed-up .env has and this server's .env lacks (or the reverse).
drift=$(diff <(sed -n 's/^\([A-Za-z0-9_]*\)=.*/\1/p' "$work/config/env" | sort -u) \
  <(sed -n 's/^\([A-Za-z0-9_]*\)=.*/\1/p' .env | sort -u) || true)
[ -z "$drift" ] || printf 'variables differing between the backed-up .env (<) and this .env (>):\n%s\n' "$drift"

"${dc[@]}" exec -T api python scripts/reindex_question_log.py

questions=$(psql -c "SELECT count(*) FROM question_log")
newest=$(psql -c "SELECT coalesce(max(created_at)::text, 'none') FROM question_log")
views=$(psql -c "SELECT count(*) FROM saved_views")
feedback=$(cat "$feedback_dir"/*.jsonl 2>/dev/null | wc -l || true)
echo "restored: $questions logged questions (newest $newest), $views saved views, $feedback feedback lines"
[ "$questions" -gt 0 ] || fail "the restored question log is empty"

if [ -n "${1:-}" ]; then
  deploy/smoke_test.sh "$@"
fi
echo "restore drill passed: $dump, $files"
