from django.urls import path

from . import views

app_name = "exchange"
urlpatterns = [
    path("prescription-sheet/", views.sheet_import, name="sheet_import"),
    path("prescription-sheet/force/", views.sheet_import_force, name="sheet_import_force"),
]
