#!/usr/bin/env bash
# Nightly backup to the Storage Box, with retention 7 daily / 4 weekly / 6 monthly per series
# (scripts/backup_retention.py):
#   plujka-YYYYmmdd-HHMMSS.dump          the public schema (question log, saved views, token usage, legacy kbw_*)
#   plujka-files-YYYYmmdd-HHMMSS.tar.gz  feedback (feedback.jsonl: thumbs-down answers, person reports) and the
#                                        server configuration: .env with secret values removed, caddy.env, backup.env
# The mart is not backed up: CI publishes it (release mart-latest) and deploy/deploy_mart.sh restores it.
# Secrets (passwords, API keys) are not in any backup: keep them in a password manager.
#
# Run by plujka-backup.timer (deploy/systemd/) as deploy, from /srv/plujka, with deploy/backup.env:
#   STORAGE_BOX=u123456@u123456.your-storagebox.de   user@host (terraform output storage_box_username / _server)
#   STORAGE_BOX_KEY=/srv/plujka/deploy/storagebox_key  private key installed on the box (docs/DEPLOYMENT.md)
#   STORAGE_BOX_DIR=plujka                              directory on the box (default plujka)
#   BACKUP_DIR=/mnt/data/backups                        local copies, the newest 3 of each kind are kept (default)
#   FEEDBACK_DIR=/mnt/data/feedback                     the API's feedback directory (default)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${STORAGE_BOX:?set STORAGE_BOX=user@host of the Storage Box}"
remote_dir="${STORAGE_BOX_DIR:-plujka}"
backup_dir="${BACKUP_DIR:-/mnt/data/backups}"
ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new ${STORAGE_BOX_KEY:+-i "$STORAGE_BOX_KEY"})
box() { ssh -p 23 "${ssh_opts[@]}" "$STORAGE_BOX" "$@"; }

# Upload under a temporary name, check the checksum on the box, then rename: a broken upload never looks
# like a backup, and retention never counts it.
upload() {
  local file=$1 name local_sum remote_sum
  name=$(basename "$file")
  scp -P 23 "${ssh_opts[@]}" -q "$file" "$STORAGE_BOX:$remote_dir/$name.partial"
  local_sum=$(sha256sum "$file" | cut -d' ' -f1)
  remote_sum=$(box sha256sum "$remote_dir/$name.partial" | cut -d' ' -f1)
  if [ "$local_sum" != "$remote_sum" ]; then
    echo "checksum mismatch on the Storage Box for $name: $local_sum here, $remote_sum there" >&2
    box rm "$remote_dir/$name.partial"
    exit 1
  fi
  box mv "$remote_dir/$name.partial" "$remote_dir/$name"
  echo "uploaded: $STORAGE_BOX:$remote_dir/$name"
}

# pg_dump of the server's own version inside its container, over the local socket (no published port).
compose="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"
out=$(PG_DUMP="$compose exec -T postgres pg_dump" DATABASE_URL="postgresql:///plujka?user=plujka" \
  BACKUP_SCHEMAS=public BACKUP_KEEP=3 scripts/backup_db.sh "$backup_dir")
echo "$out"
dump=$(sed -n 's/^backup written: \(.*\) (.*)$/\1/p' <<<"$out")

# Feedback and configuration. Every variable whose name says it holds a secret keeps its name, not its value.
files="$backup_dir/plujka-files-$(date -u +%Y%m%d-%H%M%S).tar.gz"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/config"
cp -a "${FEEDBACK_DIR:-/mnt/data/feedback}" "$stage/feedback"
sed -E 's/^([A-Za-z0-9_]*(PASSWORD|KEY|KEYS|SECRET|TOKEN)[A-Za-z0-9_]*)=.*/\1=/' .env > "$stage/config/env"
for f in deploy/caddy.env deploy/backup.env; do
  if [ -f "$f" ]; then cp "$f" "$stage/config/"; fi
done
tar -czf "$files.partial" -C "$stage" feedback config
mv "$files.partial" "$files"
ls -1t "$backup_dir"/plujka-files-*.tar.gz | tail -n +4 | xargs -r rm -f --

box mkdir -p "$remote_dir"
upload "$dump"
upload "$files"

# The list first: ssh in a loop over a pipe would read the rest of the list as its own input.
mapfile -t expired < <(box ls -1 "$remote_dir" | python3 scripts/backup_retention.py --daily 7 --weekly 4 --monthly 6)
for old in "${expired[@]}"; do
  [ -n "$old" ] || continue
  box rm "$remote_dir/$old"
  echo "removed from the Storage Box: $old"
done
