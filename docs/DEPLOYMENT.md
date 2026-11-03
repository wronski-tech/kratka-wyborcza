# Deployment and operations

How to run Plujka beyond a laptop: access control, a shared rate limit, backups, telemetry
dashboards and how the staged data is versioned. Configuration variables are listed in
[MART.md](MART.md#configuration).

## Checklist

1. `PLUJKA_ENV=production` — the API refuses to start with the default database passwords or a
   disabled rate limit.
2. Strong owner password (`DATABASE_URL`) and reader password (`MART_READER_PASSWORD`); rebuild the
   mart once so the build sets the reader password.
3. `API_KEYS` when the API is not meant to be public; give the UI one of them in `PLUJKA_API_KEY`.
4. `REDIS_URL` when more than one API worker or replica serves `/ask`, and
   `DAILY_TOKEN_LIMIT_PER_CLIENT` to cap what one key or IP can spend on OpenAI per day.
5. A scheduled backup (`scripts/backup_db.sh`) and a restore test.
6. `OPENAI_API_KEY`; without it every question falls back to the legacy Sejm templates.

## Production on Hetzner

The production setup is one server per environment, created by Terraform
([infra/hetzner/README.md](../infra/hetzner/README.md)), running `docker compose` with
[`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml):

```text
Internet ─ 80/443 ─ Caddy (TLS) ─┬─ /api/* ─ api (uvicorn, 2 workers, --root-path /api) ─┬─ postgres (/mnt/data/postgres)
                                 └─ /*     ─ streamlit ─────────────────────────────────┘  opensearch, redis
```

Only Caddy publishes ports; PostgreSQL, OpenSearch, the API and Streamlit are reachable inside the
compose network only, and the Hetzner firewall allows SSH from admin networks only. All state is on
the data volume `/mnt/data`, so rescaling or replacing the server keeps it.

Step by step:

1. `terraform apply` in `infra/hetzner` (server, volume, firewall, Storage Box).
2. DNS A/AAAA records of the domain → outputs `ipv4` / `ipv6`.
3. `/srv/plujka/.env` from [`deploy/env.production.example`](../deploy/env.production.example)
   (strong `POSTGRES_PASSWORD` and `MART_READER_PASSWORD`, `API_KEYS` and the UI's `PLUJKA_API_KEY`,
   `OPENAI_API_KEY`, `REDIS_URL`), then `sudo systemctl start plujka`.
4. `deploy/deploy_mart.sh` restores the mart CI published (below).
5. Check `https://<domain>/api/health?details=true` (`data_ready: true`) and ask one question in the UI.

Updating the code: the Deploy workflow, or by hand `deploy/update_app.sh <commit-sha>` (images of that
commit from GHCR, then the smoke test). `deploy/smoke_test.sh https://<domain> [api-key]` checks a
deployment at any time.

### Mart deployment

The server never builds the mart. On every push to `main` the CI integration job builds it with every
consistency check, dumps the `mart` schema (`pg_dump -Fc`, about 260 MB) and uploads `mart.dump` and
`mart.dump.sha256` to the pre-release `mart-latest`. `deploy/deploy_mart.sh [tag]`:

1. downloads the dump and verifies its checksum;
2. recreates the mart database not in use (`plujka_mart_a` / `plujka_mart_b`; the owner database
   `plujka` keeps the question log), creates `pg_trgm` and restores tables and data (about 20 s);
3. runs `build_mart.py --derived-only` in the loader image (persons, views, seat check and the
   read-only role; about 5 minutes);
4. switches `MART_DB` in `.env`, restarts the API and waits for `data_ready`.

Questions keep using the old mart until the switch; the old database stays for rollback (set
`MART_DB` back, `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d api`).
A full `pg_restore` of the dump is not used: it refreshes the materialized views without statistics
and takes far longer than the derived rebuild.

Rate limits behind the UI: Streamlit calls the API server-side with `PLUJKA_API_KEY`, so all UI users
share one rate-limit and token bucket (that key's). Size `ASK_RATE_LIMIT_PER_MINUTE` and
`DAILY_TOKEN_LIMIT_PER_CLIENT` for the whole UI, or give API clients their own keys.
`SESSION_QUESTION_LIMIT` is what separates UI users: the UI sends a random `X-Session-Id` per
browser session, counted per key or IP and UTC day. A client chooses that id itself (a reload starts
a new session), so it limits ordinary use, not abuse; the per-key limits above stay the protection.

## Access: API keys

`API_KEYS` is a comma-separated list. When it is set, `POST /ask` and `POST /feedback` return 401
without a matching `X-API-Key` header; `GET /health`, `/docs` and `POST /question-hints` stay open.
The rate limit then counts per key instead of per IP. Rotate a key by adding the new one, switching
clients, then removing the old one (restart the API after each change).

```bash
curl -s localhost:8000/ask -H 'Content-Type: application/json' -H 'X-API-Key: <key>' \
  -d '{"question": "Frekwencja w wyborach do Sejmu 2023"}'
```

## Rate limit shared through Redis

Without `REDIS_URL` each API process keeps its own sliding window, so N workers allow up to N times
`ASK_RATE_LIMIT_PER_MINUTE`. With `REDIS_URL` every process increments one counter per client and
minute in Redis (`plujka:ratelimit:<key>:<minute>`, expiring after two minutes). If Redis is
unreachable the process logs `redis rate limit unavailable` and falls back to its own window, so
an outage of Redis never blocks questions.

```bash
REDIS_URL=redis://redis:6379/0 docker compose --profile redis up -d
```

The bundled Redis keeps nothing on disk (counters are disposable).

## Daily token budget

`DAILY_TOKEN_LIMIT_PER_CLIENT` caps the OpenAI tokens (prompt + completion, as reported by the API)
that one API key, or one client IP without keys, may use per UTC day. A request is refused with 429
once the day's total is reached; the request that crosses the limit still completes. Counters live
in Redis (`plujka:tokens:<key>:<day>`) when `REDIS_URL` is set, otherwise per process. Watch real
usage in the cost panel (below) before choosing a value.

## LLM provider

All model calls (SQL, answers, clarification queries, embeddings, speech) go through
`api/services/llm_client.py`. To keep the data processing in the EU, use Azure OpenAI with a resource in an EU
region (for example Sweden Central or France Central) and deployments of the models:

```bash
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
OPENAI_API_KEY=<the resource's key>
OPENAI_CHAT_MODEL=<chat deployment>          # e.g. a gpt-4o-mini deployment
OPENAI_REPAIR_MODEL=<repair deployment>      # e.g. a gpt-4o deployment
OPENAI_EMBEDDING_MODEL=<embedding deployment>
OPENAI_TRANSCRIBE_MODEL=<transcription deployment>
LLM_PRICES_USD_PER_1M={"<chat deployment>": [0.15, 0.075, 0.6], "<repair deployment>": [2.5, 1.25, 10.0]}
```

The cost panel and the per-question limit look prices up by these names, so list the deployments in
`LLM_PRICES_USD_PER_1M`. An OpenAI-compatible endpoint (another provider or a self-hosted server) works with
`LLM_PROVIDER=openai` and `OPENAI_BASE_URL`; it must support JSON responses (`response_format`) and embeddings.
The API refuses to start with an unknown `LLM_PROVIDER` or `azure` without an endpoint.

## Cost panel

Every `/ask` (also the MCP `ask` tool) adds its OpenAI tokens to the table `token_usage`: one row per UTC
day, client and model (prompt, cached and completion tokens, questions). The client is `key <hash>`, the
first 8 hex digits of the API key's SHA-256 (the key itself is never stored), or `no key`. `GET /costs?days=30`
returns the rows with a USD estimate from `LLM_PRICES_USD_PER_1M`; it needs one of `ADMIN_API_KEYS` (403
when `API_KEYS` is set and no admin key is configured). The UI shows it at `?panel=koszty`, where the admin
key is typed in; do not give the UI an admin key. Speech transcription and embeddings are not counted.

`QUESTION_COST_LIMIT_USD` (default 0.10) caps one question: every model call has an output ceiling
(`max_tokens`), and before each call the cost so far plus that call priced from above (prompt length / 3
tokens, full output) must stay within the limit. Otherwise the call is skipped: no repair (status
`failed`, `cost_limited` in the meta), clarification options without ready queries, or the deterministic
summary instead of the model's text. The default leaves room for a repair with the largest prompt (about
57k characters with the repair note, at most 0.07 USD with gpt-4o-mini and gpt-4o): below about 0.07 a
repair of a question that describes many relations is skipped. A model without a price in `LLM_PRICES_USD_PER_1M` counts as free,
so add a price when you change `OPENAI_CHAT_MODEL` or `OPENAI_REPAIR_MODEL`. The question log has
`cost_usd` per question.

## Backups

`scripts/backup_db.sh` writes a compressed `pg_dump` (custom format) to `backups/` (gitignored) and
keeps the newest `BACKUP_KEEP` dumps (default 7):

```bash
DATABASE_URL=postgresql://plujka:<password>@db:5432/plujka scripts/backup_db.sh /var/backups/plujka
# Only the non-rebuildable part (question log tables, legacy kbw_facts); mart comes from parquet:
BACKUP_SCHEMAS=public scripts/backup_db.sh
```

Restore into an empty database, then rebuild or check the mart:

```bash
pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" backups/plujka-YYYYmmdd-HHMMSS.dump
DATABASE_URL=... python scripts/build_mart.py   # recreates plujka_mart_reader and its grants
```

A dump of chosen schemas (`BACKUP_SCHEMAS`) also carries the extensions (`vector`, `pg_trgm`) its tables
and indexes need, so it restores into an empty database. To check a restore against its source (a restore
drill, or CI after every push), compare tables, indexes, functions, sequences and row counts:

```bash
python scripts/check_restore.py "$SOURCE_URL" "$RESTORED_URL" --schemas public
```

What a database backup does not cover: thumbs-down feedback (`data/feedback/feedback.jsonl` on the
API host), the server configuration and the OpenSearch indexes (`question_logs`, `sql_examples`). On the
server the nightly backup below archives feedback and configuration next to the dump. The OpenSearch
indexes need no snapshots: the examples index is rebuilt from `sql_examples.jsonl` at startup, and the
question log is kept in Postgres too (`public.question_log`, in the dump), so after a restore or a lost
OpenSearch volume rebuild `question_logs` from it (documents keep their Postgres id, so a rerun adds no
duplicates):

```bash
docker compose exec api python scripts/reindex_question_log.py [--since 2026-11-01]
```

### Nightly backup to the Storage Box

On the Hetzner server `plujka-backup.timer` (`deploy/systemd/`, installed by cloud-init) runs
`deploy/backup_storagebox.sh` at 03:15 UTC. It uploads two files to the Storage Box over SSH (port 23), each
checked by SHA-256 on the box and only then renamed to its final name:

- `plujka-YYYYmmdd-HHMMSS.dump`: the `public` schema, dumped by the server's own `pg_dump` inside the
  postgres container;
- `plujka-files-YYYYmmdd-HHMMSS.tar.gz`: `feedback/` (the API's feedback directory: thumbs-down answers and
  person reports) and `config/`: `.env` with the values of every `*PASSWORD*`, `*KEY*`, `*SECRET*` and
  `*TOKEN*` variable removed (limits, `MART_DB`, image tag and URLs stay), `deploy/caddy.env` and
  `deploy/backup.env`. Secrets are in no backup: keep them in a password manager.

Retention on the box is 7 daily, 4 weekly and 6 monthly backups of each kind (`scripts/backup_retention.py`:
the newest backup of each period, counting only periods that have one, so nothing is deleted when backups
stop); the newest 3 of each stay in `/mnt/data/backups`. The mart is not part of
it: CI publishes it (release `mart-latest`) and `deploy/deploy_mart.sh` restores it.

Once per server, as `deploy` in `/srv/plujka` (user and host: `terraform output storage_box_username` and
`storage_box_server`; the first command asks for the Storage Box password):

```bash
ssh-keygen -t ed25519 -N '' -f deploy/storagebox_key
ssh -p 23 u123456@u123456.your-storagebox.de install-ssh-key < deploy/storagebox_key.pub
printf 'STORAGE_BOX=u123456@u123456.your-storagebox.de\nSTORAGE_BOX_KEY=/srv/plujka/deploy/storagebox_key\n' > deploy/backup.env
sudo systemctl start plujka-backup.service && journalctl -u plujka-backup.service -n 20
```

The timer does nothing until `deploy/backup.env` exists. On a server created before the timer existed,
install it with `sudo cp deploy/systemd/plujka-backup.* /etc/systemd/system/ && sudo systemctl daemon-reload
&& sudo systemctl enable --now plujka-backup.timer`. Restore a backup from the box with
`scp -P 23 -i deploy/storagebox_key u123456@u123456.your-storagebox.de:plujka/<name>.dump .`, then as above;
for the files, `tar -xzf plujka-files-<stamp>.tar.gz`, copy `feedback/*` to `/mnt/data/feedback/` and use
`config/env` as `.env` with the secrets filled in.

### Restore drill

Once a month (the workflow `restore-drill.yml` opens an issue with this checklist on the 1st) restore the
newest backups on a new, throwaway server and check the service answers from them. The drill needs the
Hetzner token, the Storage Box password and the secrets from the password manager, so a person runs it;
it takes about 30 minutes.

1. Server: from `infra/hetzner`, with the staging variables, its own name and state key (no Storage Box of
   its own):

   ```bash
   terraform init -reconfigure -backend-config=backend-production.hcl -backend-config=key=drill/terraform.tfstate
   terraform apply -var-file=staging.tfvars -var environment=drill -var domain=drill.plujka.example.org
   ```

   Point the DNS record of that domain at the output `ipv4`.
2. Secrets and mart, as for any server (`infra/hetzner/README.md`, steps 6–7): `.env` from the password
   manager, `sudo systemctl start plujka`, `deploy/deploy_mart.sh`.
3. Storage Box access, as `deploy` in `/srv/plujka`: a new key for this server and `deploy/drill.env`
   (not `backup.env`: the drill server must not run the nightly backup):

   ```bash
   ssh-keygen -t ed25519 -N '' -f deploy/storagebox_key
   ssh -p 23 u123456@u123456.your-storagebox.de install-ssh-key < deploy/storagebox_key.pub
   printf 'STORAGE_BOX=u123456@u123456.your-storagebox.de\nSTORAGE_BOX_KEY=/srv/plujka/deploy/storagebox_key\n' > deploy/drill.env
   ```

4. The drill: `deploy/restore_drill.sh https://drill.plujka.example.org`. It refuses a server with
   questions logged before today (production), takes the newest dump and files archive, fails when either
   is older than 36 hours or its checksum differs from the box, restores `public` with the API stopped,
   copies the feedback, lists `.env` variables that differ from the backed-up configuration, rebuilds the
   OpenSearch question log, prints what came back (logged questions and the newest one, saved views,
   feedback lines; an empty question log fails) and ends with the smoke test.
5. Clean up: remove this server's key from `.ssh/authorized_keys` on the box (fetch it with `scp -P 23`,
   delete the line ending in `deploy@plujka-drill`, upload it back), then `terraform destroy -var-file=staging.tfvars -var environment=drill -var
   domain=drill.plujka.example.org` and the DNS record. Note the result (backup age, counts, anything
   that failed) in the issue and close it.

## Load test

`scripts/load_test.py` starts the API in its own process with a fake OpenAI server (`scripts/fake_openai.py`)
and sends concurrent questions (each worded anew, so the SQL cache does not answer them) and browse
requests; it prints p50/p95/max per endpoint and fails on any failed request or a p95 above
`--max-p95-ms`. CI runs it on the built mart (300 requests, 16 concurrent). Rate limits are off for the
run; the model's own latency is not included unless `--model-delay-ms` adds it.

```bash
DATABASE_URL=... MART_DATABASE_URL=... python3 scripts/load_test.py --requests 300 --concurrency 16
```

Measured in a 4-vCPU orb with one API process: a question alone takes about 230 ms besides the model,
but 16 concurrent ones about 1.9 s each (p95 2.9 s), so one process answers roughly 8 questions per second:
the work around the model (entity matching, the SQL guard, the answer check) is CPU-bound and one Python
process uses one core. Production runs two workers (`--workers 2` in `deploy/docker-compose.prod.yml`);
add workers before adding threads, and add the model's latency (seconds) to these numbers.

## Telemetry dashboards

Every `/ask` writes a row to `public.question_log` (`document` holds the fields below, plus the
question's embedding) and the same document to the OpenSearch index `question_logs` with `created_at`,
`status` (`first_pass`, `repaired`, `cached`, `clarify`, `rejected`, `failed`, `skipped`,
`template`), `latency_ms`, `row_count`, `answer_source`, `prompt_tokens`, `completion_tokens`,
`error`, `detected_intent`, `question` and `sql`.

```bash
docker compose --profile observability up -d opensearch-dashboards   # http://localhost:5601
```

In Dashboards create an index pattern `question_logs` with time field `created_at`, then for
example:

- share of answered questions: pie of `status`;
- questions that failed: Discover with `status:(failed OR rejected OR skipped)`, columns
  `question`, `error`, `sql` — the best source for new curated examples (`scripts/add_sql_example.py`);
- cost: sum of `prompt_tokens` and `completion_tokens` per day;
- latency: 50th/95th percentile of `latency_ms` per day.

Without Dashboards, ask Postgres, e.g. statuses of the last week:
`SELECT document->>'status', count(*) FROM question_log WHERE created_at > now() - interval '7 days' GROUP BY 1`.

The bundled OpenSearch runs without its security plugin; do not expose ports 9200 or 5601 publicly.

## CI, images and releases

`.github/workflows/ci.yml` runs four jobs: unit tests; lint (ruff, mypy, pip-audit, offline links
between Markdown files); integration on PostgreSQL 16 (DB tests, mart build from the committed parquet
or the cached dump, curated SQL examples, size report, `mart.dump` to the `mart-latest` release on
main); and `compose`, which validates every Compose file and profile, builds all images, starts
postgres, OpenSearch, the API and the UI, and checks `/health`, one `/ask` request and the Streamlit
health endpoint. Dependabot opens weekly update pull requests (pip, Docker, Actions, Terraform).

`images.yml` pushes `ghcr.io/wronski-tech/plujka-{api,streamlit,loader}` tagged with the commit SHA
(and `latest` on main) and prints a Trivy report of fixed HIGH/CRITICAL vulnerabilities. New GHCR
packages are private: make them public once (package settings) or `docker login ghcr.io` on the server
with a `read:packages` token. `deploy.yml` then runs `deploy/update_app.sh <sha>` on staging over SSH
(images of that commit, `docker compose up -d`, `deploy/smoke_test.sh`); production and mart
deployments are started by hand (Run workflow) and wait for the reviewers of the `production`
environment. Each environment needs `DEPLOY_HOST`, `DEPLOY_KNOWN_HOSTS` (variables) and
`DEPLOY_SSH_KEY` (secret); without `DEPLOY_HOST` the job deploys nothing.

Releases: move the `[Unreleased]` section of `CHANGELOG.md` to `## [x.y.z] - YYYY-MM-DD`, commit,
then `git tag vx.y.z && git push origin vx.y.z`. `.github/workflows/release.yml` builds the images
and publishes a GitHub release with that CHANGELOG section as notes (it fails when the section is
missing).

## Source files changed by PKW

`data/source_manifest.json` records every file of the KBW mirror (URL, size, SHA-256, ETag,
Last-Modified). `.github/workflows/sources.yml` runs `scripts/check_sources.py` every Monday: HEAD
requests only, compared with the manifest. When files changed, a second job downloads only those
files (the mirror is not in Git), re-stages them, builds the whole mart on PostgreSQL 16 with every
check, runs the curated examples, and opens the pull request `sources/kbw-refresh` with the new
parquet and manifest. The pull request is a draft and the run fails when the build or the examples
fail; the run also fails when files disappeared. Pull requests opened with `GITHUB_TOKEN` do not
start the CI workflow, so the job runs the build itself; enable *Settings → Actions → General →
Allow GitHub Actions to create and approve pull requests* once. A republished ZIP whose member files
were renamed leaves the old members' parquet behind; remove it in the pull request. With the
mirror locally:

```bash
python3 scripts/check_sources.py --update             # download changed files, re-stage, update manifest
DATABASE_URL=... python3 scripts/check_sources.py --update --rebuild   # ... and rebuild those family-years
python3 scripts/check_sources.py --record             # after downloading new elections
```

Then run `scripts/add_sql_example.py --check`, compare the national results with PKW, and commit the
changed parquet and manifest. Files that disappeared are kept; check the KBW page by hand.

## Staged data in Git

The staged parquet (`data/kbw_stage_parquet`, `data/pkw_stage_parquet`, about 320 MB in some 15,000 files) is committed
as plain Git files, not Git LFS. CI checks out the repository and builds the whole mart from these
files on every push, and GitHub's free LFS bandwidth (1 GB per month) would be used up by those
checkouts within days. No parquet file is near GitHub's 100 MB file limit. If the data grows past a
few hundred MB, move it to release assets (one archive per election family) and download it in CI
and the loader instead.
