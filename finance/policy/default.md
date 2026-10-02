Privacy and data policy
=======================

This is a draft default template for the household's self-hosted Financial Planner. The operator who runs the basement PC may replace this text for their install. The owner of this repository reviews and approves the wording before it is treated as the household's policy.

This app is for people who live in one household and share a computer on the home network (and Tailscale). It is not a commercial cloud product. Please read this before you connect an outside AI provider or share household accounts.

What the app stores, and where
------------------------------

The app stores the records you and other members create or import. That includes:

- your username, display name, and sign-in credentials (a password hash, optional Google sign-in link, and recovery-code digests);
- household membership;
- financial accounts, transactions, categories, rules, import batches, transfers, recurring series, planned items, savings goals, and balance snapshots;
- SimpleFIN connection material needed to refresh linked accounts;
- your acceptances of this policy, including which version you accepted and when.

Those records live on the household's basement PC, in the app's PostgreSQL database. Nightly backups are written to a second local disk on that same PC and kept for a limited time (the current retention is fourteen nightly copies and eight weekly copies, unless the operator changes it). Backups contain the same kinds of data as the live database, including data that was later archived or deleted in the app, until those backup files rotate out.

The operator of the basement PC can read the database and backups. The app does not send your financial records to the people who wrote this software.

Bank connections (SimpleFIN)
----------------------------

If you connect a bank or card through SimpleFIN Bridge, you paste a setup token from SimpleFIN. The app claims that token and stores an access URL so it can download balances and posted transactions. Bank login credentials are handled by SimpleFIN's bridge, not typed into this app. SimpleFIN's own terms and privacy notice then apply to that connection, in addition to this policy.

Google sign-in
--------------

Google sign-in is optional. If you use it, you authenticate with Google and this app stores Google's subject identifier so it can recognize you later. The app does not keep Google access or refresh tokens. Google's terms and privacy policy apply to that sign-in.

What other household members can see
------------------------------------

Accounts are private until someone explicitly shares them with the household.

- A private account, and everything in it, is visible only to its owner. Other members cannot see it in lists, search, totals, exports, or error messages.
- A household-shared account is visible and editable by every current member. When an account is shared, its full history follows it. Shared accounts may be co-owned by the household or lent by the owner; lent accounts leave with the owner if that owner leaves the household.

You should treat household-shared data as visible to everyone who is currently in the household.

Outside AI providers
--------------------

This app can later let a member connect their own outside AI provider (for example a cloud assistant). That is optional. The app does not require an AI provider to import transactions, review spending, or use the rest of the household tools.

If you connect an outside AI provider:

- The app may send that provider information you can already see: your private accounts and, when household AI is allowed, household-shared accounts and transactions visible to you.
- Other members' private data is never sent.
- Once you send data to a provider, that provider's own terms, privacy policy, retention, and training practices apply. This household cannot control what the provider does with data after it leaves the basement PC.
- You may send household-shared data to your provider only while every current household member is in acceptance of this policy (they have accepted the latest material version, or a later version). If anyone has not, household-shared data is withheld from every member's AI backend. You may still use AI on your own private accounts if you yourself are in acceptance.
- Other members do not get a separate prompt each time you use AI. Their protection is this policy, plus the rule that shared-data AI stays off until everyone currently in the household has accepted it.

If you do not accept this policy, you can still use the app. You cannot connect or use an AI backend until you accept.

Export and deletion
-------------------

You can download a zip of the records that are visible to you (your private accounts plus household-shared accounts). That export includes your policy-acceptance rows. It never includes another member's private accounts.

You can permanently delete an account you own, including its transactions. Other transactions and import batches are archived (hidden) rather than erased, so imports can still be undone and corrections traced.

There is not yet a single "delete everything about me" button. Leaving the household ends your access to shared accounts. Removing the rest of a person's data is an open product decision. Until that exists, deleting each private account you own and leaving the household is the in-app path. Copies can remain in backups until those files rotate out.

Leaving the household
---------------------

Any current member may leave the household themselves from Account settings. No member can remove someone else in the app; an operator command on the basement PC can evict a member. After you leave, you lose access to household-shared accounts. Co-owned shared accounts stay with the remaining members. Lent accounts you own become private to you. If you were the last member, remaining shared accounts become private to you.

A member joining or leaving changes household AI immediately: shared-data AI is allowed only while every current member is in acceptance.

Versions and acceptance
-----------------------

This policy is stored by version. Only a version the operator marks as material asks members to accept again. Typo fixes and other non-material edits do not take anyone out of acceptance. You are in acceptance when you have accepted the latest material version, or any later version.

You can read the current text any time, including before you sign in. Account settings shows whether you are in acceptance and lets you accept the current version.

If you have questions about how this household runs the basement PC, backups, or AI, ask the person who operates the install.
