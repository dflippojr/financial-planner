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
