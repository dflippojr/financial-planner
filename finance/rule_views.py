from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .category_services import current_household, ensure_household_categories
from .forms import CategoryRuleForm
from .models import CategoryRule, Person
from .rule_services import (
    apply_rule,
    list_unreachable_applications,
    list_visible_applications,
    list_visible_rules,
    personal_rule_is_inactive,
    preview_rule,
    reverse_application,
    save_category_rule,
    set_rule_enabled,
)


def _service_or_404(action):
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc


def _first_message(exc, fallback):
    messages = getattr(exc, "messages", None)
    return messages[0] if messages else fallback


def _form_from_rule(rule, principal, data=None):
    initial = {
        "owner_kind": "household" if rule.owner_household_id else "personal",
        "description_contains": rule.description_contains,
        "account": rule.account_id,
        "min_amount": None if rule.min_amount_minor is None else Decimal(rule.min_amount_minor) / Decimal(100),
        "max_amount": None if rule.max_amount_minor is None else Decimal(rule.max_amount_minor) / Decimal(100),
        "category": rule.category_id,
        "priority": rule.priority,
        "enabled": rule.enabled,
    }
    return CategoryRuleForm(data, principal=principal, initial=initial)


def _amount_to_minor(amount):
    if amount is None:
        return None
    return int(amount * 100)


def _save_from_form(request, form, rule_id=None):
    return save_category_rule(
        request.user,
        rule_id=rule_id,
        owner_kind=form.cleaned_data["owner_kind"],
        description_contains=form.cleaned_data["description_contains"],
        account_id=None if form.cleaned_data["account"] is None else form.cleaned_data["account"].pk,
        min_amount_minor=_amount_to_minor(form.cleaned_data["min_amount"]),
        max_amount_minor=_amount_to_minor(form.cleaned_data["max_amount"]),
        category_id=form.cleaned_data["category"].pk,
        priority=form.cleaned_data["priority"],
        enabled=form.cleaned_data["enabled"],
    )


@require_http_methods(["GET", "POST"])
@never_cache
def category_rule_list(request):
    person = Person.objects.filter(user=request.user).first()
    if person is None:
        raise Http404
    household = current_household(person)
    unreachable = list_unreachable_applications(request.user)
    if household is None:
        return render(
            request,
            "finance/category_rules.html",
            {"household": None, "rules": [], "form": None, "unreachable_applications": unreachable},
        )
    ensure_household_categories(household)
    initial = None
    if request.method != "POST":
        initial = {}
        contains = (request.GET.get("description_contains") or "").strip()
        if contains:
            initial["description_contains"] = contains
        category = request.GET.get("category")
        if category:
            initial["category"] = category
    form = CategoryRuleForm(request.POST if request.method == "POST" else None, principal=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            rule = _service_or_404(lambda: _save_from_form(request, form))
        except ValidationError as exc:
            form.add_error(None, _first_message(exc, "The rule could not be saved."))
        else:
            return redirect("category-rule-detail", rule_id=rule.pk)
    return render(
        request,
        "finance/category_rules.html",
        {
            "household": household,
            "rules": list_visible_rules(request.user),
            "form": form,
            "unreachable_applications": unreachable,
        },
    )


@require_POST
@never_cache
def category_rule_application_reverse(request, application_id):
    """Undo an application whose rule this person can no longer open."""
    _service_or_404(lambda: reverse_application(request.user, application_id))
    return redirect("category-rule-list")


def _toggle_rule(request, rule, enabled):
    _service_or_404(lambda: set_rule_enabled(request.user, rule.pk, enabled))
    return redirect("category-rule-detail", rule_id=rule.pk)


def _post_save_rule(request, rule, form):
    if not form.is_valid():
        return None
    try:
        saved = _service_or_404(lambda: _save_from_form(request, form, rule_id=rule.pk))
    except ValidationError as exc:
        form.add_error(None, _first_message(exc, "The rule could not be saved."))
        return None
    return redirect("category-rule-detail", rule_id=saved.pk)


def _post_apply_rule(request, rule, errors):
    try:
        version = request.POST.get("rule_version") or None
        _service_or_404(lambda: apply_rule(request.user, rule.pk, previewed_version=version))
    except ValidationError as exc:
        errors["apply"] = _first_message(exc, "The rule could not be applied.")
        return None
    return redirect("category-rule-detail", rule_id=rule.pk)


def _post_reverse_rule(request):
    try:
        application_id = int(request.POST.get("application_id", "0"))
    except (TypeError, ValueError) as exc:
        raise Http404 from exc
    _service_or_404(lambda: reverse_application(request.user, application_id))
    return redirect("category-rule-detail", rule_id=int(request.resolver_match.kwargs["rule_id"]))


def _posted_rule_detail(request, rule, form, action, errors):
    if action == "save":
        return _post_save_rule(request, rule, form)
    if action == "disable":
        return _toggle_rule(request, rule, False)
    if action == "enable":
        return _toggle_rule(request, rule, True)
    if action == "apply":
        return _post_apply_rule(request, rule, errors)
    if action == "reverse":
        return _post_reverse_rule(request)
    return None


@require_http_methods(["GET", "POST"])
@never_cache
def category_rule_detail(request, rule_id):
    person = Person.objects.filter(user=request.user).first()
    if person is None:
        raise Http404
    rule = CategoryRule.objects.visible_to(request.user).select_related("category", "account").filter(pk=rule_id).first()
    if rule is None:
        raise Http404
    rule.inactive = personal_rule_is_inactive(rule)
    action = request.POST.get("action") if request.method == "POST" else None
    form = _form_from_rule(rule, request.user, request.POST if action == "save" else None)
    errors = {}
    posted = _posted_rule_detail(request, rule, form, action, errors)
    if posted is not None:
        return posted
    rule, matches = _service_or_404(lambda: preview_rule(request.user, rule.pk))
    rule.inactive = personal_rule_is_inactive(rule)
    return render(
        request,
        "finance/category_rule_detail.html",
        {
            "rule": rule,
            "form": form,
            "matches": matches,
            "match_count": len(matches),
            "applications": list_visible_applications(request.user, rule),
            "apply_error": errors.get("apply"),
        },
    )
