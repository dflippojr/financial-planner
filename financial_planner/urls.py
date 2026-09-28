from django.urls import path

from finance import views
from finance.csv_import import views as csv_import_views


urlpatterns = [
    path("", views.home, name="home"),
    path("sign-in/", views.sign_in, name="login"),
    path("sign-out/", views.sign_out, name="logout"),
    path("invite/", views.invite, name="invite"),
    path("join/", views.join, name="join"),
    path("recover/", views.recover, name="recover"),
    path("accounts/<int:account_id>/imports/preview/", csv_import_views.csv_preview, name="csv-import-preview"),
]
