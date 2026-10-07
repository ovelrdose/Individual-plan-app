from django.urls import path

from . import views

app_name = "staff"
urlpatterns = [
    path("shifts/", views.shifts, name="shifts"),
    path("shifts/<int:pk>/toggle/", views.shift_toggle, name="shift_toggle"),
    path("shifts/<int:pk>/pattern/", views.shift_pattern, name="shift_pattern"),
    path("duties/", views.duties, name="duties"),
    path("duties/paint/", views.duty_paint, name="duty_paint"),
]
