# Basement PC deployment

This runbook deploys the first release to the Windows basement PC with Docker Desktop's WSL2 backend. The application is available only through the existing Tailscale tailnet: Docker publishes the application on Windows loopback, and `tailscale serve` terminates HTTPS and proxies to that loopback port. PostgreSQL is not published to the host or network.

## Storage and network boundaries

| Item | Location | Retention or exposure |
|---|---|---|
| Application source and synthetic fixture | Git checkout | Code only; never add populated environment files, CSV exports, database files, or dumps. |
| Secrets and per-machine settings | A protected file outside the checkout, such as `D:/financial-planner-config/production.env` | Keep until rotated; restrict its Windows permissions to the operator account. |
| PostgreSQL data | Docker named volume selected by `POSTGRES_VOLUME_NAME` | Durable across container replacement; never commit or manually edit it. |
| Receipt files | Docker named volume selected by `RECEIPTS_VOLUME_NAME` (`RECEIPTS_DIR=/receipts`) | Durable across container replacement. Served only through access-checked views, never as static or media files. Deleting a receipt drops the database row immediately; the file is removed within about two days. Nightly backups archive this directory next to the database dump and keep deleted receipts' files until those copies rotate out. |
| Logical backups | `BACKUP_DIR` on the second local disk | Keep the 14 newest nightly dumps and 8 newest Sunday weekly copies. |
| Source CSV exports | A private folder outside the checkout | The application discards an uploaded source after a successful import; the operator should remove the original export when no longer needed. |
| Staged CSV uploads | A tmpfs (memory-backed) mount at `/run/csv-staging` inside the app container | Never written to disk. Each upload expires within an hour, is deleted on cancel, and is discarded whenever the container stops or restarts. |
| Web listener | `127.0.0.1:APP_PORT` on the basement PC | No direct LAN listener. Tailscale Serve exposes HTTPS only inside the tailnet. |

Django still requires a valid signed-in session after a request reaches the app. Tailscale controls which devices can reach the service; it does not replace application authentication or private/household authorization. Do not use Tailscale Funnel, a router port-forward, or a `0.0.0.0` host port.

## First deployment

Prerequisites are Docker Desktop configured to use WSL2 and start when Windows signs in, Tailscale connected to the intended tailnet, Git, and a second local disk for backups. Run these commands in PowerShell from the checkout.

1. Create protected configuration and backup directories outside the checkout:

   ```powershell
   New-Item -ItemType Directory -Force D:\financial-planner-config
   New-Item -ItemType Directory -Force E:\financial-planner-backups
   Copy-Item .env.example D:\financial-planner-config\production.env
   ```

2. Edit `D:\financial-planner-config\production.env`. Replace both example secrets, use the basement PC's MagicDNS name for `DJANGO_ALLOWED_HOSTS`, use the matching `https://` URL for `DJANGO_CSRF_TRUSTED_ORIGINS`, and set `BACKUP_DIR=E:/financial-planner-backups`. Generate the Django secret and database password independently. One PowerShell option for each is:

   ```powershell
   [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
   ```

   Do not reuse the example values. Keep `DJANGO_SECURE_COOKIES` and redirect behavior at their production defaults in `compose.yml`. Leave `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` empty until you finish the Google Cloud Console steps below; password-only sign-in works without them.

   Set `FIELD_ENCRYPTION_KEY` to a Fernet key. Losing it means every member must reconnect SimpleFIN; backups keep only ciphertext. Generate one:

   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   `SIMPLEFIN_SYNC_CRON` defaults to `30 6 * * *` (06:30 in `TZ`). It uses the same five-field cron shape as `BACKUP_CRON`. Optional `OPERATOR_USERNAMES` names who receive backup health alerts. Optional `OFFSITE_RCLONE_REMOTE` and `OFFSITE_AGE_RECIPIENT` enable the encrypted off-site copy; see Backups below.

3. Validate, build, migrate, and start the stack:

   ```powershell
   $Config = 'D:\financial-planner-config\production.env'
   docker compose --env-file $Config config --quiet
   docker compose --env-file $Config up -d --build
   docker compose --env-file $Config ps
   ```

   The app entrypoint runs `python manage.py migrate --noinput` before Gunicorn starts. Both the application and PostgreSQL should report `healthy`; the backup, SimpleFIN sync, and AI job runners should report `Up`.

