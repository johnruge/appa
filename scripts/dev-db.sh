#!/usr/bin/env bash
# Local development Postgres, kept inside the repo (data/pg, gitignored).
# Listens on 127.0.0.1 only and runs only while you start it; nothing is installed as a service.
#
#   scripts/dev-db.sh init     create the cluster, roles, the appa + appa_test databases, and .env
#   scripts/dev-db.sh start | stop | status | psql
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@17/bin}"
PGDATA="$ROOT/data/pg"
PORT="${APPA_PG_PORT:-5433}"   # not 5432, to avoid clashing with any other Postgres
ENV_FILE="$ROOT/.env"

export LC_ALL="${LC_ALL:-en_US.UTF-8}"   # macOS: postgres refuses to start without a valid locale
pg() { "$PGBIN/$1" "${@:2}"; }

start() {
  if pg pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then echo "already running on 127.0.0.1:$PORT"; return; fi
  pg pg_ctl -D "$PGDATA" -l "$ROOT/data/pg.log" -o "-p $PORT -k $PGDATA" -w start >/dev/null
  echo "postgres running on 127.0.0.1:$PORT"
}

init() {
  if [ -d "$PGDATA" ]; then echo "cluster already exists at $PGDATA"; start; return; fi
  mkdir -p "$ROOT/data" && chmod 700 "$ROOT/data"
  # Superuser: your OS user, local socket only (peer). TCP always needs a password (scram).
  pg initdb -D "$PGDATA" --auth-local=peer --auth-host=scram-sha-256 -U postgres --encoding=UTF8 >/dev/null
  # initdb's peer auth maps OS user -> same db user; allow your OS user to act as "postgres".
  printf 'local all postgres peer map=dev\nlocal all all peer\nhost all all 127.0.0.1/32 scram-sha-256\nhost all all ::1/128 scram-sha-256\n' > "$PGDATA/pg_hba.conf"
  printf 'dev %s postgres\n' "$(whoami)" > "$PGDATA/pg_ident.conf"
  {
    echo "listen_addresses = '127.0.0.1'"
    echo "password_encryption = scram-sha-256"
  } >> "$PGDATA/postgresql.conf"
  start

  owner_pw="$(openssl rand -hex 24)"; app_pw="$(openssl rand -hex 24)"
  for db in appa appa_test; do
    pg psql -h "$PGDATA" -p "$PORT" -U postgres -d postgres -q \
      -v db="$db" -v owner_pw="'$owner_pw'" -v app_pw="'$app_pw'" -f "$ROOT/deploy/postgres/roles.sql"
  done

  umask 077
  cat > "$ENV_FILE" <<EOF
# Local development secrets. Gitignored. Never commit.
APPA_DATABASE_URL=postgresql+psycopg://appa_app:$app_pw@127.0.0.1:$PORT/appa
APPA_MIGRATIONS_DATABASE_URL=postgresql+psycopg://appa_owner:$owner_pw@127.0.0.1:$PORT/appa
APPA_TEST_DATABASE_URL=postgresql+psycopg://appa_app:$app_pw@127.0.0.1:$PORT/appa_test
APPA_TEST_MIGRATIONS_DATABASE_URL=postgresql+psycopg://appa_owner:$owner_pw@127.0.0.1:$PORT/appa_test

# Model provider. Get a key at https://openrouter.ai/keys, then uncomment:
# OPENROUTER_API_KEY=
EOF
  echo "wrote $ENV_FILE (mode 600)"
}

case "${1:-}" in
  init) init ;;
  start) start ;;
  stop) pg pg_ctl -D "$PGDATA" -m fast stop ;;
  status) pg pg_ctl -D "$PGDATA" status ;;
  psql) pg psql -h "$PGDATA" -p "$PORT" -U postgres -d "${2:-appa}" ;;
  *) echo "usage: $0 init|start|stop|status|psql [db]"; exit 1 ;;
esac
