# Authentication, onboarding, and recovery

The app uses Django usernames and passwords with database-backed sessions, and optional Google sign-in through `django-allauth`. Authentication is required by default for every view; only sign-in, first-run setup, invitation acceptance, account recovery, and (when Google is configured) the Google OAuth start and callback paths are public. Financial records must be queried through the model `visible_to()` methods so private records do not appear in pages, aggregates, searches, errors, or exports.

## First member

Set `DJANGO_SECRET_KEY` to a long random value and apply migrations. While no `User` or `Person` exists, open `/setup/` in the browser. That page requires `SETUP_CODE` from the environment (compared in constant time, never logged or shown again). Submit the code, a username, display name, household name, and a password twice. Django's password validators apply, and surrounding whitespace is stripped the same way as on the other password forms. Success creates the household, starter categories, and eight one-time recovery codes, then signs the member in. After the first member exists, `/setup/` returns 404. Sign-in redirects to setup while no member exists. When Google sign-in is configured, the same page offers **Set up with Google**. The setup code is checked before the browser is redirected to Google.

If `SETUP_CODE` is unset, the page tells the operator to set it and does not create a user. Failed setup-code attempts use the same login throttle as sign-in. Concurrent submissions are serialized with a database lock so only one first member can be created.

The management command remains available and does not require `SETUP_CODE`:

```console
python manage.py seed_first_user --username USERNAME --display-name "DISPLAY NAME" --household "HOUSEHOLD NAME"
```

The command works only while no `Person` exists. It prompts twice for a password and applies Django's configured password validators. It then prints eight one-time recovery codes. Store those codes in a password manager; the app stores only keyed digests and cannot display them again.

## Inviting another member

Any person with a current household membership can open **Invite a household member** and create a code. The raw code is shown only in that response. Share it outside the app using a trusted channel. The recipient enters it on **Use an invitation**, chooses a username and either a password or **Join with Google**, and receives their own eight recovery codes.

An invitation can be used once and expires after 48 hours. Creating a new invitation does not invalidate older unused invitations. `INVITATION_TTL_HOURS` can change the duration for future codes.

## Google sign-in

Google sign-in is off unless both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set. With those unset, sign-in, join, and setup look and behave as password-only. When they are set, **Sign in with Google** is shown first on the sign-in page; password sign-in remains. Identities are matched by Google's subject id (`sub`), and only when Google reports a verified email. An email address match alone never signs anyone in or links accounts.

A Google identity that is not already linked is refused at sign-in and does not create an account. New members still need a valid invitation (or first-run setup). A signed-in member can connect or disconnect Google and add or remove a password on **Account**, but cannot remove their last remaining method. Recovery codes still work for Google-only members; recovery sets a password.

The OAuth handshake uses `state` and PKCE. The app does not store Google access or refresh tokens. Absolute session expiry is set at sign-in for both methods. Failed Google sign-in attempts for the same remote address use the login throttle.

## Recovery

On **Recover account**, a person enters their username, one unused recovery code, and a new password. A successful recovery consumes the code and deletes every existing server-side session for that user. Other recovery codes remain valid. There is deliberately no email, administrator, or host CLI password-reset path. If the only member loses both the password and every recovery code, the account cannot be recovered through the application; restore a database backup or rebuild the installation.

## Sessions and sign-in protection

Sessions expire 28 days after sign-in and do not extend with activity. Signing out deletes the current server-side session. Five failed attempts for the same normalized username and remote address within 15 minutes block that pair for 15 minutes. Google sign-in failures for a remote address use the same limiter. Responses remain generic so they do not confirm whether a username exists.

The defaults can be changed with `DJANGO_SESSION_COOKIE_AGE`, `LOGIN_FAILURE_LIMIT`, `LOGIN_FAILURE_WINDOW_SECONDS`, and `LOGIN_BLOCK_SECONDS`. Values are seconds except the invitation duration noted above.

Sensitive actions (invite, leave household, change sign-in methods, share or unshare an account, change co-owned or lent, delete an account, download a data export, and connect or disconnect SimpleFIN) require a fresh confirmation. One confirmation covers further sensitive actions for ten minutes in the same session (`REAUTH_WINDOW_SECONDS`). The timestamp lives in the server-side session, is set at sign-in and after a successful confirmation, and is cleared on sign-out. A sensitive POST without a fresh confirmation is not performed; the member is sent to `/reauth/` and then back to the form to submit again. Failed confirmations use the login throttle. Recovery codes are not accepted for this confirmation. `GOOGLE_REAUTH_MAX_AGE_SECONDS` bounds how recent a Google ID token `auth_time` must be. The confirmation request uses `prompt=select_account` with `max_age=0` and asks for `auth_time` through the `claims` parameter. Google sends `auth_time` only to published, verified apps; without it, the ID token must have been issued (`iat`) within the same bound. That proves a fresh round trip through the account chooser for the member's own Google account, not a fresh Google password entry. Signing in again with Google from an already signed-in session never counts as confirmation.

## HTTPS settings

The deployment must terminate HTTPS with `tailscale serve` and forward `X-Forwarded-Proto: https`. Direct HTTP requests redirect to HTTPS. Configure these environment variables:

- `DJANGO_SECRET_KEY`: required; use a long random value and keep it out of Git and logs.
- `DJANGO_ALLOWED_HOSTS`: comma-separated Tailscale hostnames accepted by Django.
- `DJANGO_CSRF_TRUSTED_ORIGINS`: comma-separated HTTPS origins, including the scheme, used to access the app.

Secure session and CSRF cookies, HTTPS redirects, and one-year HSTS are enabled by default. `DJANGO_SECURE_COOKIES`, `DJANGO_SECURE_SSL_REDIRECT`, and `DJANGO_SECURE_HSTS_SECONDS` exist for isolated local development and tests; do not weaken them on the deployed app.
