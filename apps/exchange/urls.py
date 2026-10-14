from django.urls import path

from . import views

app_name = "exchange"
urlpatterns = [
    path("prescription-sheet/", views.sheet_import, name="sheet_import"),
    path("prescription-sheet/force/", views.sheet_import_force, name="sheet_import_force"),
    path("programs/<int:pk>/update-sheet/", views.sheet_update, name="sheet_update"),
    path("prescription-sheet/update/", views.sheet_update_preview, name="sheet_update_preview"),
    path("prescription-sheet/update/apply/", views.sheet_update_apply, name="sheet_update_apply"),
]
