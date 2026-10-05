from datetime import date

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q
from simple_history.models import HistoricalRecords

from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure, ProcedureKind
from apps.programs.models import Prescription, Program
from apps.staff.models import Instructor


class BookingPeriod(Func):
    """tsrange(date + start, date + end) — реальный интервал занятия для ограничения в БД."""

    output_field = DateTimeRangeField()

    def as_sql(self, compiler, connection, **extra_context):
        day, start, end = (compiler.compile(e) for e in self.get_source_expressions())
        sql = f"tsrange(({day[0]} + {start[0]}), ({day[0]} + {end[0]}), '[)')"
        return sql, (*day[1], *start[1], *day[1], *end[1])


class BookingKind(models.TextChoices):
    LFK_GROUP = ProcedureKind.LFK_GROUP
    POOL = ProcedureKind.POOL
    INDIVIDUAL = ProcedureKind.INDIVIDUAL
    EQUIPMENT = ProcedureKind.EQUIPMENT


class BookingSource(models.TextChoices):
    AUTO = "auto", "подбор"
    MANUAL = "manual", "вручную"
    IMPORT = "import", "импорт"


class Booking(models.Model):
    """Занятие программы в конкретный день (TZ.md, FR-SCH-1).

    Пациент не может быть в двух местах сразу — это гарантирует база (FR-SCH-2), а не только
    движок или форма.
    """

    program = models.ForeignKey(
        Program, on_delete=models.CASCADE, related_name="bookings", verbose_name="программа"
    )
    prescription = models.ForeignKey(
        Prescription, on_delete=models.CASCADE, related_name="bookings", verbose_name="назначение"
    )
    procedure = models.ForeignKey(
        Procedure,
        on_delete=models.PROTECT,
        related_name="bookings",
        verbose_name="процедура",
        help_text="Для бассейна без группы в назначении — выбранная группа бассейна.",
    )
    kind = models.CharField("вид", max_length=20, choices=BookingKind.choices)
    date = models.DateField("дата")
    start = models.TimeField("начало")
    end = models.TimeField("конец")
    group_session = models.ForeignKey(
        GroupSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bookings",
        verbose_name="занятие группы",
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bookings",
        verbose_name="тренажёр",
    )
    # Индивидуальное занятие — инструктор в слоте сетки; время занятия = время слота.
    instructor = models.ForeignKey(
        Instructor,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bookings",
        verbose_name="инструктор",
    )
    slot = models.ForeignKey(
        InstructorSlot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bookings",
        verbose_name="слот",
    )
    note = models.CharField("пометка", max_length=100, blank=True)
    source = models.CharField(
        "источник", max_length=10, choices=BookingSource.choices, default=BookingSource.AUTO
    )
    pinned = models.BooleanField(
        "закреплено", default=False, help_text="Поставлено вручную — подбор его не трогает."
    )
    version = models.PositiveIntegerField("версия", default=1)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "занятие"
        verbose_name_plural = "занятия"
        ordering = ["date", "start"]
        constraints = [
            models.CheckConstraint(condition=Q(end__gt=F("start")), name="booking_end_after_start"),
            # Слот есть ровно у индивидуальных занятий (FR-SCH-1), инструктор — у них же, кроме
            # субботы и воскресенья: в выходные занятие в то же время ведёт дежурный 2/2,
            # инструктор не назначается (решение 56).
            models.CheckConstraint(
                condition=(
                    (
                        Q(kind=ProcedureKind.INDIVIDUAL, slot__isnull=False)
                        & (Q(instructor__isnull=False) | Q(date__iso_week_day__gte=6))
                    )
                    | (
                        ~Q(kind=ProcedureKind.INDIVIDUAL)
                        & Q(instructor__isnull=True, slot__isnull=True)
                    )
                ),
                name="booking_instructor_only_individual",
            ),
            # Один инструктор — один пациент в слоте (FR-SCH-2).
            models.UniqueConstraint(
                fields=["instructor", "date", "slot"],
                condition=Q(kind=ProcedureKind.INDIVIDUAL),
                name="booking_instructor_slot_unique",
                violation_error_message="У инструктора уже есть пациент в этом слоте.",
            ),
            ExclusionConstraint(
                name="booking_patient_not_in_two_places",
                expressions=[
                    ("program", RangeOperators.EQUAL),
                    (BookingPeriod(F("date"), F("start"), F("end")), RangeOperators.OVERLAPS),
                ],
                violation_error_message="У пациента уже есть занятие в это время.",
            ),
        ]
        indexes = [
            models.Index(fields=["equipment", "date"]),
            models.Index(fields=["program", "date"]),
            models.Index(fields=["instructor", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.date:%d.%m} {self.start:%H:%M} {self.procedure}"

    @property
    def place(self) -> str:
        if self.group_session is not None:
            return self.group_session.effective_place
        return self.procedure.place


class RemovedSession(models.Model):
    """Занятие назначения, убранное вручную в дату (шахматка, страница программы).

    Подбор считает его как закреплённое: в эту дату ставит на ``units`` занятий меньше. Иначе
    пересборка после любой правки назначений вернула бы убранное занятие на место.
    """

    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.CASCADE,
        related_name="removed_sessions",
        verbose_name="назначение",
    )
    date = models.DateField("дата")
    units = models.PositiveSmallIntegerField("сколько занятий убрано", default=1)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "убранное занятие"
        verbose_name_plural = "убранные занятия"
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(
                fields=["prescription", "date"], name="removed_session_unique_date"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.date:%d.%m} {self.prescription}: убрано {self.units}"


class StaffChange(models.Model):
    """Сводка перестройки после изменения смены или распорядка инструктора (FR-SCH-13).

    ``items`` — по затронутому занятию: программа, дата, было («инструктор, время») и стало
    (или ``None`` — поставить не удалось), закреплено ли вручную, по желанию ли пациента.
    По сводке специалист проверяет замены и может «Вернуть как было».
    """

    created_at = models.DateTimeField("когда", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name="кто",
    )
    instructor = models.ForeignKey(
        Instructor, on_delete=models.PROTECT, related_name="changes", verbose_name="инструктор"
    )
    title = models.CharField("что изменилось", max_length=200)
    items = models.JSONField("занятия", default=list)
    reverted_at = models.DateTimeField("возвращено", null=True, blank=True)
    reverted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="кто вернул",
    )

    class Meta:
        verbose_name = "перестройка расписания"
        verbose_name_plural = "перестройки расписания"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.created_at:%d.%m.%Y %H:%M} {self.title}"

    @property
    def unplaced(self) -> int:
        return sum(1 for item in self.items if item.get("after") is None)

    @property
    def rows(self) -> list[dict]:
        """Строки сводки с датой-объектом — для показа «05.10»."""
        return [item | {"day": date.fromisoformat(item["date"])} for item in self.items]
