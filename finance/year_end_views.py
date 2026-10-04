from urllib.parse import urlencode

from django.http import Http404, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .forms import YearEndFilterForm
from .year_end import (
    CSV_SECTIONS,
    csv_filename,
    iter_csv_bytes,
    last_full_year,
    parse_report_year,
    year_end_report,
)


def _filter_state(request, today):
    default_year = last_full_year(today)
    form = YearEndFilterForm(request.GET or None)
    if not form.is_bound:
        form = YearEndFilterForm(initial={"year": default_year, "scope": ""})
        return form, default_year, ""
    if form.is_valid():
        year = form.cleaned_data["year"] or default_year
        scope = form.cleaned_data.get("scope") or ""
        return form, year, scope
    return form, None, None


@require_GET
@never_cache
def year_end(request):
    today = timezone.localdate()
    form, year, scope = _filter_state(request, today)
    report = None
    csv_query = ""
    if year is not None:
        report = year_end_report(
            request.user,
            year=year,
            scope=scope,
            today=today,
        )
        query = {"year": str(year)}
        if scope:
            query["scope"] = scope
        csv_query = urlencode(query)
    return render(
        request,
        "finance/year_end.html",
        {
            "filter_form": form,
            "report": report,
            "csv_sections": CSV_SECTIONS,
            "csv_query": csv_query,
        },
    )


@require_GET
@never_cache
def year_end_csv(request, section):
    if section not in CSV_SECTIONS:
        raise Http404("Unknown year-end CSV section.")
    today = timezone.localdate()
    year = parse_report_year(request.GET.get("year"), today=today)
    scope = request.GET.get("scope") or ""
    if scope not in ("", "private", "household"):
        scope = ""
    report = year_end_report(request.user, year=year, scope=scope, today=today)
    response = StreamingHttpResponse(
        iter_csv_bytes(report, section),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{csv_filename(year, section)}"'
    return response
