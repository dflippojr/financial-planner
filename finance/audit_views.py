from datetime import date
from urllib.parse import urlencode

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .audit_services import events_for
from .models import AuditEvent


class AuditFilterForm(forms.Form):
    action = forms.ChoiceField(required=False, choices=[("", "All actions"), *AuditEvent.Action.choices])
    actor = forms.IntegerField(required=False, min_value=1, max_value=9223372036854775807, label="Actor member ID")
    source = forms.ChoiceField(required=False, choices=[("", "All sources"), *AuditEvent.Source.choices])
    date_from = forms.DateField(required=False, label="From (UTC)", widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, label="Through (UTC)", widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        data = super().clean()
        if data.get("date_to") == date.max:
            raise forms.ValidationError("The end date is out of range.")
        return data


@login_required
@require_GET
@never_cache
def audit_page(request):
    form = AuditFilterForm(request.GET)
    valid = form.is_valid()
    filters = form.cleaned_data if valid else {}
    rows = events_for(request.user, **filters) if valid else AuditEvent.objects.none()
    if not valid:
        # Do not reflect arbitrary submitted text in this privacy-safe reader.
        form = AuditFilterForm({})
        form.is_valid()
        form.add_error(None, "Invalid audit filters.")
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    query = urlencode({key: value for key, value in filters.items() if value})
    return render(request, "finance/audit.html", {"form": form, "page": page, "query": query})
