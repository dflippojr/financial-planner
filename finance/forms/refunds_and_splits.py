"""Refund links, refund search, and split transactions."""

from decimal import Decimal

from django import forms

from django.core.exceptions import ValidationError

from ..money_input import MoneyField

from ..models import (
    Category,
    RefundLink,
    Transaction,
    TransactionSplit,
)
from ..ledger_counting import exclusion_exists_for
from ..refund_links import REFUND_LINK_RULE, SPLIT_PART_MISMATCH
from ..refund_links import SPLIT_PART_REQUIRED
from ..category_services import assignable_categories
from ..access import visible_rows


class RefundSearchForm(forms.Form):
    refund_search = forms.CharField(required=False, max_length=255, label="Purchase description")
    refund_amount = MoneyField(
        required=False, decimal_places=2, max_digits=19, min_value=Decimal("0.01"), label="Purchase amount",
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )
    refund_same_account = forms.BooleanField(required=False, label="Only this account")

    def candidates(self, principal, refund):
        if not self.is_valid():
            return Transaction.objects.none()
        candidates = refund_candidates(principal, refund)
        text = self.cleaned_data["refund_search"]
        if text:
            candidates = candidates.filter(description__icontains=text)
        amount = self.cleaned_data["refund_amount"]
        if amount is not None:
            candidates = candidates.filter(amount_minor=-int(amount * 100))
        if self.cleaned_data["refund_same_account"]:
            candidates = candidates.filter(account_id=refund.account_id)
        return candidates.select_related("account").order_by("-transaction_date", "-pk")[:25]



def refund_candidates(principal, refund):

    # A leg of an excluded transfer or card payment was never counted as spending.
    return (
        visible_rows(Transaction, principal)
        .filter(status=Transaction.Status.ACTIVE, amount_minor__lt=0, kind=refund.kind)
        .exclude(exclusion_exists_for(principal))
    )



class RefundLinkForm(forms.Form):
    original = forms.ModelChoiceField(
        queryset=Transaction.objects.none(), required=True, label="Original purchase", widget=forms.HiddenInput
    )
    original_part = forms.ModelChoiceField(
        queryset=TransactionSplit.objects.none(),
        required=False,
        label="Split part",
    )

    def __init__(self, *args, principal=None, refund=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._refund = refund

        visible = refund_candidates(principal, refund) if refund is not None else Transaction.objects.none()
        selected_id = self.data.get("original") if self.is_bound else self.initial.get("original")
        try:
            selected_id = int(selected_id)  # pyright: ignore[reportArgumentType] - a missing or non-numeric original raises TypeError or ValueError, both caught below
        except (TypeError, ValueError):
            selected_id = None
        self.fields["original"].queryset = visible.filter(pk=selected_id)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
        self.fields["original"].error_messages["invalid_choice"] = REFUND_LINK_RULE
        self.fields["original_part"].queryset = (  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
            TransactionSplit.objects.filter(transaction__in=self.fields["original"].queryset)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField's queryset; pyright types form fields as the base Field
            .select_related("category", "transaction")
            .order_by("position")
        )
        self.fields["original_part"].error_messages["invalid_choice"] = SPLIT_PART_MISMATCH

    def clean(self):
        cleaned = super().clean()

        original = cleaned.get("original")
        refund = self._refund
        if refund is None or original is None:
            return cleaned
        if refund.amount_minor <= 0 or original.amount_minor >= 0 or refund.kind != original.kind:
            raise ValidationError(REFUND_LINK_RULE)
        part = cleaned.get("original_part")
        if original.category_source == Transaction.CategorySource.SPLIT:
            if part is None:
                raise ValidationError(SPLIT_PART_REQUIRED)
            if part.transaction_id != original.pk:
                raise ValidationError(SPLIT_PART_MISMATCH)
        elif part is not None:
            raise ValidationError(SPLIT_PART_MISMATCH)
        return cleaned



class SplitTransactionForm(forms.Form):
    part_count = forms.IntegerField(min_value=2, max_value=20, widget=forms.HiddenInput)

    def __init__(self, *args, principal=None, transaction=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._transaction = transaction
        self._refunds = []
        categories = Category.objects.none()
        if principal is not None:

            categories = assignable_categories(principal)
        existing = []
        if transaction is not None:
            existing = list(transaction.splits.select_related("category").order_by("position"))
            if principal is not None:
                self._refunds = list(
                    visible_rows(Transaction, principal)
                    .filter(
                        refund_link__original=transaction,
                        status=Transaction.Status.ACTIVE,
                        pk__in=visible_rows(RefundLink, principal).values("refund_id"),
                    )
                    .select_related("refund_link")
                    .order_by("pk")
                )
        count = max(2, len(existing))
        if self.data:
            try:
                count = max(2, int(self.data.get("part_count", count)))
            except (TypeError, ValueError):
                count = 2
        self.part_indexes = list(range(count))
        self.fields["part_count"].initial = count
        for index in self.part_indexes:
            initial_category = existing[index].category_id if index < len(existing) else None
            initial_amount = None
            if index < len(existing):
                initial_amount = Decimal(existing[index].amount_minor) / Decimal(100)
            self.fields[f"part_{index}_category"] = forms.ModelChoiceField(
                queryset=categories,
                label=f"Part {index + 1} category",
                initial=initial_category,
            )
            self.fields[f"part_{index}_amount"] = MoneyField(
                max_digits=19,
                decimal_places=2,
                label=f"Part {index + 1} amount",
                initial=initial_amount,
                widget=forms.TextInput(attrs={"inputmode": "decimal", "class": "split-part-amount"}),
            )
        self.refund_fields = []
        choices = [(str(index), f"Part {index + 1}") for index in self.part_indexes]
        for refund in self._refunds:
            name = f"refund_{refund.pk}_part"
            initial = ""
            link = getattr(refund, "refund_link", None)
            if link is not None and link.original_part_id:
                for index, part in enumerate(existing):
                    if part.pk == link.original_part_id:
                        initial = str(index)
                        break
            self.fields[name] = forms.ChoiceField(
                choices=choices,
                label=f"Refund on {refund.transaction_date} ({refund.amount_display})",
                initial=initial,
            )
            self.refund_fields.append(name)

    def parts_payload(self):
        parts = []
        for index in self.part_indexes:
            category = self.cleaned_data[f"part_{index}_category"]
            amount = self.cleaned_data[f"part_{index}_amount"]
            parts.append({"category_id": category.pk, "amount_minor": int(amount * 100)})
        return parts

    def refund_assignments(self):
        if not self.refund_fields:
            return None
        assigned = {}
        for name in self.refund_fields:
            refund_id = int(name.split("_")[1])
            assigned[refund_id] = int(self.cleaned_data[name])
        return assigned



class UnsplitTransactionForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, empty_label="Uncategorized")

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["category"].queryset = assignable_categories(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field



class SplitPartCategoryForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=True)

    def __init__(self, *args, principal=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["category"].queryset = assignable_categories(principal)  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField, typed as Field
