"""Populate a database at an old migration state through its historical models.

build(apps) takes the apps of a MigrationExecutor project state, so every row
is written with the schema of that state, never with today's models. It only
uses fields that already existed at 0040_saved_transaction_filter, so the same
builder serves every later starting point. All names, amounts and identifiers
are synthetic; SimpleFIN and AI rows carry dummy, non-secret bytes.

The data is deterministic for a seed. MIGRATION_FIXTURE_SIZE picks how many
months of history to write: "small" (CI, a few thousand transactions) or
"large" (ten years, for a local soak).
"""
import hashlib
import os
import random
from datetime import UTC, date, datetime, timedelta

SIZES = {"small": 18, "large": 120}
START_MONTH = date(2023, 1, 1)
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

MODELS = (
    "Person", "Household", "Membership", "Category", "Tag", "SavedCsvMapping", "Account", "ImportBatch",
    "Transaction", "TransactionTag", "TransactionSplit", "TransferPair", "RefundLink",
    "TransactionCorrectionHistory", "RecurringSeries", "RecurringSeriesMember", "RecurringExclusion",
    "SimpleFinConnection", "AccountLink", "BalanceSnapshot", "PlannedItem", "SavingsGoal", "CategoryRule",
    "RuleApplication", "RuleApplicationEntry", "Budget", "BudgetAmount", "BudgetRolloverReset", "Alert",
    "AlertSettings", "PrivacyPolicyVersion", "PrivacyPolicyAcceptance", "Invitation", "RecoveryCode",
    "MonthlyReview", "SavedTransactionFilter", "SheetComparisonSettings", "SheetMonthTotal",
    "MemberSecurityEvent", "CategorySuggestion", "AiJob", "AiConversation", "AiConversationMessage",
)


def months_for(size=None):
    size = size or os.environ.get("MIGRATION_FIXTURE_SIZE", "small")
    if size not in SIZES:
        raise ValueError(f"MIGRATION_FIXTURE_SIZE must be one of {sorted(SIZES)}, not {size!r}")
    return SIZES[size]


