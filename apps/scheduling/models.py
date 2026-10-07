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
    DS_GROUP = ProcedureKind.DS_GROUP
    POOL = ProcedureKind.POOL
    INDIVIDUAL = ProcedureKind.INDIVIDUAL
    EQUIPMENT = ProcedureKind.EQUIPMENT


# Занятия по расписанию групп: время выбирается из занятий группы (GroupSession).
GROUP_BOOKING_KINDS = (BookingKind.LFK_GROUP, BookingKind.DS_GROUP, BookingKind.POOL)


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
            # Слот и инструктор есть ровно у индивидуальных занятий: они стоят только в шахматке
            # по будням (TZ.md, FR-SCH-3). В одной ячейке может быть сколько угодно пациентов
            # (FR-SCH-4, FR-SCH-10), поэтому уникальности «инструктор + дата + слот» нет.
            models.CheckConstraint(
                condition=(
                    Q(kind=ProcedureKind.INDIVIDUAL, slot__isnull=False, instructor__isnull=False)
                    | (
                        ~Q(kind=ProcedureKind.INDIVIDUAL)
                        & Q(instructor__isnull=True, slot__isnull=True)
                    )
                ),
                name="booking_instructor_only_individual",
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


class BoardDay(models.Model):
    """Шахматка индивидуальных на будний день составлена (TZ.md, FR-SCH-5).

    Создаётся сама — на сегодня и на следующий будний день — копированием предыдущей
    составленной шахматки. Дальше следующего будного дня расписание не составляется.
    """

    date = models.DateField("дата", unique=True)
    created_at = models.DateTimeField("когда составлена", auto_now_add=True)

    class Meta:
        verbose_name = "шахматка на дату"
        verbose_name_plural = "шахматки на дату"
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"Шахматка {self.date:%d.%m.%Y}"


class BoardPatient(models.Model):
    """Пациент в шахматке на дату вне сетки: блоки «Не распределены» и «Отменены» (FR-SCH-11).

    ``unplaced`` — занятия, которым не нашлось места (переполнение, инструктор не вышел),
    ``cancelled`` — убранные специалистом из сетки: переходят на следующие дни, пока пациент
    лечится. ``edited`` — пациента в эту дату правили вручную: правка сегодняшней шахматки его
    завтрашнее положение уже не меняет (FR-SCH-12).
    """

    date = models.DateField("дата")
    program = models.ForeignKey(
        Program, on_delete=models.CASCADE, related_name="board_days", verbose_name="программа"
    )
    unplaced = models.PositiveSmallIntegerField("не распределено", default=0)
    cancelled = models.PositiveSmallIntegerField("отменено", default=0)
    edited = models.BooleanField("правили вручную", default=False)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "пациент в шахматке"
        verbose_name_plural = "пациенты в шахматке"
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["date", "program"], name="board_patient_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.date:%d.%m} {self.program}"


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