4. Configure persistent tailnet-only HTTPS using the current Tailscale CLI syntax:

   ```powershell
   tailscale serve --bg http://127.0.0.1:8000
   tailscale serve status
   ```

   If `APP_PORT` is not 8000, use its value in the target URL. Tailscale Serve accepts only loopback HTTP proxy targets and supplies `X-Forwarded-Proto` and `X-Forwarded-For`. Django trusts that proxy header, redirects other HTTP requests to HTTPS, and uses secure session and CSRF cookies. Compose sets `TRUST_PROXY_FORWARDED_FOR=true` for the app service because the published port is loopback-only; sign-in and session logs then store the right-most `X-Forwarded-For` address (the hop Serve added). Leave that setting off anywhere the app port is reachable without that proxy, or clients can spoof the left-most address. Open the HTTPS MagicDNS URL shown by `tailscale serve status`. The command uses Serve, not Funnel, so it does not intentionally expose the app to the public internet. See the [Tailscale Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

   **When the PC already runs other services**, check before choosing ports:

   - Run `netstat -ano | findstr LISTENING` and pick an unused loopback `APP_PORT`. Another container, such as Portainer, often holds 8000.
   - Run `tailscale serve status` first. The command above maps the default HTTPS port (443). If 443 already proxies another app, running it replaces that mapping. Add the app on its own HTTPS port instead, one that is neither listed in `tailscale serve status` nor listening on the tailnet address:

     ```powershell
     tailscale serve --bg --https=10443 http://127.0.0.1:8210
     ```

   - With a non-default HTTPS port, `DJANGO_CSRF_TRUSTED_ORIGINS` must include it, for example `https://basement-pc.example-tailnet.ts.net:10443`, or every sign-in and form post fails the CSRF check. `DJANGO_ALLOWED_HOSTS` stays the bare MagicDNS name, without a port. Passkeys use that hostname as the WebAuthn relying party ID and the CSRF origins (including `:10443`) as the allowed origin list, so members must open the same HTTPS MagicDNS URL to register or use a passkey.
   - To remove only this app's route later, run `tailscale serve --https=10443 off`.

5. Create the first account. Set `SETUP_CODE` in the env file to a long random value. The same generator as in step 2 works. A running container does not reread the env file, so recreate the app if the stack is already up:

   ```powershell
   docker compose --env-file $Config up -d --force-recreate app
   ```

   Then open `/setup/` on the HTTPS MagicDNS URL. Enter the code with a username, display name, household name, and password, read the privacy and data policy, and save the one-time recovery codes. Accepting the policy is optional at setup. They are shown only once. After the first member exists, `/setup/` returns 404. You can then remove `SETUP_CODE` from the env file. If you do, recreate the app the same way. When Google sign-in is configured, the page also offers **Set up with Google**; the setup code is still required before the redirect to Google.

   To use your own policy text, set `PRIVACY_POLICY_PATH` to a file (or a directory that contains `privacy-policy.md`) that is visible inside the app container, then publish:

   ```powershell
   docker compose --env-file $Config exec app python manage.py publish_privacy_policy --material
   ```

   Omit `--material` for typo-fix versions that should not ask members to accept again. `/privacy-policy/` is public and shows the current version and date.

   The CLI still works if you prefer not to use the browser page:

   ```powershell
   docker compose --env-file $Config exec app python manage.py seed_first_user --username USERNAME --display-name "DISPLAY NAME" --household "HOUSEHOLD NAME"
   ```

## Google Cloud Console (optional sign-in)

Google sign-in stays off until both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set in `production.env`. Recreate the app container after changing them, the same way as for `SETUP_CODE`.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project. Open **APIs & Services** → **OAuth consent screen**. Choose **External** and **Testing**. Add the household members who will sign in as **Test users**. Do not publish the app for the first release; testing mode is enough for a household. Request only the `openid`, `email`, and `profile` scopes.
2. Open **APIs & Services** → **Credentials** → **Create credentials** → **OAuth client ID**. Application type is **Web application**.
3. Authorized JavaScript origins: the HTTPS MagicDNS origin, including a non-default Serve port when you use one, for example `https://basement-pc.example-tailnet.ts.net` or `https://basement-pc.example-tailnet.ts.net:10443`.
4. Authorized redirect URIs: the same origin plus `/accounts/google/login/callback/`, for example `https://basement-pc.example-tailnet.ts.net/accounts/google/login/callback/` or `https://basement-pc.example-tailnet.ts.net:10443/accounts/google/login/callback/`. The path is exact; a missing Serve port or a trailing-path mismatch fails the Google handshake.
5. Copy the client id and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. They are secrets; keep them in the protected env file, not in Git. Recreate the app container so it reads the new values.

## Health and operations

`GET /health/` is intentionally unauthenticated so Docker and an operator can monitor readiness. It performs `SELECT 1` and returns only `ok` with HTTP 200 or `unavailable` with HTTP 503; it never returns financial records, database names, credentials, or error details.

```powershell
Invoke-WebRequest https://BASEMENT-PC.MAGICDNS-NAME/health/
docker compose --env-file $Config ps
docker compose --env-file $Config logs --tail 50 app backup simplefin-sync db
```

The container health check (`python -m financial_planner.healthcheck`) probes the app on loopback using the first concrete entry of `DJANGO_ALLOWED_HOSTS` as its `Host` header, because Django rejects any host that is not allowed. Put the MagicDNS name first and do not start the list with `*`; otherwise the container can be reported unhealthy while the app works.

All three containers use `restart: unless-stopped`, and the Tailscale Windows service starts automatically. Docker Desktop, however, starts only when a Windows user signs in. After a reboot the app is unavailable until someone signs in to the basement PC. Confirm that **Start Docker Desktop when you sign in** is enabled in Docker Desktop settings.

Application logs must not be used for transaction details or raw import rows. Stop the deployment with `docker compose --env-file $Config stop`; do not add `--volumes` when stopping or updating it.

A household member can only leave through the application. To end someone else's current membership from the basement PC, run:

```powershell
docker compose --env-file $Config exec app python manage.py evict_household_member --username USERNAME
```

That applies the same shared-account exit rules as leaving. It prints a short confirmation and does not name accounts, transactions, or amounts. An unknown username, a person with no current membership, and a repeated eviction each fail with a clear message.

## Backups

The backup container runs `pg_dump` in PostgreSQL custom format every night at 2:00 AM in `TZ` (default `America/New_York`). A Sunday dump is also copied into `weekly/`. A dump is published atomically only after `pg_restore --list` verifies it. The same run then archives `$RECEIPTS_DIR` as `financial_planner_TIMESTAMP.receipts.tar.gz` next to that dump, with the same 14 nightly and 8 weekly copies. Receipt deletes only drop the database row; files stay on disk for about two days, so a file removed from the dump between those steps is still present for the archive. Backups keep those files until they rotate out. Pruning keeps the newest 14 files of each kind in `nightly/` and 8 in `weekly/`. Each run writes `$BACKUP_DIR/health/status` with the last success time, dump name, size, table count, last error, and the weekly restore check result. The app and SimpleFIN scheduler mount only `$BACKUP_DIR/health` read-only and raise an in-app alert to operators if no dump has succeeded in 26 hours or the last run failed, including an off-site upload failure or a failed or overdue restore check.

Set `OPERATOR_USERNAMES` in `production.env` to a comma-separated list of member usernames. If it is empty, the earliest-created member is the operator. Operators see last local and off-site success times on Settings → Data. Other members do not. Alerts name no file contents and fire at most once per local calendar day until a run succeeds.

### Weekly restore check

`pg_restore --list` proves a dump is readable, not that it restores. So once every `RESTORE_CHECK_INTERVAL_DAYS` (default 7) the backup run also restores the dump it just published into a scratch database, `financial_planner_restore_check`, on the same PostgreSQL server. It uses `ops/backup/verify-restore.sh` as `POSTGRES_USER`. The check:

- drops any leftover scratch database, creates a new one, and runs `pg_restore --no-owner --no-privileges --exit-on-error` into it;
- compares the number of restored tables with the dump's `TABLE DATA` entries (the status file's `table_count`), and checks that `django_migrations`, `finance_account`, `finance_person`, and `finance_transaction` exist;
- always drops the scratch database afterwards, whether it passed or failed.

