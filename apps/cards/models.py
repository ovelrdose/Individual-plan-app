from django.conf import settings
from django.db import models

from apps.programs.models import Program


class CardExport(models.Model):
    """Журнал выгрузок карты (TZ.md, FR-CRD-5). Сама карта не хранится — она всегда
    собирается из актуальных данных программы."""

    program = models.ForeignKey(
        Program, on_delete=models.CASCADE, related_name="card_exports", verbose_name="программа"
    )
    # Удаление пользователя администратором не должно упираться в журнал выгрузок.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name="кто выгрузил",
    )
    created_at = models.DateTimeField("когда", auto_now_add=True)

    class Meta:
        verbose_name = "выгрузка карты"
        verbose_name_plural = "выгрузки карт"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.program} — {self.created_at:%d.%m.%Y %H:%M}"
