"""Import-due reminders for accounts kept current by hand (issue #428).

An account is due when it is active, takes CSV imports, has no SimpleFIN link
in transactions mode, and its newest active non-manual batch covers through a
date more than IMPORT_DUE_DAYS before today. Accounts never imported are not due.
A balances-only link still syncs SimpleFIN batches through today without any
transactions, and a batch does not record its link's mode, so a SimpleFIN batch
counts as coverage only when it holds a transaction. That holds after the link is
removed too.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef, Subquery
from django.urls import reverse
from django.utils import dateformat, timezone

from .models import Account, AccountLink, Alert, ImportBatch, Transaction


def import_due_cutoff(today):
    """Coverage ending before this date is due; the boundary itself is not."""
    return today - timedelta(days=settings.IMPORT_DUE_DAYS)


def due_import_accounts(accounts, *, today=None):
    """Filter an account queryset to due accounts, annotated with ``last_covered``, in one query."""
    today = today or timezone.localdate()
    transaction_links = AccountLink.objects.filter(account_id=OuterRef("pk"), mode=AccountLink.Mode.TRANSACTIONS)
    newest_coverage = (
        ImportBatch.objects.filter(account_id=OuterRef("pk"), status=ImportBatch.Status.ACTIVE)
        .exclude(source=ImportBatch.Source.MANUAL)
        .alias(_has_transactions=Exists(Transaction.objects.filter(import_batch_id=OuterRef("pk"))))
        .exclude(source=ImportBatch.Source.SIMPLEFIN, _has_transactions=False)
        .order_by("-date_range_end")
        .values("date_range_end")[:1]
    )
    return (
        accounts.filter(status=Account.Status.ACTIVE, archived_at__isnull=True)
        .exclude(account_type__in=Account.NON_CASH_FLOW_TYPES)
        .alias(_transactions_linked=Exists(transaction_links))
        .filter(_transactions_linked=False)
        .annotate(last_covered=Subquery(newest_coverage))
        .filter(last_covered__lt=import_due_cutoff(today))
        .order_by("name", "pk")
    )


def last_covered_display(day):
    return dateformat.format(day, "M j, Y")


def import_due_link(account):
    return reverse("csv-import-preview", args=[account.pk])


def evaluate_import_due_alerts(*, today=None):
    """Raise one alert per due account and coverage end, to that account's audience.

    Dedupe rests on the alert row, which retention purges, so a coverage end is
    alerted only while its first possible alert would still be kept. Home's
    Needs attention row stays for as long as the account is due.
    """
    from .alert_services import READ_RETENTION_DAYS, UNREAD_RETENTION_DAYS, audience_for_account, raise_alert

    today = today or timezone.localdate()
    oldest_alerted = import_due_cutoff(today) - timedelta(days=min(READ_RETENTION_DAYS, UNREAD_RETENTION_DAYS))
    created = []
    audiences = {}
    due = due_import_accounts(Account.objects.select_related("owner", "household"), today=today)
    for account in due.filter(last_covered__gte=oldest_alerted):
        key = (account.scope, account.household_id if account.scope == Account.Scope.HOUSEHOLD else account.owner_id)
        if key not in audiences:
            audiences[key] = audience_for_account(account)
        created.extend(
            raise_alert(
                audiences[key],
                Alert.Kind.IMPORT_DUE,
                f"Import {account.name}: last covered {last_covered_display(account.last_covered)}",
                import_due_link(account),
                f"import-due:{account.pk}:{account.last_covered.isoformat()}",
                account=account,
            )
        )
    return created
