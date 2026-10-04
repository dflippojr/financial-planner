# Basement PC deployment

This runbook deploys the first release to the Windows basement PC with Docker Desktop's WSL2 backend. The application is available only through the existing Tailscale tailnet: Docker publishes the application on Windows loopback, and `tailscale serve` terminates HTTPS and proxies to that loopback port. PostgreSQL is not published to the host or network.

## Storage and network boundaries

| Item | Location | Retention or exposure |
|---|---|---|
| Application source and synthetic fixture | Git checkout | Code only; never add populated environment files, CSV exports, database files, or dumps. |
| Secrets and per-machine settings | A protected file outside the checkout, such as `D:/financial-planner-config/production.env` | Keep until rotated; restrict its Windows permissions to the operator account. |
| PostgreSQL data | Docker named volume selected by `POSTGRES_VOLUME_NAME` | Durable across container replacement; never commit or manually edit it. |
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

   If `APP_PORT` is not 8000, use its value in the target URL. Tailscale Serve accepts only loopback HTTP proxy targets and supplies `X-Forwarded-Proto`; Django trusts that proxy header, redirects other HTTP requests to HTTPS, and uses secure session and CSRF cookies. Open the HTTPS MagicDNS URL shown by `tailscale serve status`. The command uses Serve, not Funnel, so it does not intentionally expose the app to the public internet. See the [Tailscale Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

   **When the PC already runs other services**, check before choosing ports:

   - Run `netstat -ano | findstr LISTENING` and pick an unused loopback `APP_PORT`. Another container, such as Portainer, often holds 8000.
   - Run `tailscale serve status` first. The command above maps the default HTTPS port (443). If 443 already proxies another app, running it replaces that mapping. Add the app on its own HTTPS port instead, one that is neither listed in `tailscale serve status` nor listening on the tailnet address:

     ```powershell
     tailscale serve --bg --https=10443 http://127.0.0.1:8210
     ```

   - With a non-default HTTPS port, `DJANGO_CSRF_TRUSTED_ORIGINS` must include it, for example `https://basement-pc.example-tailnet.ts.net:10443`, or every sign-in and form post fails the CSRF check. `DJANGO_ALLOWED_HOSTS` stays the bare MagicDNS name, without a port.
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

The backup container runs `pg_dump` in PostgreSQL custom format every night at 2:00 AM in `TZ` (default `America/New_York`). A Sunday dump is also copied into `weekly/`. A dump is published atomically only after `pg_restore --list` verifies it. Pruning keeps the newest 14 files in `nightly/` and 8 in `weekly/`. Each run writes `$BACKUP_DIR/status` with the last success time, dump name, size, table count, and last error. The app and SimpleFIN scheduler mount that directory read-only and raise an in-app alert to operators if no dump has succeeded in 26 hours or the last run failed, including an off-site upload failure.

Set `OPERATOR_USERNAMES` in `production.env` to a comma-separated list of member usernames. If it is empty, the earliest-created member is the operator. Operators see last local and off-site success times on Settings → Data. Other members do not. Alerts name no file contents and fire at most once per local calendar day until a run succeeds.

### Encrypted off-site copy

When both `OFFSITE_RCLONE_REMOTE` and `OFFSITE_AGE_RECIPIENT` are set, each verified dump is encrypted with `age` to that public key and uploaded with rclone. Remote nightly and weekly prefixes keep the same 14 and 8 file retention, pruned by name. An upload failure is recorded in the status file and alerts operators; the local dump is left in place.

1. Install rclone on a trusted machine, run `rclone config`, and save the file as `D:\financial-planner-config\rclone.conf`. The backup container mounts `CONFIG_DIR` (default `D:/financial-planner-config`) read-only as `/config`.
2. Create an age key pair on a trusted machine (`age-keygen`). Put the **public** key in `OFFSITE_AGE_RECIPIENT`. Keep the private key in a password manager. The private key must never live on the tower.
3. Set `OFFSITE_RCLONE_REMOTE` to the rclone destination, for example `b2:bucket/financial-planner` or `drive:financial-planner-backups`. Recreate the backup container after editing `production.env`.

Example remote names include Backblaze B2, Google Drive, and OneDrive. A NAS can be another rclone remote later.

Run and verify an extra backup before an upgrade or restore drill:

```powershell
docker compose --env-file $Config run --rm backup /opt/financial-planner/backup.sh
Get-ChildItem E:\financial-planner-backups\nightly
Get-Content E:\financial-planner-backups\status
docker compose --env-file $Config logs --tail 50 backup
```

The backup directory is a bind mount from the second disk, not part of the database volume or image. Monitor that disk's free space and confirm new nightly files appear.

## Restore into a fresh database volume

Use a fresh named volume so the old database remains available for investigation or rollback. The commands below cause downtime and assume `BACKUP_DIR` still points to the directory containing the selected dump. For an off-site copy, download the `.dump.age` file, decrypt it with the age private key, then restore the dump:

```powershell
age -d -i $AgeIdentity -o financial_planner_YYYYMMDDTHHMMSSZ.dump financial_planner_YYYYMMDDTHHMMSSZ.dump.age
```

Copy the decrypted dump into `E:\financial-planner-backups\nightly\` (or pass its path to `restore.sh`) and continue with the local procedure. Do not copy the age private key onto the tower.

1. Choose a known-good file and stop writers:

   ```powershell
   $Config = 'D:\financial-planner-config\production.env'
   $Dump = 'financial_planner_YYYYMMDDTHHMMSSZ.dump'
   docker compose --env-file $Config stop app backup simplefin-sync db
   ```

2. In `production.env`, change `POSTGRES_VOLUME_NAME` to a new name such as `financial-planner-postgres-data-restored-YYYYMMDD`. Do not delete or reuse the old volume.

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

## Synthetic restore exercise record

On 2026-09-27, this procedure was exercised locally with Docker Desktop 29.8.0, PostgreSQL 16, and only the committed `synthetic_demo` fixture. The app and database health checks passed; the fixture contained 3 synthetic transactions; a custom-format dump produced both nightly and forced weekly copies; the transactions were deleted (count 0); and `pg_restore` recovered the count to 3. The same dump was then restored into a second, fresh named volume, where the count was 3 and `/health/` returned HTTP 200. No real statement, credential, account number, or financial record was used or written to Git.
