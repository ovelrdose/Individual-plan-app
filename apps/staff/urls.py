from django.urls import path

from . import views

app_name = "staff"
urlpatterns = [
    path("shifts/", views.shifts, name="shifts"),
    path("shifts/<int:pk>/toggle/", views.shift_toggle, name="shift_toggle"),
    path("instructors/add/", views.instructor_add, name="instructor_add"),
    path("instructors/<int:pk>/", views.instructor_edit, name="instructor_edit"),
    path("instructors/<int:pk>/delete/", views.instructor_delete, name="instructor_delete"),
    path("instructors/<int:pk>/restore/", views.instructor_restore, name="instructor_restore"),
    path("duties/", views.duties, name="duties"),
    path("duties/paint/", views.duty_paint, name="duty_paint"),
]
