from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .budget_services import add_months
from .models import Person
from .monthly_review import latest_closed_month, parse_review_month, review_for_viewer
from .monthly_review_ai import visible_phrasing
from .unusual_spending_ai import visible_unusual_phrasing


def _person(request):
    return get_object_or_404(Person, user=request.user)


def _list_url(month):
    return f"{reverse('monthly-review')}?month={month.isoformat()[:7]}"


@require_GET
@never_cache
def monthly_review(request):
    person = _person(request)
    today = timezone.localdate()
    month = parse_review_month(request.GET.get("month"), today=today)
    closed = latest_closed_month(today)
    review = review_for_viewer(person, month, today=today)
    previous_month = add_months(month, -1)
    next_month = add_months(month, 1)
    ai_paragraph, ai_label = visible_phrasing(person, review)
    unusual_paragraph, unusual_label = visible_unusual_phrasing(person, review)
    return render(
        request,
        "finance/monthly_review.html",
        {
            "review": review,
            "facts": review.facts,
            "ai_paragraph": ai_paragraph,
            "ai_label": ai_label,
            "unusual_paragraph": unusual_paragraph,
            "unusual_label": unusual_label,
            "month": month,
            "month_label": review.facts.get("month_label"),
            "previous_url": _list_url(previous_month),
            "next_url": _list_url(next_month) if next_month <= closed else None,
            "regenerate_url": reverse("monthly-review-regenerate"),
        },
    )


@require_POST
@never_cache
def monthly_review_regenerate(request):
    person = _person(request)
    today = timezone.localdate()
    month = parse_review_month(request.POST.get("month"), today=today)
    review_for_viewer(person, month, today=today, force=True)
    return redirect(_list_url(month))
