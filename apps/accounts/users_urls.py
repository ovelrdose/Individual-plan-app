from django.urls import path

from . import views

app_name = "users"

urlpatterns = [
    path("", views.user_list, name="list"),
    path("new/", views.user_create, name="create"),
    path("<int:pk>/", views.user_edit, name="edit"),
    path("<int:pk>/password/", views.user_password, name="password"),
    path("<int:pk>/unlock/", views.user_unlock, name="unlock"),
    path("departments/", views.department_list, name="departments"),
    path("departments/add/", views.department_add, name="department_add"),
    path("departments/<int:pk>/", views.department_edit, name="department_edit"),
    path("departments/<int:pk>/delete/", views.department_delete, name="department_delete"),
    path("departments/<int:pk>/restore/", views.department_restore, name="department_restore"),
]
