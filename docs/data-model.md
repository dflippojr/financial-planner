# Core financial data model

Updated: 2026-09-27. This document records the storage contract introduced by issue #4. Provider parsing, deduplication behavior, access-enforcing query APIs, categories, transfer links, and reporting are separate issues.

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
- Each transaction identifies its batch, source row number, optional provider transaction ID, and a fingerprint field reserved for issue #6. The fingerprint's logical inputs are account, date, amount, and description; issue #6 owns normalization, computation, uniqueness, and reimport behavior.
- `original_fields` is a JSON object on each transaction. It keeps the imported row's original field names and values without retaining a batch-level raw file. These values are financial data: never write them to logs, errors, fixtures, issues, or screenshots.
- Deleting related people, accounts, or batches is protected at the database relationship level. Correction and undo features must archive records rather than hard-delete them.

The committed `synthetic_demo` fixture contains invented names, hashes, descriptions, and amounts only. It is suitable for schema demonstrations, not provider-format verification.
