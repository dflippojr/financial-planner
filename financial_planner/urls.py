from django.urls import path

from finance import views
from finance.csv_import import views as csv_import_views


urlpatterns = [
    path("health/", views.health, name="health"),
    path("", views.home, name="home"),
    path("spending/", views.spending_by_category, name="spending-by-category"),
    path("sign-in/", views.sign_in, name="login"),
    path("sign-out/", views.sign_out, name="logout"),
    path("invite/", views.invite, name="invite"),
    path("join/", views.join, name="join"),
    path("recover/", views.recover, name="recover"),
    path("accounts/<int:account_id>/imports/preview/", csv_import_views.csv_preview, name="csv-import-preview"),
    path("accounts/<int:account_id>/imports/<int:batch_id>/undo/", csv_import_views.csv_undo_import, name="csv-import-undo"),
    path("transactions/", views.transaction_list, name="transaction-list"),
    path("transactions/<int:transaction_id>/edit/", views.transaction_edit, name="transaction-edit"),
    path("transactions/<int:transaction_id>/category/", views.transaction_categorize, name="transaction-categorize"),
    path("transactions/<int:transaction_id>/refund/", views.transaction_link_refund, name="transaction-link-refund"),
    path("categories/", views.category_list, name="category-list"),
    path("transfers/", views.transfer_review, name="transfer-review"),
    path("recurring/", views.recurring_review, name="recurring-review"),
]
