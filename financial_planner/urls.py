from django.urls import path

from finance import views


urlpatterns = [
    path("health/", views.health, name="health"),
    path("", views.home, name="home"),
    path("sign-in/", views.sign_in, name="login"),
    path("sign-out/", views.sign_out, name="logout"),
    path("invite/", views.invite, name="invite"),
    path("join/", views.join, name="join"),
    path("recover/", views.recover, name="recover"),
]
