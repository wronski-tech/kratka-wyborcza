#!/usr/bin/env bash
# Update the running stack to a commit whose images CI pushed to GHCR (workflow Images), then run the
# smoke test. Usage (on the server, in /srv/kratka-wyborcza): deploy/update_app.sh <commit-sha>
set -euo pipefail
# The whole script is one compound command: bash reads it completely before `git checkout` replaces it.
{
cd "$(dirname "$0")/.."
sha="${1:?usage: update_app.sh <commit-sha>}"
dc=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)

git fetch --quiet origin
git checkout --quiet --detach "$sha"
# The tag stays in .env so that restarts of kratka-wyborcza.service keep the same images.
if grep -q '^KRATKA_WYBORCZA_IMAGE_TAG=' .env; then sed -i "s/^KRATKA_WYBORCZA_IMAGE_TAG=.*/KRATKA_WYBORCZA_IMAGE_TAG=$sha/" .env
else echo "KRATKA_WYBORCZA_IMAGE_TAG=$sha" >> .env; fi
"${dc[@]}" pull --quiet api streamlit loader
"${dc[@]}" up -d --remove-orphans

set -a
# shellcheck disable=SC1091
source .env
source deploy/caddy.env
set +a
for _ in $(seq 1 30); do curl -fsS "https://$DOMAIN/api/health" >/dev/null 2>&1 && break; sleep 5; done
deploy/smoke_test.sh "https://$DOMAIN" "${KRATKA_WYBORCZA_API_KEY:-}"
exit 0
}
