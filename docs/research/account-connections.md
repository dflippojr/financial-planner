# Optional automatic account connections: research

Status: research only (issue #20). No integration, dependency, key, or sign-up was made. The first release stays CSV-only (docs/requirements.md). Adopting any option needs a separate owner decision and a new issue. No personal financial data appears here.

Every source key below (P1..T2) was accessed on **2026-09-26** and is cited as [key, 2026-09-26] near each claim. Sources were fetched as page summaries; anything not stated on a fetched primary page is marked **unverified**, and nothing was filled from memory. Many per-institution facts could not be verified because coverage lists are interactive or gated behind dashboards or sign-ups.

## Options compared

1. **Plaid** (commercial aggregator).
2. **SimpleFIN Bridge** (low-cost, user-facing bridge with a small open protocol).
3. **Teller** (non-Plaid commercial aggregator with a developer free tier).
4. **Keep CSV import only** (status quo).
5. Direct institution APIs: not researched beyond noting them. **Unverified** for all four institutions; no developer program pages were checked.

## Primary sources

- [P1] Plaid pricing: https://plaid.com/pricing/
- [P2] Plaid institutions: https://plaid.com/docs/institutions/
- [P3] Plaid OAuth and re-auth: https://plaid.com/docs/link/oauth/
- [P4] Plaid Transactions: https://plaid.com/docs/api/products/transactions/
- [P5] Plaid errors: https://plaid.com/docs/errors/
- [P6] Plaid legal index: https://plaid.com/legal/
- [S1] SimpleFIN Bridge: https://beta-bridge.simplefin.org/
- [S2] SimpleFIN protocol: https://www.simplefin.org/protocol.html
- [T1] Teller: https://teller.io/
- [T2] Teller docs: https://teller.io/docs

Not retrievable (404, do not retry): simplefin.org/bridge, teller.io/pricing, Plaid rate-limit URL guesses. Facts that would need them are Unverified below because the pages are missing, gated, or need sign-up/payment.

## Comparison by option

| | Plaid | SimpleFIN Bridge | Teller | CSV only |
|---|---|---|---|---|
| **Cost** | Pay as You Go, Growth (12-month commitment), Custom. Limited Production sandbox: up to 200 API calls per product on live data, free [P1]. Per-call prices: Unverified (not captured from the pricing page; full rates may need sales contact). Whether a developer may use it for personal, non-commercial use: Unverified (Developer Policy via [P6] not read in full; needs legal review). | $1.50/month or $15/year plus tax, up to 25 institutions and 25 apps [S1]. Personal-use terms: Unverified (terms not read). | Free developer tier up to 100 live connections; production $0.30 per enrollment per month for transactions, $0.10 per balance call [T1]. Personal-use terms: Unverified (terms not read). | Free. |
| **Coverage (general)** | Over 10,000 US/CA institutions [P2]. | "Numerous" institutions [S1]; list Unverified (no public list found). | Over 7,000 institutions claimed [T1]. | Any institution with an export. |
| **Huntington** | Unverified (Plaid coverage explorer [P2] is interactive; cannot be checked without it). | Unverified. | Unverified. | CSV format still to verify (issue #1). |
| **Capital One** | Unverified (coverage explorer is interactive). Plaid's OAuth page names Capital One as a US OAuth bank with 12-month consent [P3]. | Unverified. | Unverified. | As above. |
| **Apple Card** | Unverified. | Unverified. | Unverified. | As above. |
| **Vanguard** | Unverified (interactive coverage tool), including holdings and investment transactions. | Unverified. | Unverified. | Meaning of Vanguard rows unresolved (issue #1). |
| **History** | Default 90 days, max 730 days for Transactions [P4]. | Optional date-range filter [S2]; depth Unverified (operator-dependent, not documented). | Unverified. | Whatever the export holds. |
| **Consent flow** | Plaid Link, OAuth at many banks. Expired consent gives `ITEM_LOGIN_REQUIRED` [P5]; update mode re-runs the flow [P3]. Bank-side revocation takes 24-48 h to show; `PENDING_DISCONNECT` webhook one week before expiry [P3]. | User creates a token at the Bridge, gives it to the app, the app claims an access URL. A 403 on claim means the token may be compromised [S2]. Revocation mechanics: Unverified (not stated on fetched pages). | Teller Connect [T2]. Re-auth and revocation: Unverified (docs not fully retrievable). | Manual download and upload. |
| **Operational limits** | Rate-limit values: Unverified (rate-limit page returned 404). Sync cursor valid at least one year [P4]. Redirect URI optional for desktop web and required on mobile [P3]; webhooks need a reachable URL, fit for this app Unverified. | Access URL uses HTTP Basic Auth over TLS; no inbound callback in the protocol, the app polls [S2]. Rate limits: Unverified (not stated on fetched pages). | mTLS details, callbacks, rate limits: Unverified (pricing/limits pages 404 or need sign-up). | None. |
| **Privacy** | Data flows through Plaid to the developer; an End User Privacy Policy is listed on the legal page [P6]. Sharing terms: Unverified (not read). | Bridge operator holds credentials and data; privacy handling: Unverified (privacy policy not read; [S1]). | Unverified (policy not read). | Data never leaves the owner (no third party involved). |

## Fit with the existing model

- **Sign convention.** Plaid: positive is money out, negative is money in [P4]. This may be the opposite of the app's convention, so an adapter must map it explicitly and document it per importer. SimpleFIN and Teller sign conventions: unverified; define the adapter only after checking their docs. Currency must stay explicit; SimpleFIN allows custom currencies (such as points) [S2], so an adapter must reject non-currency units.
- **Provenance and import batches.** Each sync run would be a batch tagged with source (provider name, provider account id) and run id, so it can be corrected or undone like a CSV batch. Plaid's `transaction_id` and cursor give stable source ids [P4]; others Unverified.
- **Reimport de-duplication against CSV rows.** Provider ids do not exist in CSV exports, so matching CSV against connected rows needs a fuzzy rule (account, date, amount, normalized description) or a rule that an account is either CSV or connected for a date range. Pending-to-posted changes (Plaid `pending_transaction_id` [P4]) must be handled to avoid double counting. Owner decision needed.
- **Private versus household.** A connection belongs to one person and links accounts that inherit private or household visibility like CSV-added accounts. Credentials or access URLs must be stored only for the owner, never shown in logs or errors, and a shared account's connection should be manageable only by its owner (open question).
- **Internal transfers, card payments, investments.** Connected feeds do not resolve report semantics; the same rules as CSV apply.

## Unverified recommendation

Stay CSV-only for the first release, as already decided. If the owner later wants automation, evaluate SimpleFIN Bridge first (small fixed cost [S1], simple polling protocol, no hosted callback [S2]), then Teller's free developer tier [T1], and treat Plaid as last because its personal-use terms are unconfirmed ([P1], [P6]; Unverified) and it must not be a paid dependency (AGENTS.md). This is a judgment from incomplete evidence, not a verified fact; institution coverage in particular must be checked before any choice.

## Open owner decisions

1. Is any automatic connection worth exploring after the CSV release, and what monthly cost is acceptable?
2. Is a third-party bridge holding bank credentials acceptable privacy-wise?
3. Should connected and CSV data coexist on one account, and what is the de-duplication rule?
4. Who may manage a connection on a shared household account?
5. Who verifies institution coverage (needs a sign-up or dashboard access, which this issue forbids)?