It covers the database only, not the receipts archive, and never starts the app or runs migrations against the scratch copy. A pass sets `restore_check_at` in the status file. A failure sets `restore_check_error` to a short fixed message (never pg_restore output, which can quote row values) and the run exits non-zero. The dump stays published, off-site copying still runs, and the next nightly run tries the check again. The status file also records `restore_check_interval_days` so the app knows whether the check is on and when it is overdue. Set `RESTORE_CHECK_INTERVAL_DAYS=0` to turn the check off.

Operators get the backup alert "The weekly restore check failed" when `restore_check_error` is set. They get "The restore check is overdue" when the check is on and its last pass is more than one day past the interval (8 days with the weekly default). Settings → Data shows the last restore check, `Never`, or `Off`. A status file written before this release has no restore check fields and counts as never checked, not as a failure; the next nightly backup runs the first check.

The check needs a server at least as new as the backup image's `pg_restore` (18). `pg_restore` 17 and later always sends `SET transaction_timeout`, which a PostgreSQL 16 server rejects. So against 16 (for example while running `ops/postgres16-rollback.yml`) the check fails with "PostgreSQL 16 server is older than pg_restore 18". That message is accurate: `restore.sh` can't restore onto that server either. Finish the upgrade below, or set `RESTORE_CHECK_INTERVAL_DAYS=0` until then.

