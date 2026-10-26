from django.urls import path

from . import views

app_name = "catalog"
urlpatterns = [
    path("", views.catalog, name="index"),
    path("procedures/search/", views.procedure_search, name="procedure_search"),
    path("procedures/add/", views.procedure_add, name="procedure_add"),
    path("procedures/<int:pk>/", views.procedure_edit, name="procedure_edit"),
    path("procedures/<int:pk>/delete/", views.procedure_delete, name="procedure_delete"),
    path("procedures/<int:pk>/restore/", views.procedure_restore, name="procedure_restore"),
    path("instructors/add/", views.instructor_add, name="instructor_add"),
    path("instructors/<int:pk>/", views.instructor_edit, name="instructor_edit"),
    path("instructors/<int:pk>/delete/", views.instructor_delete, name="instructor_delete"),
    path("instructors/<int:pk>/restore/", views.instructor_restore, name="instructor_restore"),
]
