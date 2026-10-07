from django.urls import path

from . import views

app_name = "programs"
urlpatterns = [
    path("", views.program_list, name="list"),
    path("new/", views.program_create, name="create"),
    path("<int:pk>/", views.program_detail, name="detail"),
    path("<int:pk>/edit/", views.program_edit, name="edit"),
    path("<int:pk>/delete/", views.program_delete, name="delete"),
    path("<int:pk>/repeat/", views.program_repeat, name="repeat"),
    path("<int:pk>/withdraw/", views.program_withdraw, name="withdraw"),
    path("<int:pk>/restore/", views.program_restore, name="restore"),
    path("<int:pk>/warnings/dismiss/", views.program_dismiss_warnings, name="dismiss_warnings"),
    path("<int:pk>/prescriptions/", views.prescription_add, name="prescription_add"),
    path(
        "<int:pk>/prescriptions/<int:prescription_pk>/edit/",
        views.prescription_edit,
        name="prescription_edit",
    ),
    path(
        "<int:pk>/prescriptions/<int:prescription_pk>/delete/",
        views.prescription_delete,
        name="prescription_delete",
    ),
    path(
        "<int:pk>/prescriptions/<int:prescription_pk>/move/<str:direction>/",
        views.prescription_move,
        name="prescription_move",
    ),
]
