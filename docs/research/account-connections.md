# Optional automatic account connections: research

Status: research only (issue #20). No integration, dependency, key, or sign-up was made. The first release stays CSV-only (docs/requirements.md). Adopting any option needs a separate owner decision and a new issue. No personal financial data appears here.

Access date for every source below: **2026-09-26**. Sources were fetched as page summaries; anything not stated on a fetched primary page is marked **unverified**, and nothing was filled from memory. Many per-institution facts could not be verified because coverage lists are interactive or gated behind dashboards or sign-ups.

## Options compared

1. **Plaid** (commercial aggregator).
2. **SimpleFIN Bridge** (low-cost, user-facing bridge with a small open protocol).
3. **Teller** (non-Plaid commercial aggregator with a developer free tier).
4. **Keep CSV import only** (status quo).
5. Direct institution APIs: not researched beyond noting them. **Unverified** for all four institutions; no developer program pages were checked.

## Primary sources

- Plaid pricing: https://plaid.com/pricing/
- Plaid institutions: https://plaid.com/docs/institutions/
- Plaid OAuth and re-auth: https://plaid.com/docs/link/oauth/
- Plaid Transactions: https://plaid.com/docs/api/products/transactions/
- Plaid errors: https://plaid.com/docs/errors/
- Plaid legal index: https://plaid.com/legal/
- SimpleFIN Bridge: https://beta-bridge.simplefin.org/
- SimpleFIN protocol: https://www.simplefin.org/protocol.html
- Teller: https://teller.io/ and https://teller.io/docs

## Comparison by option

| | Plaid | SimpleFIN Bridge | Teller | CSV only |
|---|---|---|---|---|
| **Cost** | Pay as You Go, Growth (12-month commitment), Custom. Limited Production sandbox: up to 200 API calls per product on live data, free. Per-call prices not captured (unverified). Whether a developer may use it for personal, non-commercial use: unverified (the Developer Policy was not read in full). | $1.50/month or $15/year plus tax, up to 25 institutions and 25 apps. Personal use is the evident target; explicit terms unverified. | Free developer tier up to 100 live connections. Production: $0.30 per enrollment per month for transactions, $0.10 per balance call. Personal use terms unverified. | Free. |
| **Coverage (general)** | Over 10,000 US/CA institutions. | "Numerous" institutions; list unverified. | Over 7,000 institutions claimed. | Any institution with an export. |
| **Huntington** | Unverified (coverage explorer is interactive). | Unverified. | Unverified. | CSV format still to verify (issue #1). |
| **Capital One** | Unverified. Plaid's OAuth page names Capital One as a US OAuth bank with 12-month consent. | Unverified. | Unverified. | As above. |
| **Apple Card** | Unverified. | Unverified. | Unverified. | As above. |
| **Vanguard** | Unverified, including holdings and investment transactions. | Unverified. | Unverified. | Meaning of Vanguard rows unresolved (issue #1). |
| **History** | Default 90 days, max 730 days for Transactions. | Optional date-range filter; depth unverified. | Unverified. | Whatever the export holds. |
| **Consent flow** | Plaid Link, OAuth at many banks. Expired consent gives `ITEM_LOGIN_REQUIRED`; update mode re-runs the flow. Bank-side revocation takes 24-48 h to show. `PENDING_DISCONNECT` webhook one week before expiry. | User creates a token at the Bridge, gives it to the app, the app claims an access URL. A 403 on claim means the token may be compromised. Revocation mechanics unverified. | Teller Connect. Re-auth and revocation unverified. | Manual download and upload. |
| **Operational limits** | Rate-limit values not documented on the page read (unverified). Sync cursor valid at least one year. Redirect URI is optional for desktop web and required on mobile; webhooks need a reachable URL (unverified for this app). | Access URL uses HTTP Basic Auth over TLS. Rate limits unverified. No inbound callback needed by the protocol as described, since the app polls. | mTLS details, callbacks, rate limits unverified. | None. |
| **Privacy** | Data flows through Plaid to the developer; see End User Privacy Policy on the legal page. Sharing terms not summarized here (unverified). | Bridge operator holds credentials and data; privacy policy exists on the site but was not read (unverified). | Unverified. | Data never leaves the owner. |

## Fit with the existing model

- **Sign convention.** Plaid: positive is money out, negative is money in. This may be the opposite of the app's convention, so an adapter must map it explicitly and document it per importer. SimpleFIN and Teller sign conventions: unverified; define the adapter only after checking their docs. Currency must stay explicit; SimpleFIN allows custom currencies (such as points), so an adapter must reject non-currency units.
- **Provenance and import batches.** Each sync run would be a batch tagged with source (provider name, provider account id) and run id, so it can be corrected or undone like a CSV batch. Plaid's `transaction_id` and cursor give stable source ids; others unverified.
- **Reimport de-duplication against CSV rows.** Provider ids do not exist in CSV exports, so matching CSV against connected rows needs a fuzzy rule (account, date, amount, normalized description) or a rule that an account is either CSV or connected for a date range. Pending-to-posted changes (Plaid `pending_transaction_id`) must be handled to avoid double counting. Owner decision needed.
- **Private versus household.** A connection belongs to one person and links accounts that inherit private or household visibility like CSV-added accounts. Credentials or access URLs must be stored only for the owner, never shown in logs or errors, and a shared account's connection should be manageable only by its owner (open question).
- **Internal transfers, card payments, investments.** Connected feeds do not resolve report semantics; the same rules as CSV apply.

## Unverified recommendation

Stay CSV-only for the first release, as already decided. If the owner later wants automation, evaluate SimpleFIN Bridge first (small fixed cost, simple polling protocol, no hosted callback), then Teller's free developer tier, and treat Plaid as last because its terms for personal use are unconfirmed and it must not be a paid dependency. This is a judgment from incomplete evidence, not a verified fact; institution coverage in particular must be checked before any choice.

## Open owner decisions

1. Is any automatic connection worth exploring after the CSV release, and what monthly cost is acceptable?
2. Is a third-party bridge holding bank credentials acceptable privacy-wise?
3. Should connected and CSV data coexist on one account, and what is the de-duplication rule?
4. Who may manage a connection on a shared household account?
5. Who verifies institution coverage (needs a sign-up or dashboard access, which this issue forbids)?
