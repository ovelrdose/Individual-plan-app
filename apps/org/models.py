from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from simple_history.models import HistoricalRecords

SHRM_VALUES = (3, 4, 5)


def default_course_days() -> dict[str, int]:
    # Календарные дни, включая день поступления (01-analysis.md §7, п. 2).
    return {"3": 11, "4": 15, "5": 20}


class Department(models.Model):
    """Отделение медицинской реабилитации (TZ.md, FR-ORG-1)."""

    name = models.CharField("название", max_length=100, unique=True)
    code = models.SlugField("код", max_length=20, unique=True)
    course_days = models.JSONField(
        "длительность курса по ШРМ, дней",
        default=default_course_days,
        help_text='Например: {"3": 11, "4": 15, "5": 20}',
    )
    max_individual_per_day = models.PositiveSmallIntegerField(
        "индивидуальных занятий в день, не больше",
        default=2,
        validators=[MinValueValidator(1)],
    )
    is_active = models.BooleanField("действует", default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "отделение"
        verbose_name_plural = "отделения"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        days = self.course_days
        expected = {str(shrm) for shrm in SHRM_VALUES}
        if not isinstance(days, dict) or set(days) != expected:
            raise ValidationError({"course_days": "Нужны ровно три значения: для ШРМ 3, 4 и 5."})
        if not all(isinstance(value, int) and value > 0 for value in days.values()):
            raise ValidationError(
                {"course_days": "Длительность курса — целое положительное число дней."}
            )

    def course_length(self, shrm: int) -> int:
        return self.course_days[str(shrm)]
