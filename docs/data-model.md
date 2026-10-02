# Core financial data model

Updated: 2026-10-01. This document records the storage contract introduced by issue #4, the reimport rules from issue #6, correction history from issue #31, category/transfer/refund rules from issue #8, cash-flow reporting from issue #9, recurring-charge series from issue #17, household share modes from issue #30, SimpleFIN Bridge connections from issue #67, and net worth from balance snapshots from issue #69. Provider parsing and Vanguard-specific activity meaning remain separate issues.

## People and sharing

- `Person` is the financial-domain profile for one Django user.
- `Household` groups people through dated `Membership` rows. A database constraint allows at most one membership without an end date for each person. Ended rows remain as history.
- Every `Account` always has one owner. Its scope is either `private`, with no household and an empty share mode, or `household`, with exactly one household and a share mode of `co_owned` or `lent`. A database constraint rejects any other combination. Transactions and import batches inherit their visibility from their account instead of copying a scope that could drift. `Account.objects.visible_to` still follows current household membership, so a lent account that reverts to private is immediately invisible to other members.
- Share modes (issue #30): co-owned accounts belong to the household; any current member may unshare or archive them, and if the owner leaves, ownership moves to the longest-standing remaining member (or the last member takes them private). Lent accounts stay owned by the lender; other members may view and edit transactions while they belong to the household, but only the owner may unshare, archive, or change the mode. If that owner leaves, the account becomes private to them and history is preserved. Existing household rows migrate as co-owned.
- Account ownership and membership use protected foreign keys. The lifecycle actions in issue #3 must transfer ownership or change scope before removing related records. Issue #3 also owns access-filtered query APIs and authorization enforcement.
- Accounts, transactions, and import batches use `active` or `archived` status. Archiving requires an archive timestamp and preserves provenance and transaction history.

## Money and transaction meaning

- `Transaction.amount_minor` is an exact signed 64-bit integer. Negative means money out and positive means money in for every account type, including credit cards. Balances have separate future semantics.
- Every account and transaction stores its currency explicitly. V1 accepts only `USD`; database constraints reject another currency.
- V1 account types are `checking`, `savings`, `credit_card`, and `investment`.
- Ordinary imported rows have kind `cash_flow`. Unverified Vanguard activity has the neutral `investment_activity` kind and retains its opaque source fields. It must not affect income or spending reports until issues #15 and #9 define verified treatment. The schema intentionally has no invented symbol, quantity, or price fields.

## Import provenance

- An `ImportBatch` identifies its account, importing person, source provider, user-selected date range, import timestamp, and SHA-256 hash of the discarded source file. The application must discard uploaded CSV contents after a successful import.
- Each transaction identifies its batch, source row number, optional provider transaction ID, and a SHA-256 fingerprint of account id, ISO date, signed minor amount, and the stored description (newline-separated). The fingerprint is computed at import and never rewritten. It is not unique: the same fingerprint may occur more than once on an account when the export contains legitimate repeated purchases. Overlap detection counts active fingerprints on the target account only (the account the importer can already see); archived rows do not count, so an undone batch can be reimported. A matching fingerprint on a different account is not an overlap. The Huntington checking profile stores `Transaction Number` as `source_transaction_id` and does not use it for overlap matching.
- Reimport preview reports new, duplicate, and invalid counts. Commit creates an import batch only when at least one row is new, stores those rows with batch provenance and original field names/values, and discards the staged CSV. Duplicate valid rows are left on their original batches. Undo archives one active batch and only that batch's transactions.
- `original_fields` is a JSON object on each transaction. It keeps the imported row's original field names and values without retaining a batch-level raw file. These values are financial data: never write them to logs, errors, fixtures, issues, or screenshots.
- Deleting related people, accounts, or batches is protected at the database relationship level. Correction and undo features must archive records rather than hard-delete them.

The committed `synthetic_demo` fixture contains invented names, hashes, descriptions, and amounts only. It is suitable for schema demonstrations, not provider-format verification.

## Transaction review and correction

- The transaction review UI starts with `Transaction.objects.visible_to(person)` on every request, excludes archived transactions, and orders by transaction date and then primary key descending.
- User corrections may change `transaction_date`, `description`, and `amount_minor`. Category assignment, transfer exclusions, and refund links are separate writes from issue #8 and also append correction history. Amount entry converts decimal major units directly to integer minor units without using binary floating point.
- Each changed field writes one append-only `TransactionCorrectionHistory` row in the same database transaction as the correction (lock order: memberships, then account, then transaction, then history insert). A no-op save writes none. Amounts on history rows are integer minor units plus currency. History is listed through `TransactionCorrectionHistory.objects.visible_to`, which is `Transaction.objects.visible_to` on the parent row, including shared and archived accounts. History values are never written to logs or error messages.
- Corrections never replace `original_fields` or change the transaction's account, import batch, source row number, fingerprint, currency, or kind. Those fields continue to describe the imported record and its provenance.
- The list shows the assigned household category, `Uncategorized` when none is assigned, and `Transfer` when both legs of an exclusion pair are visible to the viewer and still active.

## Categories, transfers, and refunds

- Each household has an editable category list seeded with Income, Groceries, Dining, Transportation, Housing, Utilities, Health, Insurance, Shopping, Entertainment, Subscriptions, Travel, Education, Personal care, Gifts and donations, Fees and interest, Taxes, Uncategorized, and the system Transfer category. Transfer cannot be assigned by hand and is never itself what excludes a row from income and spending.
- `Transaction.category` is optional. Clearing it leaves the transaction uncategorized.
- A `TransferPair` records two transactions (`leg_a_id` < `leg_b_id`), a confidence reading (`high` or `low`), a list of reasons, a kind (`transfer` or `card_payment`), and a status. High confidence means each leg has exactly one counterpart in the configured date window. Auto-marked and confirmed pairs are excluded from income and spending only when both legs are visible to the person asking and both remain active. Archiving, unsharing, or undoing an import of an account or batch revalidates every pair with a leg there even when the other leg is not visible to the actor, unmarks pairs that no longer hold, and restores snapshot categories on surviving legs. Suggested pairs are not excluded until confirmed. Suggestions whose legs no longer cancel are invalidated so they stop occupying those transactions, and pairing then recomputes. Undoing restores the category ids stored at mark time. Dismissed and undone pairs are not auto-marked again.
- Pairing requires opposite signs, equal absolute amounts, different accounts, dates within the household match window (default 5 days), and at least one person who can see both accounts. Detection runs on the actor's visible transactions only. Saving a new match window revalidates suggested and auto-marked pairs, undoes auto-marked pairs that no longer fit the window (restoring snapshot categories), leaves confirmed pairs, and then refreshes pairing.
- A `RefundLink` is a manual link from a refund transaction to an original. The refund must be a positive amount and the original a negative purchase of the same transaction kind; otherwise the link is rejected with a safe message. The refund stores the inherited category on itself. Recategorizing the original copies that category onto linked refunds and records correction history. `income_and_spending_totals` never treats a linked refund as income; it subtracts the refund's own amount from spending in the refund's stored category without requiring the original to remain visible and without exposing the original.

## Recurring charges

- Detection is a local deterministic function over `Transaction.objects.visible_to` for the viewer. It never includes investment activity, positive amounts, or rows excluded by a visible transfer/card-payment pair (#8 helpers).
- A `RecurringSeries` belongs to one person and has an `is_active` flag. Visibility is that person plus a check that every member transaction is still `visible_to` them, so a private account cannot leak through another household member's suggestions, totals, or errors.
- Series identity for dismiss is a SHA-256 fingerprint of the sorted member transaction ids. A dismissed fingerprint is not suggested again until that set of transactions changes. Confirmed series stay confirmed and absorb later occurrences of the same merchant, cadence, and amount band (within 25% of that series' typical amount). One confirmed row is never reassigned onto a second amount cluster, and a refresh never writes a fingerprint that already belongs to another series for that person. Refresh keeps a confirmed series active while at least one of its member occurrences is still eligible, even if those leftovers cannot form a detected chain; it marks the series inactive and clears members only when none remain. A later eligible chain of the same merchant, cadence, and amount band can reactivate it; inactive confirmed series are omitted from Recurring-page totals.
- Cadences are weekly, biweekly, monthly, quarterly, and annual, each with a few days of calendar tolerance. Two occurrences are stored as `possible`; three or more at a regular interval are `suggested` until confirmed. Exact amounts raise confidence above varying amounts (still capped at 25% from the selected cadence chain's median). When a picked chain fails that band, detection drops the farthest member from this pick and re-picks before falling back to amount clusters. Same-merchant noise is clustered against that chain, not the merchant's overall median.
- Typical amount, monthly equivalent, and annual equivalent are integer minor units (annual is typical absolute amount times occurrences per year; monthly is that annual figure integer-divided by 12). The Recurring page lists confirmed series and those totals; suggestions on the same page can be confirmed or dismissed.

## Cash flow over time

- Period totals call `income_and_spending_totals` for each window so transfer, refund, and investment rules are not re-derived. Optional account lists are intersected with `Account.objects.visible_to`.
- Missing-import flags use active `ImportBatch` date ranges on those same visible selected accounts. A period is flagged when any selected account has no overlapping active batch; the flag is separate from the zero amounts.
- Unverified `investment_activity` rows remain omitted from income and spending; the home view states that when an investment account is included in the selection.

## SimpleFIN Bridge (verified from https://www.simplefin.org/protocol.html, 2026-10-01)

Facts below are from the published protocol. They are not inferred from a live bank export.

**Claim.** The setup token the member pastes is Base64 of a claim URL. The app POSTs to that URL once and stores the Access URL from a 200 response. The Access URL includes HTTP Basic credentials. Only `https` claim and access URLs are used. A 403 on claim means the token does not exist or was already claimed and may indicate compromise; the UI says so without echoing the token or URL.

**Fetch.** Account data is `GET {access_url}/accounts` with optional `start-date` and `end-date` (Unix epoch; start inclusive, end exclusive). `pending=1` would include pending transactions; the default omits them. `balances-only=1` omits transaction arrays. `version=2` selects this protocol version. A 403 on `/accounts` means authentication failed or access was revoked. A 402 means payment is required.

**Account Set.** `errlist` is the structured error list (required in v2). `errors` is a deprecated array of display strings. `connections` and `accounts` are required. Each Error has `code`, `msg` (user-facing), and optional `conn_id` / `account_id`. Prefixes are `gen`, `con`, and `act`. Unknown subcodes fall back to the prefix. `msg` values are sanitized before display and never logged with access URLs.

**Account.** `id` is unique within a Connection (not globally). `name` is the account label. `conn_id` ties it to a Connection. `currency` is an ISO 4217 code, or a URL for a custom currency (points, miles). This app rejects any currency that is not ISO 4217 `USD`. `balance` and optional `available-balance` are numeric strings as of `balance-date` (Unix epoch). The protocol Account object has no account-type field; institution comes from the matching Connection `name`. The member chooses the app account type when linking or creating.

**Transaction.** `id` is unique within that SimpleFIN account. `posted` is a Unix epoch; it may be `0` when pending. `amount` is a numeric string: **positive means money deposited into the account** (same sign as this app). `description` is required. Optional `pending` is true when not yet posted (default false/absent). This app imports only posted transactions (`pending` not true and `posted` not 0), using the local calendar date of `posted` as `transaction_date`, and stores `id` in `source_transaction_id`.

**Storage.** `SimpleFinConnection` belongs to one `Person`. The Access URL is Fernet-encrypted (`FIELD_ENCRYPTION_KEY`) and is never rendered, logged, or placed in errors. `AccountLink` maps one app `Account` to one SimpleFIN account id on that connection, with a cut-over date and mode `transactions` (checking, savings, credit card) or `balances_only` (investment). `BalanceSnapshot` stores one dated balance in minor units per (`account`, `date`, `source`) where source is `simplefin` or `manual`, with optional `ImportBatch` provenance and an optional `note` (manual entry only). Each linked account that receives new rows in a sync gets an `ImportBatch` with source `simplefin`. Re-syncs skip an active transaction that already has the same account and `source_transaction_id`. Disconnect deletes the connection and ciphertext and keeps imported rows. Only the owner can see or manage the connection.

## Net worth from balances (issue #69)

Net worth is computed only from `BalanceSnapshot` rows. Transfers, card payments, and investment transactions do not enter the series.

**Sign convention.** Checking, savings, and investment amounts are assets as stored (positive means money held; an overdraft is a negative asset). Credit-card balances are liabilities, and the stored sign depends on source:

- `simplefin` keeps the SimpleFIN protocol value: a negative credit-card balance is money owed. Net worth uses that signed amount, so the owed magnitude subtracts.
- `manual` stores the amount the member typed. For a credit card that is the amount owed as a positive number; an overpayment is negative. Net worth subtracts that stored amount.

A manual row never replaces a SimpleFIN row. They can share an account and date because uniqueness includes `source`. When both exist on the same date, the series uses `simplefin`.

**Monthly series.** Each month in the selected range uses the last snapshot on or before that month's as-of date (`min` of the calendar month end and the range end). A month with no snapshot for an account carries the last earlier value forward and is flagged carried forward. Months before an account's first snapshot omit that account from totals and flag the month. Visible accounts are the viewer's private accounts plus household accounts; a household-only filter drops private accounts. Another member's private accounts are never in totals, the chart payload, or errors.

