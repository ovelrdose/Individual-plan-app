from django.apps import AppConfig


class LiveConfig(AppConfig):
    name = "apps.live"
    verbose_name = "Обновление в реальном времени"

    def ready(self) -> None:
        from . import signals  # noqa: F401 — подключает обработчики сигналов
