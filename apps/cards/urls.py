from django.urls import path

from . import views

app_name = "cards"
urlpatterns = [
    path("program/<int:pk>/", views.card_download, name="download"),
]
