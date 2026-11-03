#!/usr/bin/env bash
# Nightly backup of the public schema (question log tables, saved views, token usage, legacy kbw_* tables)
# to the Storage Box, with retention 7 daily / 4 weekly / 6 monthly (scripts/backup_retention.py).
# The mart is not backed up: CI publishes it (release mart-latest) and deploy/deploy_mart.sh restores it.
#
# Run by plujka-backup.timer (deploy/systemd/) as deploy, from /srv/plujka, with deploy/backup.env:
#   STORAGE_BOX=u123456@u123456.your-storagebox.de   user@host (terraform output storage_box_username / _server)
#   STORAGE_BOX_KEY=/srv/plujka/deploy/storagebox_key  private key installed on the box (docs/DEPLOYMENT.md)
#   STORAGE_BOX_DIR=plujka                              directory on the box (default plujka)
#   BACKUP_DIR=/mnt/data/backups                        local copies, the newest 3 are kept (default)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${STORAGE_BOX:?set STORAGE_BOX=user@host of the Storage Box}"
remote_dir="${STORAGE_BOX_DIR:-plujka}"
ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new ${STORAGE_BOX_KEY:+-i "$STORAGE_BOX_KEY"})
box() { ssh -p 23 "${ssh_opts[@]}" "$STORAGE_BOX" "$@"; }

# pg_dump of the server's own version inside its container, over the local socket (no published port).
compose="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"
out=$(PG_DUMP="$compose exec -T postgres pg_dump" DATABASE_URL="postgresql:///plujka?user=plujka" \
  BACKUP_SCHEMAS=public BACKUP_KEEP=3 scripts/backup_db.sh "${BACKUP_DIR:-/mnt/data/backups}")
echo "$out"
dump=$(sed -n 's/^backup written: \(.*\) (.*)$/\1/p' <<<"$out")
name=$(basename "$dump")

# Upload under a temporary name, check the checksum on the box, then rename: a broken upload never looks
# like a backup, and retention below never counts it.
box mkdir -p "$remote_dir"
scp -P 23 "${ssh_opts[@]}" -q "$dump" "$STORAGE_BOX:$remote_dir/$name.partial"
local_sum=$(sha256sum "$dump" | cut -d' ' -f1)
remote_sum=$(box sha256sum "$remote_dir/$name.partial" | cut -d' ' -f1)
if [ "$local_sum" != "$remote_sum" ]; then
  echo "checksum mismatch on the Storage Box for $name: $local_sum here, $remote_sum there" >&2
  box rm "$remote_dir/$name.partial"
  exit 1
fi
box mv "$remote_dir/$name.partial" "$remote_dir/$name"
echo "uploaded: $STORAGE_BOX:$remote_dir/$name"

# The list first: ssh in a loop over a pipe would read the rest of the list as its own input.
mapfile -t expired < <(box ls -1 "$remote_dir" | python3 scripts/backup_retention.py --daily 7 --weekly 4 --monthly 6)
for old in "${expired[@]}"; do
  [ -n "$old" ] || continue
  box rm "$remote_dir/$old"
  echo "removed from the Storage Box: $old"
done