def _digest(*parts):
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def _add_months(month, count):
    index = month.month - 1 + count
    return date(month.year + index // 12, index % 12 + 1, 1)


class _Builder:
    def __init__(self, apps, months, seed):
        self.m = {name: apps.get_model("finance", name) for name in MODELS}
        self.user_model = apps.get_model("auth", "User")
        self.rng = random.Random(seed)
        self.months = [_add_months(START_MONTH, offset) for offset in range(months)]
        self.rows = {}  # account pk -> pending Transaction rows
        self.batches = {}  # account pk -> {month: ImportBatch}
        self.pairs = []
        self.refunds = []
        self.splits = []
        self.series = {}

    def create(self, name, /, **fields):
        return self.m[name].objects.create(**fields)

    def bulk(self, name, rows):
        return self.m[name].objects.bulk_create(rows, batch_size=1000)

    # People, households and shares.
    def people(self):
        self.policy = [
            self.create("PrivacyPolicyVersion", version=v, body=f"Synthetic policy text v{v}", is_material=True)
            for v in (1, 2)
        ]
        names = ("Alex", "Blair", "Casey", "Drew")
        self.person = {}
        for name in names:
            user = self.user_model.objects.create(username=f"synthetic-{name.lower()}", email=f"{name.lower()}@example.invalid")
            self.person[name] = self.create("Person", user=user, display_name=f"Synthetic {name}")
            self.create("RecoveryCode", user=user, code_digest=_digest("recovery", name))
        self.create("PrivacyPolicyAcceptance", person=self.person["Alex"], policy_version=self.policy[0])
        # 0065 moves a "Not now" decline into the dismissal column.
        casey = self.person["Casey"]
        casey.privacy_policy_declined_version = self.policy[1]
        casey.save(update_fields=["privacy_policy_declined_version"])

        self.home = self.create("Household", name="Synthetic Home", transfer_match_window_days=7)
        self.flat = self.create("Household", name="Synthetic Flat")
        joined = NOW - timedelta(days=900)
        for name in ("Alex", "Blair"):
            self.create("Membership", person=self.person[name], household=self.home, joined_at=joined)
        self.create("Membership", person=self.person["Casey"], household=self.flat, joined_at=joined)
        # Drew left the home and now lives in the flat.
        self.create("Membership", person=self.person["Drew"], household=self.home, joined_at=joined,
                    ended_at=joined + timedelta(days=200))
        self.create("Membership", person=self.person["Drew"], household=self.flat, joined_at=joined + timedelta(days=201))
        self.create("Invitation", household=self.home, invited_by=self.person["Alex"], token_digest=_digest("invite"),
                    expires_at=NOW + timedelta(days=7))

    def categories(self):
        self.cat = {}
        for household in (self.home, self.flat):
            per = {
                "uncategorized": self.create("Category", household=household, name="Uncategorized", code="uncategorized"),
                "transfer": self.create("Category", household=household, name="Transfer", code="transfer"),
            }
            for name in ("Groceries", "Dining", "Utilities", "Rent", "Salary", "Shopping", "Travel", "Streaming",
                         "Interest", "Investing"):
                per[name] = self.create("Category", household=household, name=f"Synthetic {name}")
            self.cat[household.pk] = per
        self.tags = [self.create("Tag", household=self.home, name=f"synthetic-tag-{n}") for n in range(4)]
        self.create("Tag", household=self.home, name="synthetic-retired", is_archived=True)
        self.mapping = self.create(
            "SavedCsvMapping", household=self.home, created_by=self.person["Alex"], name="Synthetic bank layout",
            headers=["Date", "Description", "Amount"], date_column="Date", description_column="Description",
            date_format="mdy_slash_4", number_format="dot_comma", amount_mode="signed", amount_column="Amount",
        )

    def accounts(self):
        alex, blair, casey = self.person["Alex"], self.person["Blair"], self.person["Casey"]

        def account(owner, name, kind, household=None, share_mode="", **extra):
            return self.create(
                "Account", owner=owner, name=f"Synthetic {name}", account_type=kind,
                scope="household" if household else "private", household=household, share_mode=share_mode, **extra,
            )

        home = self.home
        self.acct = {
            "alex_checking": account(alex, "Alex checking", "checking"),
            "alex_savings": account(alex, "Alex savings", "savings"),
            "alex_card": account(alex, "Alex card", "credit_card", apr_percent="24.99", minimum_payment_minor=3500),
            "alex_brokerage": account(alex, "Alex brokerage", "investment"),
            "home_checking": account(alex, "Home checking", "checking", home, "co_owned",
                                     default_saved_csv_mapping=self.mapping),
            "home_card": account(blair, "Home card", "credit_card", home, "lent"),
            "house": account(alex, "House", "real_estate", home, "co_owned"),
            "car": account(blair, "Car", "vehicle"),
            "gold": account(blair, "Coins", "precious_metals"),
            "casey_checking": account(casey, "Casey checking", "checking"),
            "casey_other": account(casey, "Casey collectibles", "other_asset"),
        }
        self.acct["mortgage"] = account(alex, "Mortgage", "loan", home, "co_owned", secured_asset=self.acct["house"],
                                        apr_percent="6.125", minimum_payment_minor=180000, payment_day=1)
        self.acct["car_loan"] = account(blair, "Car loan", "loan", secured_asset=self.acct["car"], payment_day=15,
                                        minimum_payment_minor=41000)
        self.acct["casey_old_savings"] = account(casey, "Casey old savings", "savings",
                                                 status="archived", archived_at=NOW - timedelta(days=30))
        self.source = {
            "alex_checking": "huntington", "alex_savings": "huntington", "alex_card": "capital_one",
            "alex_brokerage": "vanguard", "home_checking": "simplefin", "home_card": "apple_card",
            "mortgage": "huntington", "car_loan": "huntington", "casey_checking": "huntington",
            "casey_old_savings": "huntington",
        }
        self.connection = self.create("SimpleFinConnection", owner=alex, encrypted_access_url=b"synthetic-not-a-secret",
                                      last_sync_at=NOW, last_sync_result="ok")
        self.create("AccountLink", account=self.acct["home_checking"], connection=self.connection,
                    simplefin_account_id="SYNTH-ACCT-1", cutover_date=START_MONTH, mode="transactions")

    def household_of(self, key):
        account = self.acct[key]
        if account.household_id:
            return account.household_id
        return self.flat.pk if account.owner_id == self.person["Casey"].pk else self.home.pk

    def batch(self, key, month):
        account = self.acct[key]
        per = self.batches.setdefault(account.pk, {})
        if month not in per:
            archived = key == "casey_old_savings"
            source = self.source[key]
            per[month] = self.create(
                "ImportBatch", account=account, source=source, imported_by=account.owner,
                source_file_sha256=_digest("file", key, month), date_range_start=month,
                date_range_end=_add_months(month, 1) - timedelta(days=1),
                simplefin_account_id="SYNTH-ACCT-1" if source == "simplefin" else "",
                saved_csv_mapping=self.mapping if key == "home_checking" and month == self.months[0] else None,
                status="archived" if archived else "active", archived_at=NOW if archived else None,
            )
        return per[month]

    def txn(self, key, month, day, amount, description, category=None, source="manual", kind="cash_flow"):
        account = self.acct[key]
        rows = self.rows.setdefault(account.pk, [])
        batch = self.batch(key, month)
        archived = batch.status == "archived"
        row = self.m["Transaction"](
            account=account, import_batch=batch, transaction_date=month.replace(day=day), amount_minor=amount,
            currency="USD", description=description, kind=kind, source_row_number=len(rows) + 1,
            source_transaction_id=f"SYNTH-{account.pk}-{len(rows) + 1}" if batch.source == "simplefin" else "",
            fingerprint=_digest("txn", account.pk, len(rows) + 1), original_fields={"synthetic": True},
            category=self.cat[self.household_of(key)][category] if category else None,
            category_source=source if category else "",
            status="archived" if archived else "active", archived_at=NOW if archived else None,
        )
        rows.append(row)
        return row

    def pair(self, left, right, status, kind="transfer", confidence="high", reasons=("Synthetic reason",)):
        self.pairs.append((left, right, status, kind, confidence, list(reasons)))

    def month_of_activity(self, index, month):
        rng = self.rng
        salary = 412_500 + 1_250 * (index // 12)
        self.txn("alex_checking", month, 1, salary, "Synthetic Employer PAYROLL", "Salary")
        self.txn("casey_checking", month, 2, 298_000, "Synthetic Studio PAYROLL", "Salary")
        self.txn("home_checking", month, 3, 155_000, "Synthetic Blair PAYROLL", "Salary")
        self.txn("casey_checking", month, 1, -145_000, "Synthetic Landlord rent", "Rent")
        self.txn("alex_savings", month, 28, rng.randint(100, 900), "Synthetic interest paid", "Interest")
        for day in rng.sample(range(2, 28), 6):
            self.txn("home_checking", month, day, -rng.randint(4_000, 21_000), "Synthetic Utility Co", "Utilities")
        for _ in range(4):
            self.txn("casey_checking", month, rng.randint(1, 28), -rng.randint(900, 12_000), "Synthetic Corner Shop",
                     "Groceries")
        if index % 6 == 0:
            self.txn("casey_old_savings", month, 10, 5_000, "Synthetic old deposit")
        for key, count in (("alex_card", 60), ("home_card", 55)):
            for _ in range(count):
                name = rng.choice(("Groceries", "Dining", "Shopping", "Travel"))
                purchase = self.txn(key, month, rng.randint(1, 28), -rng.randint(350, 25_000),
                                    f"Synthetic {name} merchant {rng.randint(1, 40)}", name,
                                    rng.choice(("rule", "manual")))
                if rng.random() < 0.04:
                    refund = self.txn(key, month, 28, -purchase.amount_minor // 2, "Synthetic merchant refund",
                                      name, "inherited")
                    self.refunds.append((refund, purchase))
                elif name == "Groceries" and rng.random() < 0.15:
                    self.splits.append(purchase)
        streaming = self.txn("alex_card", month, 12, -1_599, "Synthetic Streaming Svc", "Streaming", "rule")
        self.series.setdefault("streaming", []).append(streaming)
        gym = self.txn("home_card", month, 5, -4_500 - (500 if index >= len(self.months) - 2 else 0),
                       "Synthetic Gym", "Shopping")
        self.series.setdefault("gym", []).append(gym)
        for _ in range(3):
            self.txn("alex_brokerage", month, rng.randint(1, 28), rng.choice((-1, 1)) * rng.randint(10_000, 90_000),
                     "Synthetic fund activity", "Investing", kind="investment_activity")

        # Card payments with payment wording and the right direction: evidence holds.
        for card, payer in (("alex_card", "alex_checking"), ("home_card", "home_checking")):
            amount = rng.randint(80_000, 240_000)
            left = self.txn(payer, month, 20, -amount, "Synthetic CARD PAYMENT", "transfer")
            right = self.txn(card, month, 21, amount, "Synthetic PAYMENT THANK YOU", "transfer")
            self.pair(left, right, "confirmed" if index % 3 == 0 else "auto_marked", "card_payment")
        # Savings moves with no wording: auto-marked ones are what 0060 downgrades (#391).
        amount = rng.randint(10_000, 60_000)
        left = self.txn("alex_checking", month, 15, -amount, "Synthetic plain move", "transfer")
        right = self.txn("alex_savings", month, 16, amount, "Synthetic plain move", "transfer")
        status = ("auto_marked", "confirmed", "suggested", "dismissed", "undone")[index % 5]
        self.pair(left, right, status, confidence="high" if status == "auto_marked" else "low")
        for loan, payer, amount in (("mortgage", "home_checking", 180_000), ("car_loan", "alex_checking", 41_000)):
            left = self.txn(payer, month, 1, -amount, "Synthetic LOAN PMT", "transfer")
            right = self.txn(loan, month, 1, amount, "Synthetic LOAN PMT received", "transfer")
            self.pair(left, right, "confirmed")

    def ledger(self):
        for index, month in enumerate(self.months):
            self.month_of_activity(index, month)
        transactions = []
        for rows in self.rows.values():
            transactions += self.bulk("Transaction", rows)
        groceries = {pk: per["Groceries"].pk for pk, per in self.cat.items()}
        pairs = []
        for left, right, status, kind, confidence, reasons in self.pairs:
            left, right = sorted((left, right), key=lambda leg: leg.pk)
            pairs.append(self.m["TransferPair"](
                leg_a=left, leg_b=right, status=status, kind=kind, confidence=confidence, reasons=reasons,
                leg_a_category_id_at_mark=groceries[self.home.pk], leg_b_category_id_at_mark=None,
            ))
        self.bulk("TransferPair", pairs)
        self.bulk("RefundLink", [self.m["RefundLink"](refund=refund, original=original)
                                 for refund, original in self.refunds])
        splits = []
        for purchase in self.splits:
            per = self.cat[self.household_of_account(purchase.account_id)]
            first = purchase.amount_minor // 3
            splits += [
                self.m["TransactionSplit"](transaction=purchase, position=1, amount_minor=first,
                                           category=per["Groceries"]),
                self.m["TransactionSplit"](transaction=purchase, position=2, amount_minor=purchase.amount_minor - first,
                                           category=per["Shopping"]),
            ]
        self.bulk("TransactionSplit", splits)
        self.m["Transaction"].objects.filter(pk__in=[p.pk for p in self.splits]).update(category=None,
                                                                                         category_source="split")
        tagged = [row for row in transactions if row.account.household_id == self.home.pk][::7]
        self.bulk("TransactionTag", [self.m["TransactionTag"](transaction=row, tag=self.tags[n % len(self.tags)])
                                     for n, row in enumerate(tagged)])
        noted = transactions[::50]
        for row in noted:
            row.note = "Synthetic note"
        self.m["Transaction"].objects.bulk_update(noted, ["note"], batch_size=1000)
        self.transactions = transactions

    def household_of_account(self, account_pk):
        key = next(key for key, account in self.acct.items() if account.pk == account_pk)
        return self.household_of(key)

    def history(self):
        actor = self.person["Alex"]
        rows = []
        for row in self.transactions[::40]:
            rows.append(self.m["TransactionCorrectionHistory"](
                transaction=row, actor=actor, field_name="description",
                previous_description="Synthetic before", new_description=row.description,
            ))
            rows.append(self.m["TransactionCorrectionHistory"](
                transaction=row, actor=actor, field_name="amount_minor", currency="USD",
                previous_amount_minor=row.amount_minor + 100, new_amount_minor=row.amount_minor,
            ))
        self.bulk("TransactionCorrectionHistory", rows)

    def recurring(self):
        alex, blair = self.person["Alex"], self.person["Blair"]
        for name, person, status, members, typical in (
            ("streaming", alex, "confirmed", self.series["streaming"], -1_599),
            ("gym", blair, "suggested", self.series["gym"], -4_500),
        ):
            series = self.create(
                "RecurringSeries", person=person, merchant_key=f"synthetic {name}", display_name=f"Synthetic {name}",
                cadence="monthly", typical_amount_minor=typical, currency="USD", status=status, confidence="high",
                reasons=["Synthetic monthly charge"], fingerprint=_digest("series", name),
                confirmed_at=NOW if status == "confirmed" else None,
                # The gym price rose; Blair dismissed the alert at the new amount (0071 moves this).
                acknowledged_amount_minor=-5_000 if name == "gym" else None,
            )
            self.bulk("RecurringSeriesMember", [self.m["RecurringSeriesMember"](series=series, transaction=row)
                                                for row in members])
        self.create("RecurringSeries", person=alex, merchant_key="synthetic old box", display_name="Synthetic box",
                    cadence="monthly", typical_amount_minor=-2_999, currency="USD", status="dismissed",
                    confidence="low", reasons=["Synthetic"], fingerprint=_digest("series", "box"), is_active=False,
                    cancelled_at=NOW)
        self.create("RecurringExclusion", person=alex, transaction=self.series["streaming"][0])

    def balances(self):
        rows = []
        for key, start in (("alex_checking", 600_000), ("alex_savings", 2_500_000), ("house", 41_000_000),
                           ("car", 2_200_000), ("gold", 900_000), ("casey_other", 150_000),
                           ("mortgage", -31_000_000), ("car_loan", -1_800_000), ("alex_brokerage", 9_000_000),
                           ("home_checking", 300_000)):
            linked = key == "home_checking"
            for index, month in enumerate(self.months):
                rows.append(self.m["BalanceSnapshot"](
                    account=self.acct[key], snapshot_date=month, amount_minor=start + index * self.rng.randint(-5_000, 9_000),
                    currency="USD", source="simplefin" if linked else "manual",
                    import_batch=self.batch(key, month) if linked else None,
                    note="" if linked else "Synthetic statement balance",
                    net_contribution_minor=None if linked or index % 4 else 25_000,
                ))
        self.bulk("BalanceSnapshot", rows)

    def plans(self):
        alex, blair, casey = self.person["Alex"], self.person["Blair"], self.person["Casey"]
        home_cat = self.cat[self.home.pk]
        budgets = [
            self.create("Budget", owner=alex, scope="private", category=home_cat["Dining"]),
            self.create("Budget", owner=alex, scope="private"),
            # A legacy rollover start before 2000, which 0062 clamps.
            self.create("Budget", owner=blair, scope="household", household=self.home, category=home_cat["Groceries"],
                        rollover_enabled=True, rollover_started_month=date(1999, 6, 1), rollover_enabled_at=NOW),
            self.create("Budget", owner=casey, scope="private", category=self.cat[self.flat.pk]["Groceries"],
                        status="archived", archived_at=NOW),
        ]
        amounts = []
        for n, budget in enumerate(budgets):
            for month in self.months[::3]:
                amounts.append(self.m["BudgetAmount"](budget=budget, effective_month=month, currency="USD",
                                                      amount_minor=50_000 + 10_000 * n))
        self.bulk("BudgetAmount", amounts)
        self.create("BudgetRolloverReset", budget=budgets[2], month=self.months[-1], actor=blair)
        first = self.months[0]
        for owner, household, name, kind, amount, cadence in (
            (alex, None, "Synthetic bonus", "income", 500_000, "annual"),
            (alex, self.home, "Synthetic insurance", "expense", 120_000, "quarterly"),
            (casey, None, "Synthetic course", "expense", 30_000, "one_time"),
        ):
            self.create("PlannedItem", owner=owner, scope="household" if household else "private", household=household,
                        name=name, kind=kind, amount_minor=amount, currency="USD", start_date=first, cadence=cadence,
                        category=home_cat["Shopping"] if household else None)
        self.create("SavingsGoal", owner=alex, scope="private", name="Synthetic emergency fund",
                    target_amount_minor=3_000_000, currency="USD", target_date=date(2027, 6, 1),
                    linked_account=self.acct["alex_savings"])
        self.create("SavingsGoal", owner=blair, scope="household", household=self.home, name="Synthetic trip",
                    target_amount_minor=800_000, currency="USD", target_date=date(2026, 9, 1),
                    manual_amount_minor=120_000, manual_amount_date=date(2025, 12, 1))
        self.create("SavingsGoal", owner=casey, scope="private", name="Synthetic laptop", target_amount_minor=200_000,
                    currency="USD", target_date=date(2025, 3, 1), completed_at=NOW,
                    status="archived", archived_at=NOW)

    def rules(self):
        alex = self.person["Alex"]
        home_cat = self.cat[self.home.pk]
        household_rule = self.create("CategoryRule", owner_household=self.home, description_contains="Streaming",
                                     category=home_cat["Streaming"], priority=10, confirmed_at=NOW)
        self.create("CategoryRule", owner_person=alex, description_contains="Utility", category=home_cat["Utilities"],
                    account=self.acct["home_checking"], min_amount_minor=-50_000, max_amount_minor=0)
        self.create("CategoryRule", owner_person=self.person["Casey"], description_contains="Shop",
                    category=self.cat[self.flat.pk]["Groceries"], enabled=False)
        application = self.create("RuleApplication", rule=household_rule, applied_by=alex)
        self.bulk("RuleApplicationEntry", [
            self.m["RuleApplicationEntry"](application=application, transaction=row, previous_category=None)
            for row in self.series["streaming"]
        ])
        self.create("RuleApplication", rule=household_rule, applied_by=alex, reversed_at=NOW)

    def alerts(self):
        rows = []
        for name, person in self.person.items():
            self.create("AlertSettings", person=person, large_transaction_minor=100_000 if name == "Alex" else None)
            for kind in ("sync", "recurring_price", "recurring_missed", "budget", "large_transaction",
                         "monthly_review", "backup"):
                for n in range(3):
                    rows.append(self.m["Alert"](
                        recipient=person, kind=kind, title=f"Synthetic {kind} alert {n}", link="/synthetic/",
                        dedupe_key=f"{kind}:{n}", read_at=NOW if n == 0 else None,
                        account=self.acct["home_checking"] if kind == "sync" else None,
                    ))
        self.bulk("Alert", rows)

    def member_records(self):
        alex = self.person["Alex"]
        for name, person in self.person.items():
            self.create("MemberSecurityEvent", member=person, event_type="sign_in_success", ip_address="192.0.2.10",
                        user_agent="Synthetic browser")
            self.create("SavedTransactionFilter", member=person, name=f"Synthetic {name} filter",
                        query={"q": "Synthetic"})
            self.create("MonthlyReview", person=person, month=self.months[-1], visibility_key=_digest("review", name),
                        facts={"synthetic": True}, generated_at=NOW)
        self.create("SheetComparisonSettings", member=alex, month_column="Month", income_column="Income",
                    spending_column="Spending")
        self.bulk("SheetMonthTotal", [
            self.m["SheetMonthTotal"](member=alex, month=month, income_minor=400_000, spending_minor=310_000,
                                      source="synthetic sheet")
            for month in self.months
        ])
        row = self.series["streaming"][-1]
        self.create("CategorySuggestion", member=alex, transaction=row, category=self.cat[self.home.pk]["Streaming"],
                    provider="synthetic", backend="synthetic", snapshot_hash=_digest("suggestion"))
        self.create("AiJob", member=alex, feature="synthetic_feature", status="succeeded", finished_at=NOW)
        for name, person in self.person.items():
            conversation = self.create("AiConversation", member=person, title=f"Synthetic {name} chat",
                                       used_account_ids=[self.acct["home_checking"].pk], turn_count=1,
                                       tool_call_count=1, expires_at=NOW + timedelta(days=30))
            self.create("AiConversationMessage", conversation=conversation, role="user", content="Synthetic question")
        # Drew's chat began while Drew still lived in the home, so 0076 deletes it.
        self.m["AiConversation"].objects.filter(member=self.person["Drew"]).update(created_at=NOW - timedelta(days=800))

    def build(self):
        self.people()
        self.categories()
        self.accounts()
        self.ledger()
        self.history()
        self.recurring()
        self.balances()
        self.plans()
        self.rules()
        self.alerts()
        self.member_records()


def build(apps, *, months=None, seed=532):
    """Populate the database through ``apps`` (a historical state) and return a short summary."""
    builder = _Builder(apps, months or months_for(), seed)
    builder.build()
    return {
        "months": len(builder.months),
        "transactions": len(builder.transactions),
        "transfer_pairs": len(builder.pairs),
        "weak_auto_marked": sum(
            1 for left, _, status, *_ in builder.pairs if status == "auto_marked" and left.description == "Synthetic plain move"
        ),
    }
