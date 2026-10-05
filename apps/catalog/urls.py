from django.urls import path

from . import views

app_name = "catalog"
urlpatterns = [
    path("procedures/search/", views.procedure_search, name="procedure_search"),
]
