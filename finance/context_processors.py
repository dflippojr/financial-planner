from .google_auth import google_signin_enabled


def google_signin(request):
    return {"google_signin_enabled": google_signin_enabled()}
