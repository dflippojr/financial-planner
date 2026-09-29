# Core financial data model

Updated: 2026-09-29. This document records the storage contract introduced by issue #4, the reimport rules from issue #6, and correction history from issue #31. Provider parsing, access-enforcing query APIs, categories, transfer links, and reporting are separate issues.

## People and sharing

- `Person` is the financial-domain profile for one Django user.
- `Household` groups people through dated `Membership` rows. A database constraint allows at most one membership without an end date for each person. Ended rows remain as history.
- Every `Account` always has one owner. Its scope is either `private`, with no household, or `household`, with exactly one household. Transactions and import batches inherit their visibility from their account instead of copying a scope that could drift.
- Account ownership and membership use protected foreign keys. The lifecycle actions in issue #3 must transfer ownership or change scope before removing related records. Issue #3 also owns access-filtered query APIs and authorization enforcement.
- Accounts, transactions, and import batches use `active` or `archived` status. Archiving requires an archive timestamp and preserves provenance and transaction history.

## Money and transaction meaning

- `Transaction.amount_minor` is an exact signed 64-bit integer. Negative means money out and positive means money in for every account type, including credit cards. Balances have separate future semantics.
- Every account and transaction stores its currency explicitly. V1 accepts only `USD`; database constraints reject another currency.
- V1 account types are `checking`, `savings`, `credit_card`, and `investment`.
- Ordinary imported rows have kind `cash_flow`. Unverified Vanguard activity has the neutral `investment_activity` kind and retains its opaque source fields. It must not affect income or spending reports until issues #15 and #9 define verified treatment. The schema intentionally has no invented symbol, quantity, or price fields.

## Import provenance

- An `ImportBatch` identifies its account, importing person, source provider, user-selected date range, import timestamp, and SHA-256 hash of the discarded source file. The application must discard uploaded CSV contents after a successful import.
- Each transaction identifies its batch, source row number, optional provider transaction ID, and a SHA-256 fingerprint of account id, ISO date, signed minor amount, and the stored description (newline-separated). The fingerprint is computed at import and never rewritten. It is not unique: the same fingerprint may occur more than once on an account when the export contains legitimate repeated purchases. Overlap detection counts active fingerprints on the target account only (the account the importer can already see); archived rows do not count, so an undone batch can be reimported. A matching fingerprint on a different account is not an overlap.
- Reimport preview reports new, duplicate, and invalid counts. Commit creates an import batch only when at least one row is new, stores those rows with batch provenance and original field names/values, and discards the staged CSV. Duplicate valid rows are left on their original batches. Undo archives one active batch and only that batch's transactions.
- `original_fields` is a JSON object on each transaction. It keeps the imported row's original field names and values without retaining a batch-level raw file. These values are financial data: never write them to logs, errors, fixtures, issues, or screenshots.
- Deleting related people, accounts, or batches is protected at the database relationship level. Correction and undo features must archive records rather than hard-delete them.

The committed `synthetic_demo` fixture contains invented names, hashes, descriptions, and amounts only. It is suitable for schema demonstrations, not provider-format verification.

## Transaction review and correction

- The transaction review UI starts with `Transaction.objects.visible_to(person)` on every request, excludes archived transactions, and orders by transaction date and then primary key descending.
- User corrections may change only `transaction_date`, `description`, and `amount_minor`. Amount entry converts decimal major units directly to integer minor units without using binary floating point.
- Each changed field writes one append-only `TransactionCorrectionHistory` row in the same database transaction as the correction (lock order: memberships, then account, then transaction, then history insert). A no-op save writes none. Amounts on history rows are integer minor units plus currency. History is listed through `TransactionCorrectionHistory.objects.visible_to`, which is `Transaction.objects.visible_to` on the parent row, including shared and archived accounts. History values are never written to logs or error messages.
- Corrections never replace `original_fields` or change the transaction's account, import batch, source row number, fingerprint, currency, or kind. Those fields continue to describe the imported record and its provenance.
- The list exposes category as `Uncategorized` for now. Issue #8 owns category persistence, assignment, and transfer/exclusion semantics after its product decisions are resolved.
