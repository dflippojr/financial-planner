from django.conf import settings
from django.contrib.auth.middleware import LoginRequiredMiddleware


def _static_prefix():
    prefix = settings.STATIC_URL or "/static/"
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    if not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return prefix


class LoginRequiredExceptStaticMiddleware(LoginRequiredMiddleware):
    """Default-deny auth, with WhiteNoise's /static/ prefix left public."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.path.startswith(_static_prefix()):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)


class MemberSessionActivityMiddleware:
    """Keep the member session index current without scanning django_session."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .security_services import enforce_session_validity, touch_member_session

        enforce_session_validity(request)
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            touch_member_session(request)
        return response
