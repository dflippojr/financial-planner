#!/bin/sh
# Print an exact row count for every table in the public schema, one
# "table<TAB>count" line each, sorted by table name. Compare the output taken
# before and after a dump-and-restore move (docs/deployment.md); it holds only
# table names and counts, never row contents.
set -eu

for variable in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_HOST; do
  eval "value=\${$variable:-}"
  if [ -z "$value" ]; then
    echo "$variable must be set" >&2
    exit 2
  fi
done

export PGPASSWORD=$POSTGRES_PASSWORD
psql \
  --host "$POSTGRES_HOST" \
  --port "${POSTGRES_PORT:-5432}" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --no-psqlrc \
  --tuples-only \
  --no-align \
  --field-separator "$(printf '\t')" \
  --set ON_ERROR_STOP=1 \
  --command "
SELECT c.relname,
       (xpath('/row/n/text()',
              query_to_xml(format('SELECT count(*) AS n FROM public.%I', c.relname),
                           false, true, '')))[1]::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY c.relname"
