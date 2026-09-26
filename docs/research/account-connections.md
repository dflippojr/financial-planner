# Optional automatic account connections: research

Status: research only (issue #20). No integration, dependency, key, or sign-up was made. The first release stays CSV-only (docs/requirements.md). Adopting any option needs a separate owner decision and a new issue. No personal financial data appears here.

Every source key below (P1..T4) was accessed on **2026-09-26** and is cited as [key, 2026-09-26] near each claim. Sources were fetched as page summaries; anything not stated on a fetched primary page is marked **unverified**, and nothing was filled from memory. Many per-institution facts could not be verified because coverage lists are interactive or gated behind dashboards or sign-ups.

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
- [P7] Plaid legal policies (accessed 2026-09-26): https://plaid.com/legal/
- [S1] SimpleFIN Bridge: https://beta-bridge.simplefin.org/
- [S2] SimpleFIN protocol: https://www.simplefin.org/protocol.html
- [S3] SimpleFIN security (accessed 2026-09-26): https://beta-bridge.simplefin.org/info/security
- [S4] SimpleFIN privacy (accessed 2026-09-26): https://beta-bridge.simplefin.org/info/privacy
- [S5] SimpleFIN terms (accessed 2026-09-26): https://beta-bridge.simplefin.org/info/terms
- [S6] SimpleFIN supported institutions (interactive; accessed 2026-09-26): https://beta-bridge.simplefin.org/search-institutions
- [T1] Teller: https://teller.io/
- [T2] Teller docs: https://teller.io/docs
- [T3] Teller developer terms (last revised June 18, 2021; accessed 2026-09-26): https://teller.io/legal/developer/terms
- [T4] Teller end-user privacy policy (last revised November 10, 2021; accessed 2026-09-26): https://teller.io/legal/user/privacy
- [T5] Teller institution API docs (accessed 2026-09-26): https://teller.io/docs/api/institutions
- [T6] Teller public institutions directory response (unauthenticated; accessed 2026-09-26): https://api.teller.io/institutions
- [T7] Teller account transactions API docs (accessed 2026-09-26): https://teller.io/docs/api/account/transactions

Not retrievable (404, do not retry): simplefin.org/bridge, teller.io/pricing, Plaid rate-limit URL guesses. Facts that would need them are Unverified below because the pages are missing, gated, or need sign-up/payment.

## Comparison by option

| | Plaid | SimpleFIN Bridge | Teller | CSV only |
|---|---|---|---|---|
| **Cost** | Pay as You Go, Growth (12-month commitment), Custom. Limited Production sandbox: up to 200 API calls per product on live data, free [P1]. Per-call prices: Unverified (not captured from the pricing page; full rates may need sales contact). Whether a developer may use it for personal, non-commercial use: Unverified; legal policies are listed at [P7]. | $1.50/month or $15/year plus tax, up to 25 institutions and 25 apps [S1]. Personal-use terms: Unverified ([S5]). | Free developer tier up to 100 live connections; production $0.30 per enrollment per month for transactions, $0.10 per balance call [T1]. Personal/non-commercial use: Unverified ([T3], [T4]); required terms do not clearly settle this project's self-hosted personal use. | No provider fee; the project chooses CSV for the first release (docs/requirements.md). |
| **Coverage (general)** | Over 10,000 US/CA institutions [P2]. | "Numerous" institutions [S1]; list Unverified (no public list found on [S1]). | Over 7,000 institutions claimed [T1]. | Planned workflow: CSV exports from the four named institutions (docs/requirements.md, docs/backlog.md #1); export availability is not confirmed. |
| **Huntington** | Unverified: Plaid's coverage explorer [P2] is interactive and could not be queried. | Unverified: the public supported-institutions page is interactive and its fetched page does not enumerate providers ([S6]); this institution's outcome is not established. | Directory entry: `Huntington` (id `huntington`); products `verify.instant`, `balance`, `transactions`, `identity` ([T5], [T6], accessed 2026-09-26). This records a public directory listing only, not account data. | Planned CSV import; shape still to verify with synthetic examples (docs/requirements.md, docs/backlog.md #1). |
| **Capital One** | Unverified (coverage explorer is interactive). Plaid's OAuth page names Capital One as a US OAuth bank with 12-month consent [P3]; that is not a coverage confirmation for this app's needs. | Unverified: the public supported-institutions page is interactive and its fetched page does not enumerate providers ([S6]); this institution's outcome is not established. | Directory entry: `CapitalOne` (id `capital_one`); products `verify.instant`, `balance`, `transactions`, `identity`, `payments` ([T5], [T6], accessed 2026-09-26). This records a public directory listing only, not account data. | Planned CSV import; shape to verify (docs/requirements.md, docs/backlog.md #1). |
| **Apple Card** | Unverified: no fetched page in this research lists per-institution support, and no sign-up or dashboard access was allowed ([P2] interactive). | Unverified: the public supported-institutions page is interactive and its fetched page does not enumerate providers ([S6]); this institution's outcome is not established. | No exact Apple Card entry in the current full public directory ([T5], [T6], accessed 2026-09-26). Similarly named Apple Bank for Savings and Apple River State Bank are different directory entries and do not establish Apple Card support. | Planned CSV import; shape to verify (docs/requirements.md, docs/backlog.md #1). |
| **Vanguard** | Unverified, including holdings and investment transactions: [P2] is interactive and no fetched page describes investment data for this institution. | Unverified: the public supported-institutions page is interactive and its fetched page does not enumerate providers ([S6]); this institution's outcome is not established. | No exact Vanguard entry in the current full public directory ([T5], [T6], accessed 2026-09-26). Similarly named BayVanguard Bank is a different directory entry and does not establish Vanguard support. | Planned CSV import; the meaning of Vanguard rows is unresolved (docs/requirements.md, docs/backlog.md #1). |
| **History** | Default 90 days, max 730 days for Transactions [P4]. | Optional date-range filter [S2]; depth Unverified (operator-dependent, not documented). | Transactions endpoint returns all transactions and accepts inclusive `start` and `end` date filters; exact maximum history remains Unverified ([T7], accessed 2026-09-26). | Limited to what an export contains; the exports are unverified (docs/backlog.md #1). |
| **Consent flow** | Plaid Link, OAuth at many banks. Expired consent gives `ITEM_LOGIN_REQUIRED` [P5]; update mode re-runs the flow [P3]. Bank-side revocation takes 24-48 h to show; `PENDING_DISCONNECT` webhook one week before expiry [P3]. | User creates a token at the Bridge, gives it to the app, the app claims an access URL. A 403 on claim means the token may be compromised [S2]. Revocation mechanics: Unverified (not stated on fetched pages). | Teller Connect [T2]. Re-auth and revocation: Unverified (docs not fully retrievable). | Manual upload with column mapping and preview, and no stored bank credentials (docs/requirements.md). |
| **Operational limits** | Rate-limit values: Unverified (rate-limit page returned 404). Sync cursor valid at least one year [P4]. Redirect URI optional for desktop web and required on mobile [P3]; webhooks need a reachable URL, fit for this app Unverified. | Access URL uses HTTP Basic Auth over TLS; no inbound callback in the protocol, the app polls [S2]. Rate limits: Unverified (not stated on fetched pages). | Enrollments refresh at least daily; `transactions.processed` webhook fires when new data is available ([T7], accessed 2026-09-26). Exact numeric rate limits, webhook callback requirements and public URL fit/reachability, and mTLS details: Unverified ([T7]; [T1] pages do not settle these). | No provider limits; import limits are not yet specified. |
| **Privacy** | Data flows through Plaid to the developer; an End User Privacy Policy is listed on the legal page [P7]. Sharing terms: Unverified (not read). Personal/non-commercial permission: Unverified; pricing language does not establish it [P7]. | Bank credentials are handled by MX and do not touch SimpleFIN's servers [S3]. Credentials go to a third party; user-created access tokens let selected apps read connected data [S4]. Staff debugging access requires the user's explicit grant and is logged [S3]. | The end-user privacy policy (last revised November 10, 2021) says Teller may collect login information and security tokens and shares account data with app developers and service providers [T4]. Exact credential storage and access practices are not established by the cited policy. Developer terms (last revised June 18, 2021) require end users to accept Teller's end-user terms and privacy policy [T3]. Personal/non-commercial permission for this project: Unverified. | Files are uploaded by the user and no bank credentials are stored (docs/requirements.md); retention of uploaded files is an open question there. |

## Fit with the existing model

Everything in this section is a **design implication or recommendation**, not a provider fact. Provider facts carry source keys; project requirements are cited by path (AGENTS.md, docs/requirements.md, docs/backlog.md).

- **Sign convention.** Plaid: positive is money out, negative is money in [P4]. AGENTS.md requires defining debit and credit signs for every importer, so design implication: any adapter should map this explicitly, since the app's convention is not yet fixed here. SimpleFIN and Teller sign conventions: unverified; define the adapter only after checking their docs. Currency must stay explicit; SimpleFIN allows custom currencies (such as points) [S2], so a design recommendation is that an adapter reject non-currency units (AGENTS.md: explicit currency).
- **Provenance and import batches.** Recommendation, from AGENTS.md (provenance so imports can be corrected or undone): each sync run would be a batch tagged with source (provider name, provider account id) and run id, so it can be corrected or undone like a CSV batch. Plaid's `transaction_id` and cursor give stable source ids [P4]; others Unverified.
- **Reimport de-duplication against CSV rows.** Provider ids do not exist in CSV exports, so matching CSV against connected rows needs a fuzzy rule (account, date, amount, normalized description) or a rule that an account is either CSV or connected for a date range. Pending-to-posted changes (Plaid `pending_transaction_id` [P4]) must be handled to avoid double counting. Reimports must not double count (AGENTS.md). Owner decision needed.
- **Private versus household.** Design implication of docs/requirements.md (accounts are private or explicitly shared; no stored bank credentials by default): a connection would belong to one person, and its accounts would inherit private or household visibility like CSV-added accounts. Storing credentials or access URLs would conflict with the current no-stored-credentials rule and needs an owner decision; if allowed, they or access URLs should be stored only for the owner and never shown in logs or errors, and a shared account's connection should be manageable only by its owner (open question).
- **Internal transfers, card payments, investments.** AGENTS.md requires explicit report semantics before these affect income or spending; no fetched provider page was used to resolve them, so the same open rules as CSV apply (docs/backlog.md #1).

## Unverified recommendation

Stay CSV-only for the first release, as already decided (docs/requirements.md). If the owner later wants automation, evaluate SimpleFIN Bridge first (premises: fixed cost $1.50/month or $15/year [S1]; polling protocol with no inbound callback [S2]), then Teller's free developer tier (premise: [T1]), and treat Plaid as last because its personal-use terms are unconfirmed ([P1], [P6]; Unverified) and AGENTS.md and docs/requirements.md say Plaid must not be a paid dependency now. This is a judgment from incomplete evidence, not a verified fact; institution coverage in particular must be checked before any choice.

## Open owner decisions

1. Is any automatic connection worth exploring after the CSV release, and what monthly cost is acceptable?
2. Is a third-party bridge holding bank credentials acceptable privacy-wise?
3. Should connected and CSV data coexist on one account, and what is the de-duplication rule?
4. Who may manage a connection on a shared household account?
5. Who verifies institution coverage (needs a sign-up or dashboard access, which this issue forbids)?