When the check fails:

1. Read the message on Settings → Data or in `Get-Content E:inancial-planner-backups\health\status`, and the backup logs (`docker compose --env-file $Config logs --tail 50 backup`).
2. Run the check by hand against the newest dump: `docker compose --env-file $Config run --rm backup /opt/financial-planner/verify-restore.sh /backups/nightly/<newest dump>`. If it passes now, the failure was transient (for example, the server was restarting). The next scheduled check clears the alert.
3. If it fails again, treat that dump as unusable: confirm an older dump restores with the same command, and take a fresh manual backup (see below) and check it. A table count mismatch or a missing core table usually means the dump was taken from the wrong database or during a broken migration; investigate before relying on any newer dump.

### Encrypted off-site copy

When both `OFFSITE_RCLONE_REMOTE` and `OFFSITE_AGE_RECIPIENT` are set, each verified dump and its receipts archive are encrypted with `age` to that public key and uploaded with rclone. Remote nightly and weekly prefixes keep the same 14 and 8 file retention per kind, pruned by name. An upload failure is recorded in `offsite_error` (the local dump stays published) and alerts operators. A configured off-site copy whose last success is older than 26 hours is also unhealthy.

1. Install rclone on a trusted machine, run `rclone config`, and save the file outside the checkout. Set `OFFSITE_RCLONE_CONFIG` in `production.env` to that path (forward slashes on Windows). The backup container mounts only that file, read-only, as `/config/rclone.conf`. Leave the variable unset to use the committed empty placeholder.
2. Create an age key pair on a trusted machine (`age-keygen`). Put the **public** key in `OFFSITE_AGE_RECIPIENT`. Keep the private key in a password manager. The private key must never live on the tower.
3. Set `OFFSITE_RCLONE_REMOTE` to the rclone destination, for example `b2:bucket/financial-planner` or `drive:financial-planner-backups`. Recreate the backup container after editing `production.env`.

Example remote names include Backblaze B2, Google Drive, and OneDrive. A NAS can be another rclone remote later.

Run and verify an extra backup before an upgrade or restore drill:

```powershell
docker compose --env-file $Config run --rm backup /opt/financial-planner/backup.sh
Get-ChildItem E:\financial-planner-backups\nightly
Get-Content E:\financial-planner-backups\health\status
docker compose --env-file $Config logs --tail 50 backup
```

The backup directory is a bind mount from the second disk, not part of the database volume or image. Monitor that disk's free space and confirm new nightly files appear.

## Restore into a fresh database volume

Use a fresh named volume so the old database remains available for investigation or rollback. The commands below cause downtime and assume `BACKUP_DIR` still points to the directory containing the selected dump. For an off-site copy, download the `.dump.age` file and the matching `.receipts.tar.gz.age` file, decrypt both with the age private key, then restore:

```powershell
age -d -i $AgeIdentity -o financial_planner_YYYYMMDDTHHMMSSZ.dump financial_planner_YYYYMMDDTHHMMSSZ.dump.age
age -d -i $AgeIdentity -o financial_planner_YYYYMMDDTHHMMSSZ.receipts.tar.gz financial_planner_YYYYMMDDTHHMMSSZ.receipts.tar.gz.age
```

