from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from apps.scheduling import views as scheduling_views

admin.site.site_header = "Программа реабилитации — администрирование"
admin.site.site_title = "Администрирование"

urlpatterns = [
    # Главный экран — индивидуальные программы текущего отделения.
    path("", RedirectView.as_view(pattern_name="programs:list"), name="home"),
    path("programs/", include("apps.programs.urls")),
    path("import/", include("apps.exchange.urls")),
    path("cards/", include("apps.cards.urls")),
    path("schedule/", include("apps.scheduling.urls")),
    # Публичная шахматка без входа (FR-ACC-5): сети задаёт PUBLIC_BOARD_NETWORKS.
    path("board/", scheduling_views.public_board, name="public_board"),
    path("catalog/", include("apps.catalog.urls")),
    path("staff/", include("apps.staff.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("admin/", admin.site.urls),
]
