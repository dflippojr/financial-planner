# Production signals

How to read the tower's production state without changing it. The box-level
signal sweep (dflippojr/agent-ops#52) and the post-deploy check
(`ops/post-deploy-check.ps1`) use these commands, and an operator can run them
by hand.

Every command here only reads. None restarts, recreates, migrates or writes
anything, and none prints amounts, names, log lines or error text: only
states, counts, exception types, timestamps and SHAs. Do not add a signal that
breaks either rule. In particular, never paste raw `docker logs` output into an
issue; count it with `scripts/prod_signals.py` instead.

Run them in Windows PowerShell 5.1 from the deployed checkout on the tower.
`scripts/prod_signals.py` needs only the standard library, so the host's
`python` runs it. Set these first:

```powershell
$Project = 'financial-planner'
$HealthUrl = 'https://BASEMENT-PC.MAGICDNS-NAME:10443/health/'
$BackupDir = 'D:\financial-planner-backups'   # BACKUP_DIR from production.env
$Config = 'D:\financial-planner-config\production.env'
```

| Signal | Healthy | Fresh as of |
| --- | --- | --- |
| `/health/` status | 200 and `ok` | live |
| Deployed SHA | `X-App-Revision` and every image label match the merged `main` | the last build |
| Container state and `RestartCount` | all four `running`, count not rising | live |
| Tracebacks per container, last 24 h | 0 | live, over a 24 h window |
| Backup status | `state` is `ok` | the last backup run, nightly at 03:30 |
| Balance reconciliation | `counts.mismatch` is 0 | the last SimpleFIN sync, daily at 06:30 |

## Health

```powershell
$r = Invoke-WebRequest -UseBasicParsing $HealthUrl
"$($r.StatusCode) $($r.Headers['X-App-Revision'])"
```

`/health/` runs `SELECT 1` and answers `ok` (200) or `unavailable` (503), with
no error details. On a 503, or when the app is down, `Invoke-WebRequest`
throws instead of printing; that is the signal. Its `X-App-Revision` header is the first 12 hex digits of the
commit the app image was built from, or `unknown` for an image built without
`GIT_SHA` (see [deployment](deployment.md#upgrades-and-migrations)). Nothing
else about the build is exposed.

## Deployed SHA, container state and restarts

```powershell
docker ps -aq --filter "label=com.docker.compose.project=$Project" --filter label=com.docker.compose.oneoff=False |
  ForEach-Object { (docker inspect $_) -join "`n" | ConvertFrom-Json } |
  ForEach-Object { [pscustomobject]@{
      Service      = $_.Config.Labels.'com.docker.compose.service'
      State        = $_.State.Status
      RestartCount = $_.RestartCount
      StartedAt    = $_.State.StartedAt
      Revision     = $_.Config.Labels.'org.opencontainers.image.revision' } } |
  Sort-Object Service | Format-Table -AutoSize
```

Expect `db`, `app`, `backup` and `background`, each `running`, and the one-shot `config-check` `exited`. A `background` that is `created` but never ran means `config-check` refused a setting, such as an invalid `SIMPLEFIN_SYNC_CRON` (#560): `docker compose logs config-check` names it, and so does a second line on `/health/`. The `oneoff` filter leaves out containers that `docker compose run` left behind. `RestartCount`
counts Docker's automatic restarts of that container since it was created, so
compare it between two readings: a count that rises, or a state of
`restarting`, is a crash loop (as in #514). A recreated container starts again
at 0.

`Revision` is the full SHA the `app`, `background` and `backup` images were
built from (`db` uses the stock PostgreSQL image and has none). Compare it with
`git rev-parse origin/main` after `git fetch origin`. A service still on an
older SHA after a deploy was not recreated; the deployment runbook covers that.

## Tracebacks in the last 24 hours

```powershell
foreach ($Service in 'db', 'app', 'backup', 'background') {
  $Id = docker ps -aq --filter "label=com.docker.compose.project=$Project" --filter "label=com.docker.compose.service=$Service" --filter label=com.docker.compose.oneoff=False | Select-Object -First 1
  "$Service $(python scripts\prod_signals.py tracebacks --container $Id --since 24h)"
}
```

Each line is the service and a JSON object such as
`{"tracebacks": 2, "types": {"ValueError": 2}}`. The helper reads both output
streams with `docker logs --since` itself, so log lines stay inside it; it
reports only how many Python tracebacks started in the window and the
exception type that ended each one. A traceback with no readable exception line
counts as `unknown`. `--since` also takes an RFC 3339 time, which the
post-deploy check uses to count only tracebacks since the deploy.

Docker keeps a container's logs only while the container exists, so a
container recreated within the window shows only its own part of it.

## Backup status

```powershell
python scripts\prod_signals.py backup-status (Join-Path $BackupDir 'health\status')
```

`ops/backup/backup.sh` writes this file after each run. The helper prints its
`state`, the fields that set it, `last_success_at`, `restore_check_at` and
whether the off-site copy is configured. It never prints the error text.

| `state` | Meaning |
| --- | --- |
| `ok` | The last run succeeded within 26 hours, and so did the off-site copy if configured. The restore check is not overdue. |
| `error` | `errors` lists which of `last_error`, `offsite_error` and `restore_check_error` is set. Read the text in **Settings → Data** as an operator. |
| `stale` | `stale` lists what is too old: `last_success`, `offsite`, or `restore_check` (over its interval plus a day). |
| `missing` | No status file: no backup has run, or `$BackupDir` is wrong. |

These are the same limits that raise the in-app backup alert
(`finance/backup_health.py`).

## Balance reconciliation

```powershell
$Recon = docker compose --env-file $Config exec -T app python manage.py reconcile_balances --json
$Code = $LASTEXITCODE
($Recon | ConvertFrom-Json) | Select-Object accounts, counts | ConvertTo-Json -Compress
$Code
```

`reconcile_balances` compares each account's transactions with the
bank-reported balances, read-only. The command's own `--json` output has three
fields: `version` (1), `accounts` (how many accounts were reconciled) and
`counts` (windows per state: `ok`, `ok-late`, `mismatch`, `insufficient`). It
also has a `windows` list with every window's account ID and dates. The
command above leaves that list out, so only `accounts` and `counts` reach your
terminal or an issue; never paste the raw output. The exit code is 1 when
`counts.mismatch` is above 0, and 0 otherwise. The `background` container runs
the command after each SimpleFIN sync and raises the in-app alert; see
[Reconciling balances](deployment.md#reconciling-balances) for the Settings → Data view.

## Post-deploy check

`ops/post-deploy-check.ps1` combines these signals into a pass or fail right
after a deploy. The deployment runbook runs it; see
[Upgrades and migrations](deployment.md#upgrades-and-migrations). It fails, with
one line per reason, when:

- `/health/` does not return 200;
- `X-App-Revision`, or the revision label of any image built from this repository (including the one `config-check` last ran, which is `background`'s), is not `origin/main`;
- any of the four containers is not running, or restarts during a 2 minute
  window, or has restarted since the deploy (by `RestartCount`, its start
  time, and for a new container its `start` events in `docker events`, which
  also catch a manual restart);
- Docker's bounded event history no longer reaches a new container's creation,
  so the check cannot count its starts; run it right after `up -d`;
- `/health/` stops returning 200 by the end of the window;
- any container logged a traceback since the deploy started;
- the backup status file is missing or in the `error` state (`stale` is only a
  warning; the nightly alert handles it).

```powershell
.\ops\post-deploy-check.ps1 -ConfigPath $Config -Since $DeployStart
```

It reads `DJANGO_CSRF_TRUSTED_ORIGINS` (for the health URL) and `BACKUP_DIR`
from the env file and nothing else. Pass `-HealthUrl`, `-StatusPath`,
`-ExpectedSha`, `-ProjectName` or `-StableSeconds` to override a default.
`-SkipRevision` is only for a rollback to a release older than #537, whose
images record no SHA.
Without `-Since`, it dates the deploy from when the `app` and `background`
containers were created.
