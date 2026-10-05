from django.urls import path

from . import views

app_name = "staff"
urlpatterns = [
    path("shifts/", views.shifts, name="shifts"),
    path("shifts/<int:pk>/toggle/", views.shift_toggle, name="shift_toggle"),
    path("shifts/<int:pk>/pattern/", views.shift_pattern, name="shift_pattern"),
    path("duties/", views.duties, name="duties"),
    path("duties/instructor/<int:pk>/add/", views.duty_add, name="duty_add"),
    path("duties/<int:pk>/edit/", views.duty_edit, name="duty_edit"),
    path("duties/<int:pk>/end/", views.duty_end, name="duty_end"),
    path("blocks/instructor/<int:pk>/add/", views.block_add, name="block_add"),
    path("blocks/<int:pk>/delete/", views.block_delete, name="block_delete"),
]
