#!/usr/bin/env bash
# Production security checks on the server (docs/DEPLOYMENT.md, "Production security checklist"): secrets,
# file permissions, open ports, SSH, system services, TLS and headers, backups. Prints one line per check
# ("ok", "WARN" or "FAIL") and exits 1 when any check fails. Read-only: it changes nothing.
#
# Usage (on the server, as deploy, in /srv/kratka-wyborcza): deploy/security_check.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

failed=0
ok() { printf 'ok    %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; failed=1; }
env_value() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1; }

# --- Secrets and files ---------------------------------------------------------------------------------
if [ ! -f .env ]; then
  fail ".env is missing"
else
  mode=$(stat -c %a .env)
  [ "$mode" = 600 ] && ok ".env is readable by its owner only" || fail ".env has mode $mode, expected 600 (chmod 600 .env)"
  [ "$(stat -c %U .env)" = "$(id -un)" ] && ok ".env belongs to $(id -un)" || fail ".env belongs to $(stat -c %U .env), not $(id -un)"
fi
declare -A defaults=([POSTGRES_PASSWORD]=kratka_wyborcza [MART_READER_PASSWORD]=kratka_wyborcza_mart_reader
                     [OPENSEARCH_INITIAL_ADMIN_PASSWORD]='KratkaWyborczaAdmin123!' [GRAFANA_ADMIN_PASSWORD]=admin)
for name in POSTGRES_PASSWORD MART_READER_PASSWORD OPENSEARCH_INITIAL_ADMIN_PASSWORD GRAFANA_ADMIN_PASSWORD; do
  value=$(env_value "$name")
  if [ -z "$value" ] || [ "$value" = "${defaults[$name]}" ]; then fail "$name is empty or the default"
  elif [ "${#value}" -lt 16 ]; then fail "$name is shorter than 16 characters"
  else ok "$name is set and long"; fi
done
[ -n "$(env_value API_KEYS)" ] && ok "API_KEYS is set (the API is not open to everyone)" \
  || warn "API_KEYS is empty: anyone can call /ask directly, limited only by the rate limit and token budget"
admin=$(env_value ADMIN_API_KEYS) ui=$(env_value KRATKA_WYBORCZA_API_KEY)
if [ -n "$admin" ] && [ -n "$ui" ] && [[ ",$admin," == *",$ui,"* ]]; then
  fail "KRATKA_WYBORCZA_API_KEY is also in ADMIN_API_KEYS: the UI's key must not open the cost panel"
else
  ok "the UI's key is not an admin key"
fi
[ "$(env_value DAILY_TOKEN_LIMIT_PER_CLIENT)" != "" ] && [ "$(env_value DAILY_TOKEN_LIMIT_PER_CLIENT)" != 0 ] \
  && ok "daily token budget per client is on" || warn "DAILY_TOKEN_LIMIT_PER_CLIENT is off: one client can spend without limit"
if [ -f deploy/storagebox_key ]; then
  mode=$(stat -c %a deploy/storagebox_key)
  [ "$mode" = 600 ] && ok "Storage Box key has mode 600" || fail "deploy/storagebox_key has mode $mode, expected 600"
fi
if [ -f deploy/backup.env ]; then
  grep -q '^BACKUP_AGE_RECIPIENT=age1' deploy/backup.env && ok "backups are encrypted (BACKUP_AGE_RECIPIENT)" \
    || fail "deploy/backup.env has no BACKUP_AGE_RECIPIENT: the backup does not run"
else
  fail "deploy/backup.env is missing: no nightly backup"
fi
# The backup's private key belongs in the password manager, never on the server (the drill keeps it in /dev/shm).
# Small files near the repository and the home directory, where a key would be pasted.
keys=$(find . "$HOME" -maxdepth 3 -name .git -prune -o -type f -size -64k -print0 2>/dev/null \
  | xargs -0 -r grep -lE 'AGE-SECRET-KEY-1[0-9A-Z]{50,}' 2>/dev/null | sort -u | head -3 | tr '\n' ' ')
if [ -n "$keys" ]; then
  fail "an age private key (AGE-SECRET-KEY-...) is stored on this server: $keys"
