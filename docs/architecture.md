# Architecture

Updated: 2026-09-27. Records the stack and deployment decisions for issue #2. Owner-confirmed choices are marked; the rest are the agent's recommendation with rationale, open to correction.

## Stack

| Layer | Choice | Rationale | Rejected alternative |
|---|---|---|---|
| Language | Python (owner-confirmed) | Owner preference. | TypeScript/Node, C#/.NET |
| Web framework | Django | Batteries-included: ORM, migrations, session auth, and server-rendered templates out of the box, which matches the server-rendered-pages decision below without assembling separate libraries. | Flask/FastAPI + SQLAlchemy + Alembic (more assembly, no built-in auth/session model) |
| UI style | Server-rendered pages (owner-confirmed) | Simplest to build and deploy for a 2-person household app; works fine in a desktop/laptop browser over Tailscale. | SPA + API (not needed without a near-term mobile/PWA requirement) |
| Database | PostgreSQL (owner-confirmed) | Owner preference over an embedded database. | SQLite |
| Migrations | Django's built-in migration framework | Ships with the chosen framework; no separate tool to install or keep in sync with models. | Alembic (redundant once Django is chosen) |
| Money representation | Integer minor units (cents) in a `BigInteger` column, plus an explicit ISO 4217 `currency` column (e.g. `"USD"`) per amount | Exact integer arithmetic with no floating-point drift, and avoids decimal-serialization pitfalls in JSON APIs and templates. Every table storing an amount stores its currency alongside it, per AGENTS.md. | `NUMERIC`/`Decimal` columns (also exact, but requires consistent decimal handling through the ORM, templates, and JSON serialization to avoid accidental float coercion) |

## Authentication and authorization

- Authentication uses Django's built-in `User` model and session-based login (cookie sessions over HTTPS via Tailscale; see Network below). Owner decision 2026-09-30: Google sign-in is added alongside passwords as the preferred method, never as a replacement. Joining still requires an invitation. Google is implemented with `django-allauth` (Google provider only, PKCE, no stored tokens) and is off unless `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set. See `docs/requirements.md`, "Sign-in methods".
- Authentication is denied by default at the middleware layer. Only sign-in, first-run setup, invitation acceptance, recovery, and (when Google is enabled) the Google OAuth start and callback paths are explicitly public. Session state is stored server-side; cookies are secure, HTTP-only, and SameSite=Lax. See `docs/authentication.md` for lifecycle and operator procedures.
- The private/household authorization model itself (household membership, share/unshare, ownership transfer) is specified in issue #3. Architecturally: every `Account` and `Transaction` queryset is built through a shared manager/query layer that filters by "owned by this person" or "shared with a household this person currently belongs to." Views, dashboard aggregates, search, import, and export all go through that layer rather than filtering ad hoc, so a private-account leak requires a defect in one place, not many.
- No personal financial data is ever included in logs or error messages; the shared query layer is the single place that enforces this, per AGENTS.md.

## Deployment (basement PC)

- Basement PC runs Windows with Docker Desktop (WSL2 backend), per owner decision.
- A `docker-compose` stack runs: the Django app (behind gunicorn), PostgreSQL with a named volume for durable storage, and a scheduled backup job. Docker's `restart: unless-stopped` policy handles process supervision; Docker Desktop is configured to start at Windows boot.
- Network exposure: the app is reached over Tailscale using `tailscale serve`, which terminates HTTPS with Tailscale's own certificates. The app listens on plain HTTP inside the container; `tailscale serve` reverse-proxies HTTPS to it. Plain-HTTP LAN access outside the tailnet is not provided in the first release, since Tailscale already reaches every household device on the home network. Revisit if a household member needs access from a device that can't run Tailscale.
- Backup and restore approach (detailed schedule and retention in issue #11): nightly logical backups (`pg_dump`) written to a second local disk on the basement PC. Restore is `pg_restore` into a fresh Postgres volume. This is an approach, not the final schedule/retention policy, which #11 will pin down.

## Development workflow

- Dependency management: Poetry (or pip + `requirements.txt`/`venv` if the owner prefers less tooling — default to Poetry unless corrected).
- Local dev mirrors production shape via the same `docker-compose` stack (app + Postgres), so behavior matches the basement PC.
- Tests: pytest. Lint/format: ruff + black.
- Fixtures are synthetic only; no real financial data is ever used in development or tests, per AGENTS.md.

## Open items carried to other issues

- Exact private/household authorization rules: issue #3.
- Account/transaction/import-batch schema built on the money and provenance conventions above: issue #4.
- Backup schedule, retention, and restore drill: issue #11.
- Session/login UX details (password reset, lockout, etc.): issue #12.
