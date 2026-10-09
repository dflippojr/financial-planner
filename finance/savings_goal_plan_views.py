from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .access import request_person as _person
from .access import service_or_404 as _service_or_404
from .csv_import.parser import CsvInputError
from .csv_import.staging import KIND_GOAL_IMPORT, StageUnavailable, create_stage, delete_stage, load_stage
from .forms import GoalImportForm, SavingsBufferForm
from .projection import DEFAULT_HORIZON, HORIZONS
from .savings_goal_import import GoalFileError, commit_goal_import, preview_goal_import
from .savings_goal_plan import SCOPES, build_funding_plan, set_savings_buffer

IMPORT_TEMPLATE = "finance/savings_goal_import.html"
STALE_MESSAGE = "That preview expired. Upload the file again."


def _choice(value, allowed, default):
    return value if value in allowed else default


def _horizon(value):
    try:
        return _choice(int(value), HORIZONS, DEFAULT_HORIZON)
    except (TypeError, ValueError):
        return DEFAULT_HORIZON


def _buffer_initial(plan):
    if plan.buffer_source != "household":
        return None
    return {"buffer": f"{plan.buffer_minor // 100}.{plan.buffer_minor % 100:02d}"}


@require_http_methods(["GET"])
@never_cache
def savings_goal_plan(request):
    _person(request)
    scope = _choice(request.GET.get("scope", ""), SCOPES, "")
    horizon = _horizon(request.GET.get("horizon"))
    plan = build_funding_plan(request.user, today=timezone.localdate(), scope=scope, horizon=horizon)
    return render(
        request,
        "finance/savings_goal_plan.html",
        {
            "plan": plan,
            "scopes": (("", "Everything I can see"), ("private", "My private goals"), ("household", "Household goals")),
            "horizons": HORIZONS,
            "buffer_form": SavingsBufferForm(initial=_buffer_initial(plan)),
        },
    )


@require_POST
@never_cache
def savings_goal_buffer(request):
    _person(request)
    form = SavingsBufferForm(request.POST)
    if form.is_valid():
        _service_or_404(lambda: set_savings_buffer(request.user, form.buffer_minor()), also=(ValidationError,))
        messages.success(request, "Safety buffer saved.")
    else:
        messages.error(request, "Enter a buffer of zero or more, or leave it blank.")
    return redirect("savings-goal-plan")


def _render_import(request, **context):
    context.setdefault("upload_form", GoalImportForm())
    return render(request, IMPORT_TEMPLATE, context)


def _stage_upload(request, form):
    try:
        token, content = create_stage(request, None, form.cleaned_data["goals_file"], kind=KIND_GOAL_IMPORT)
    except CsvInputError as exc:
        form.add_error("goals_file", str(exc))
        return None, None
    try:
        return token, preview_goal_import(request.user, content)
    except GoalFileError as exc:
        delete_stage(request, token)
        form.add_error("goals_file", str(exc))
        return None, None


def _commit_import(request):
    token = request.POST.get("token", "")
    try:
        content = load_stage(request, token, None, kind=KIND_GOAL_IMPORT)
    except StageUnavailable:
        messages.error(request, STALE_MESSAGE)
        return redirect("savings-goal-import")
    try:
        result = commit_goal_import(request.user, content)
    except (ValidationError, GoalFileError):
        # Data changed since the preview: show it again with the current errors.
        try:
            return _render_import(request, preview=preview_goal_import(request.user, content), token=token)
        except GoalFileError:
            messages.error(request, STALE_MESSAGE)
            return redirect("savings-goal-import")
    delete_stage(request, token)
    counts = result.counts
    messages.success(
        request,
        f"Imported {counts.create} new, {counts.update} updated and {counts.unchanged} unchanged goals.",
    )
    return redirect("savings-goals")


@require_http_methods(["GET", "POST"])
@never_cache
def savings_goal_import(request):
    _person(request)
    if request.method == "GET":
        return _render_import(request)
    if request.POST.get("action") == "commit":
        return _service_or_404(lambda: _commit_import(request))
    if request.POST.get("action") == "cancel":
        delete_stage(request, request.POST.get("token", ""))
        return redirect("savings-goals")
    form = GoalImportForm(request.POST, request.FILES)
    if not form.is_valid():
        return _render_import(request, upload_form=form)
    token, preview = _stage_upload(request, form)
    if preview is None:
        return _render_import(request, upload_form=form)
    return _render_import(request, preview=preview, token=token)