Copy the decrypted dump into `E:\financial-planner-backups\nightly\` (or pass the dump path to `restore.sh`). If a sibling receipts archive is present, restore puts it into `RECEIPTS_DIR`; if that archive is missing (backups from before this release), the database is still restored and the live receipts directory is left unchanged. Continue with the local procedure. Do not copy the age private key onto the tower.

1. Choose a known-good file and stop writers:

   ```powershell
   $Config = 'D:\financial-planner-config\production.env'
   $Dump = 'financial_planner_YYYYMMDDTHHMMSSZ.dump'
   docker compose --env-file $Config stop app backup simplefin-sync db
   ```

2. In `production.env`, change `POSTGRES_VOLUME_NAME` to a new name such as `financial-planner-postgres-data-restored-YYYYMMDD`. Change `RECEIPTS_VOLUME_NAME` to a matching new receipts volume such as `financial-planner-receipts-restored-YYYYMMDD`. Do not delete or reuse the old volumes.

3. Start empty PostgreSQL, restore, and start the application:

   ```powershell
   docker compose --env-file $Config up -d db
   docker compose --env-file $Config run --rm backup /opt/financial-planner/restore.sh "/backups/nightly/$Dump"
   docker compose --env-file $Config up -d app backup simplefin-sync
   docker compose --env-file $Config ps
   Invoke-WebRequest https://BASEMENT-PC.MAGICDNS-NAME/health/
   ```

4. Sign in and verify expected synthetic or real records according to the purpose of the restore. Keep the old volume until the owner explicitly accepts the restored database. Volume deletion is intentionally not part of this runbook.

## Upgrades and migrations

Review release notes and take a verified manual backup first. Then fetch the approved revision and run:

```powershell
docker compose --env-file $Config build --pull app backup simplefin-sync ai-jobs
docker compose --env-file $Config up -d
docker compose --env-file $Config ps
Invoke-WebRequest https://BASEMENT-PC.MAGICDNS-NAME/health/
```

Starting the new app applies all pending Django migrations before Gunicorn accepts traffic. `simplefin-sync` and `ai-jobs` wait for the app to report healthy, so they never run against a database that has not been migrated yet. After `up -d`, compare each running container's image with the newly built one (`docker inspect -f '{{.Image}}' <container>` against `docker image inspect -f '{{.Id}}' <image>`). If one still runs the old image, as `backup` has done, recreate it with `docker compose --env-file $Config up -d --force-recreate <service>`. If a migration or health check fails, inspect bounded logs with `docker compose --env-file $Config logs --tail 100 app db`; do not repeatedly restart or run migrations by hand. Restore the pre-upgrade dump into a fresh volume using the procedure above when database rollback is required.

A PostgreSQL major-version change cannot use these steps. Follow [PostgreSQL 16 to 18 upgrade](#postgresql-16-to-18-upgrade) instead.

## PostgreSQL 16 to 18 upgrade

A PostgreSQL major version cannot open an older major's data directory, so this upgrade moves the data with a dump and restore onto new volumes. Do it only with the owner present. It causes downtime, and the old volumes stay untouched for rollback.

From 18, the `postgres` image keeps its data in a versioned subdirectory, so `compose.yml` mounts the volume at `/var/lib/postgresql` instead of `/var/lib/postgresql/data`. If the 18 image is started on a volume that still holds 16 data, it refuses to start and restarts in a loop (`docker compose logs db` explains). It never starts an empty database in its place. The backup image moves to 18 with it: `pg_dump` 18 can dump a 16 server, but `pg_dump` 16 cannot dump an 18 server.

1. Record the starting point. Keep both files outside the checkout:

   ```powershell
   $Config = 'D:\financial-planner-config\production.env'
   $Work = 'D:\financial-planner-config\pg18-upgrade'
   New-Item -ItemType Directory -Force $Work
   git rev-parse HEAD | Out-File -Encoding utf8 "$Work\revision-before.txt"
   Select-String '^(POSTGRES|RECEIPTS)_VOLUME_NAME=' $Config | Out-File -Encoding utf8 "$Work\volumes-before.txt"
   ```

   If `POSTGRES_VOLUME_NAME` or `RECEIPTS_VOLUME_NAME` is not set, the volume in use is the default from `compose.yml` (`financial-planner-postgres-data` or `financial-planner-receipts`).

2. Take and verify a backup while still on 16, using the commands under [Backups](#backups).

3. Fetch the approved revision and build its images. Building does not change the running containers:

   ```powershell
   docker compose --env-file $Config build --pull app backup simplefin-sync ai-jobs
   ```

4. Stop the app and workers, leaving PostgreSQL 16 running. Then take the cut-over dump with the new backup image, and record row counts. `--no-deps` keeps compose from recreating `db` with the 18 image:

   ```powershell
   docker compose --env-file $Config stop app simplefin-sync ai-jobs backup
   docker compose --env-file $Config run --rm -T --no-deps backup /opt/financial-planner/backup.sh
   docker compose --env-file $Config run --rm -T --no-deps backup /opt/financial-planner/row-counts.sh | Out-File -Encoding utf8 "$Work\row-counts-before.tsv"
   docker compose --env-file $Config stop db
   ```

   Note the `Backup completed: financial_planner_TIMESTAMP.dump` name as `$Dump`. `row-counts.sh` prints one line for each table: the table name and an exact row count, never row contents.

5. In `production.env`, set `POSTGRES_VOLUME_NAME` to a new name such as `financial-planner-postgres18-YYYYMMDD`. Set `RECEIPTS_VOLUME_NAME` to a new name such as `financial-planner-receipts-pg18-YYYYMMDD`. The receipts move too, so the old receipts volume still matches the old database for rollback. Do not delete or reuse the old volumes.

6. Start PostgreSQL 18 on the new volume, restore the dump, and compare row counts. `Compare-Object` should print nothing:

   ```powershell
   $Dump = 'financial_planner_YYYYMMDDTHHMMSSZ.dump'
   docker compose --env-file $Config up -d db
   docker compose --env-file $Config run --rm -T backup /opt/financial-planner/restore.sh "/backups/nightly/$Dump"
   docker compose --env-file $Config run --rm -T backup /opt/financial-planner/row-counts.sh | Out-File -Encoding utf8 "$Work\row-counts-after.tsv"
   Compare-Object (Get-Content "$Work\row-counts-before.tsv") (Get-Content "$Work\row-counts-after.tsv")
   ```

   If the counts differ, stop here and roll back. Do not start the app.

7. Start everything, then check health:

   ```powershell
   docker compose --env-file $Config up -d
   docker compose --env-file $Config ps
   docker compose --env-file $Config exec db psql -U financial_planner -d financial_planner -tAc "select version()"
   Invoke-WebRequest https://BASEMENT-PC.MAGICDNS-NAME/health/
   ```

   `ps` should show `db` on `postgres:18-alpine`, with `app` and `db` healthy. If you changed `POSTGRES_USER` or `POSTGRES_DB`, use those values with `psql`. Sign in and spot-check recent transactions and a receipt. Run one manual backup on 18 (see [Backups](#backups)).

8. Keep the old 16 volumes until the owner explicitly confirms the upgrade. Deleting a volume is intentionally not part of this runbook.

### Rolling back to 16

Rollback points the stack back at the old volumes and runs the 16 image on them through the `ops/postgres16-rollback.yml` override. Anything written after the cut-over is not carried back. The app and backup images stay on the new revision; the 18 `pg_dump` still backs up a 16 server.

1. Stop everything: `docker compose --env-file $Config stop`.
2. In `production.env`, set `POSTGRES_VOLUME_NAME` and `RECEIPTS_VOLUME_NAME` back to the values in `$Work\volumes-before.txt`. If they were not set before, remove them again.
3. Start with the override, then verify:

   ```powershell
   $Rollback = @('--env-file', $Config, '-f', 'compose.yml', '-f', 'ops/postgres16-rollback.yml')
   docker compose @Rollback up -d
   docker compose @Rollback ps
   docker compose @Rollback run --rm -T backup /opt/financial-planner/row-counts.sh | Out-File -Encoding utf8 "$Work\row-counts-rollback.tsv"
   Compare-Object (Get-Content "$Work\row-counts-before.tsv") (Get-Content "$Work\row-counts-rollback.tsv")
   Invoke-WebRequest https://BASEMENT-PC.MAGICDNS-NAME/health/
   ```

While rolled back, pass both `-f` files on every compose command. A command without the override tries the 18 image on the 16 volume, which only restart-loops. That failed start leaves an empty `18` directory in the old volume, which PostgreSQL 16 ignores. To go back to the full revision that ran before the upgrade instead, check out the commit in `revision-before.txt`, rebuild the images, and run `up -d` without the override.

## Reviewing a dependency pull request

Dependabot opens version and security update PRs. There is no auto-merge; the owner approves every merge. Keep exact pins in `requirements.txt`. Treat `django-allauth` upgrades as needing the Google sign-in tests as well as the rest of the suite.

On the PR branch:

1. Run the SQLite suite (`python -m pytest tests -q`) and `bash scripts/test_postgres.sh` (the `PostgreSQL 18` workflow runs the same on the PR).
2. After merge, rebuild and deploy with the upgrade steps above (backup first, then `docker compose --env-file $Config build --pull` and `up -d`, then the health check).

## Synthetic restore exercise record

On 2026-09-27, this procedure was exercised locally with Docker Desktop 29.8.0, PostgreSQL 16, and only the committed `synthetic_demo` fixture. The app and database health checks passed; the fixture contained 3 synthetic transactions; a custom-format dump produced both nightly and forced weekly copies; the transactions were deleted (count 0); and `pg_restore` recovered the count to 3. The same dump was then restored into a second, fresh named volume, where the count was 3 and `/health/` returned HTTP 200. No real statement, credential, account number, or financial record was used or written to Git.

## PostgreSQL 16 to 18 upgrade drill record

On 2026-10-04, the upgrade and rollback runbook was exercised on the basement PC with Docker Desktop 29.8.1. It ran in an isolated compose project (`-p fp-drill`) with its own volume names, loopback port, and scratch backup directory, so the production project and volumes were not touched. Only the committed `synthetic_demo` fixture and one invented receipt file were used.

- **Baseline:** the pre-change revision ran on PostgreSQL 16.15, held 3 synthetic transactions, and returned HTTP 200 from `/health/`.
- **Cut-over:** with the app and workers stopped, the new 18 backup image dumped the 16 server under `run --no-deps`, and the running 16 container was left in place. `row-counts.sh` recorded 66 tables.
- **Restore:** PostgreSQL 18.6 started on new database and receipts volumes. `restore.sh` restored the dump and the receipt, and every table's row count matched the baseline. Counts still matched after the app started and ran its migrations. All containers came up, `app` and `db` reported healthy, `/health/` returned HTTP 200, and a nightly backup on 18 succeeded.
- **Rollback:** a `db` start on the 16 volume without the override restart-looped with the image's old-data error and never initialized a database. With `ops/postgres16-rollback.yml`, the stack returned to 16.15 on the old volumes. Row counts matched the baseline, `/health/` returned HTTP 200, the receipt was intact, and the 18 backup image backed up the 16 server.

The drill's containers, volumes, and images were removed afterward.

## Optional alert email notices

Email notices are off per member by default. To offer them, set `SMTP_HOST`,
`SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, and
`SMTP_TLS` (default true, STARTTLS) in the ignored `production.env`. Set
`ALERT_EMAIL_BASE_URL` to the app's full HTTPS Tailscale origin without a trailing
path (for example `https://planner.example.invalid`). Keep credentials out of Git.
The app and SimpleFIN scheduler receive these settings through Compose.

With host, from address, or app origin unset, email controls are hidden and no
notices are sent. Members opt in and choose their address under Settings > Alerts;
changing that address requires recent authentication. Save first, then use
**Send a test** to check delivery. Disabling notices does not require reauthentication.

After a daily alert pass or sync, one plain-text notice per opted-in member lists
the count and kinds of unread visible alerts that have not been emailed, with a link to
`/alerts/`. It never includes alert titles, amounts, merchants, or account names.
Alerts raised between runs wait for the next pass. Successful delivery records a
timestamp so those alerts are not emailed again. Nested scheduled sync and daily
passes share one batch; overlapping runs serialize delivery per member. Notices
wait for database commit. Failed sends leave the inbox intact, log a generic message without the address or SMTP exception, and
remain pending for a later pass. SMTP has a ten-second timeout.

For development or tests, use Django's console or locmem email backend. Never
point a test suite at a real SMTP server.
