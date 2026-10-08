"""Pre-#265 scan/sort oracle; test-only semantic reference."""
from django.urls import reverse
from finance.recurring_services import merchant_key
from finance.models import Transaction
from finance.cash_flow import format_minor
from finance.unusual_spending import (KIND_MERCHANT, KIND_NEW_MERCHANT, _median_minor, _whole_minor, exceeds_merchant_median)

def _charges(principal, accounts, *, date_to, excluded):
    rows = Transaction.objects.visible_to(principal).filter(
        status=Transaction.Status.ACTIVE, kind=Transaction.Kind.CASH_FLOW,
        amount_minor__lt=0, transaction_date__lte=date_to, account__in=accounts,
    ).select_related("account").order_by("transaction_date", "pk")
    return [row for row in rows if row.pk not in excluded]

def reference_merchant_flags(principal, start, end, prefs, accounts, excluded):
    threshold = prefs.large_transaction_minor
    history = _charges(principal, accounts, date_to=end, excluded=excluded)
    by_key = {}
    for txn in history:
        key = merchant_key(txn.description)
        if not key:
            continue
        by_key.setdefault(key, []).append(txn)
    flags = []
    for key, group in by_key.items():
        in_month = [txn for txn in group if start <= txn.transaction_date <= end]
        for txn in in_month:
            earlier = [item for item in group if (item.transaction_date, item.pk) < (txn.transaction_date, txn.pk)]
            amount = abs(txn.amount_minor)
            if not earlier:
                if threshold is None or threshold <= 0 or amount < threshold:
                    continue
                flags.append(
                    {
                        "kind": KIND_NEW_MERCHANT,
                        "name": txn.description,
                        "merchant_key": key,
                        "amount_minor": amount,
                        "amount_display": format_minor(amount, txn.currency),
                        "date": txn.transaction_date.isoformat(),
                        "url": reverse("transaction-edit", args=[txn.pk]),
                        "transaction_id": txn.pk,
                        "account_id": txn.account_id,
                        "item_id": f"new_merchant:{key}",
                    }
                )
                continue
            baseline = _median_minor(abs(item.amount_minor) for item in earlier)
            if not exceeds_merchant_median(amount, baseline, prior_count=len(earlier)):
                continue
            flags.append(
                {
                    "kind": KIND_MERCHANT,
                    "name": txn.description,
                    "merchant_key": key,
                    "amount_minor": amount,
                    "median_minor": _whole_minor(baseline),
                    "median_display": format_minor(baseline, txn.currency),
                    "amount_display": format_minor(amount, txn.currency),
                    "date": txn.transaction_date.isoformat(),
                    "url": reverse("transaction-edit", args=[txn.pk]),
                    "transaction_id": txn.pk,
                    "account_id": txn.account_id,
                    "item_id": f"merchant:{txn.pk}",
                }
            )
    flags.sort(key=lambda item: (-item["amount_minor"], item["name"], item.get("transaction_id") or 0))
    return flags