else
  ok "no backup private key on the disk"
fi

# --- Network --------------------------------------------------------------------------------------------
if command -v ss >/dev/null; then
  # Listening TCP sockets outside loopback; only SSH and Caddy may be there.
  open=$(ss -Htln | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | sed -E 's/.*:([0-9]+)$/\1/' | sort -un \
    | grep -vxE '22|80|443' | tr '\n' ' ')
  [ -z "$open" ] && ok "only ports 22, 80 and 443 listen outside loopback" || fail "ports listening outside loopback: $open"
else
  fail "ss is not installed: open ports not checked"
fi
if sshd_config=$(sudo -n sshd -T 2>/dev/null); then
  grep -qx 'passwordauthentication no' <<<"$sshd_config" && ok "SSH refuses passwords" || fail "SSH accepts passwords"
  grep -qxE 'permitrootlogin (no|prohibit-password|without-password)' <<<"$sshd_config" \
    && ok "SSH refuses root passwords" || fail "SSH allows root to log in with a password"
else
  warn "sshd -T needs sudo: SSH settings not checked"
fi

# --- System ---------------------------------------------------------------------------------------------
for unit in fail2ban unattended-upgrades; do
  systemctl is-active --quiet "$unit" 2>/dev/null && ok "$unit is running" || fail "$unit is not running"
done
systemctl is-enabled --quiet kratka-wyborcza-backup.timer 2>/dev/null && ok "kratka-wyborcza-backup.timer is enabled" \
  || fail "kratka-wyborcza-backup.timer is not enabled"
[ -f /var/run/reboot-required ] && warn "a reboot is pending for installed updates" || ok "no reboot pending"

# --- Backups --------------------------------------------------------------------------------------------
stamp=$(sed -n 's/^kratka_wyborcza_backup_last_success_timestamp_seconds //p' /mnt/data/backups/metrics/backup.prom 2>/dev/null)
if [ -z "$stamp" ]; then
  fail "no complete backup recorded (/mnt/data/backups/metrics/backup.prom)"
elif [ $(( $(date +%s) - ${stamp%.*} )) -gt $(( 30 * 3600 )) ]; then
  fail "the last complete backup is $(( ($(date +%s) - ${stamp%.*}) / 3600 )) h old"
else
  ok "last complete backup $(( ($(date +%s) - ${stamp%.*}) / 3600 )) h ago"
fi

# --- TLS and HTTP, from this server through the public address ------------------------------------------
domain=$(sed -n 's/^DOMAIN=//p' deploy/caddy.env 2>/dev/null)
if [ -z "$domain" ]; then
  fail "deploy/caddy.env has no DOMAIN: TLS not checked"
else
  headers=$(curl -fsS -o /dev/null -D - --max-time 15 "https://$domain/" 2>/dev/null | tr -d '\r')
  if [ -z "$headers" ]; then
    fail "https://$domain/ does not answer"
  else
    grep -qi '^strict-transport-security: max-age=' <<<"$headers" && ok "HSTS header is sent" || fail "no HSTS header"
    grep -qi '^server:' <<<"$headers" && fail "the Server header is sent" || ok "no Server header"
  fi
  redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 15 "http://$domain/")
  [[ $redirect == 30[18]\ https://* ]] && ok "http redirects to https" || fail "http://$domain/ answers $redirect, not a redirect to https"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$domain/api/metrics")
  [ "$code" = 404 ] && ok "/api/metrics is not public" || fail "https://$domain/api/metrics answers $code"
  end=$(echo | openssl s_client -connect "$domain:443" -servername "$domain" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  if [ -n "$end" ]; then
    days=$(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
    # Caddy renews 30 days before expiry; under 14 days left, renewal has been failing for two weeks.
    [ "$days" -ge 14 ] && ok "certificate valid for $days more days" || fail "certificate expires in $days days: is Caddy renewing?"
  else
    fail "could not read the certificate of $domain"
  fi
fi

if [ "$failed" = 0 ]; then echo "security check passed"; else echo "security check failed" >&2; fi
exit "$failed"
