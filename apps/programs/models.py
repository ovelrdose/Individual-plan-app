from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, F, OuterRef, Q
from django.utils.functional import cached_property
from simple_history.models import HistoricalRecords

from apps.catalog.models import Procedure, ProcedureKind
from apps.org.models import SHRM_VALUES, Department

from .domain import (
    Gap,
    course_dates,
    course_end,
    is_absent,
    is_therapy_day,
    room_label,
    surname,
    therapy_dates,
)


class Sex(models.TextChoices):
    MALE = "М", "мужской"
    FEMALE = "Ж", "женский"


class ProgramSource(models.TextChoices):
    IMPORT = "import", "лист назначений"
    MANUAL = "manual", "вручную"


class Program(models.Model):
    """Индивидуальная программа медицинской реабилитации на один курс (TZ.md, FR-PRG-1).

    Это не история болезни: выписки нет, программа завершается по дате окончания или
    выбытием пациента (``Withdrawal``, FR-PRG-9).
    """

    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="programs", verbose_name="отделение"
    )
    full_name = models.CharField("ФИО", max_length=150)
    sex = models.CharField(
        "пол",
        max_length=1,
        choices=Sex.choices,
        blank=True,
        help_text="При импорте определяется по отчеству.",
    )
    age = models.PositiveSmallIntegerField(
        "возраст",
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(120)],
        help_text="Необязательно. Без возраста в карте печатается только пол.",
    )
    history_number = models.CharField("№ ИБ", max_length=20, blank=True)
    room = models.CharField("палата", max_length=10, help_text="Например «9» или «5а».")
    shrm = models.PositiveSmallIntegerField(
        "ШРМ", choices=[(value, str(value)) for value in SHRM_VALUES]
    )
    diagnosis = models.CharField("диагноз", max_length=255, blank=True)
    attending_doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="programs",
        verbose_name="лечащий врач",
    )
    start_date = models.DateField("начало курса")
    end_date = models.DateField("окончание курса")
    end_date_manual = models.BooleanField(
        "окончание задано вручную",
        default=False,
        help_text="Если нет — дата пересчитывается при смене ШРМ или начала курса.",
    )
    source = models.CharField(
        "создана", max_length=10, choices=ProgramSource.choices, default=ProgramSource.MANUAL
    )
    import_warnings = models.JSONField("проверить после импорта", default=list, blank=True)
    schedule_issues = models.JSONField(
        "проблемы расписания",
        default=list,
        blank=True,
        help_text="Конфликты и предупреждения последнего подбора: [{code, message, conflict}].",
    )
    # Прошлый курс того же пациента (FR-PRG-10): карточки пациента нет, курсы связаны цепочкой.
    previous = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="next_courses",
        verbose_name="прошлый курс",
    )
    created_at = models.DateTimeField("создана", auto_now_add=True)
    updated_at = models.DateTimeField("изменена", auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "индивидуальная программа"
        verbose_name_plural = "индивидуальные программы"
        ordering = ["room", "full_name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")),
                name="program_end_not_before_start",
                violation_error_message="Окончание курса не может быть раньше начала.",
            ),
        ]
        indexes = [models.Index(fields=["department", "end_date"])]

    def __str__(self) -> str:
        return f"{self.full_name}, палата {self.room}"

    @property
    def surname(self) -> str:
        return surname(self.full_name)

    @property
    def room_label(self) -> str:
        """«9п», «5а» — как палата пишется в шахматке."""
        return room_label(self.room)

    def planned_end_date(self) -> date:
        return course_end(self.start_date, self.department.course_length(self.shrm))

    def course_dates(self) -> list[date]:
        return course_dates(self.start_date, self.end_date)

    @cached_property
    def gaps(self) -> list[Gap]:
        """Периоды выбытия (FR-PRG-9). Кэшируются на экземпляре: сервисы, которые их меняют,
        работают с заново прочитанной программой."""
        # .all() — чтобы работал prefetch_related("withdrawals") в списках.
        return [(item.date_from, item.returned_on) for item in self.withdrawals.all()]

    def therapy_dates(self) -> list[date]:
        """Дни занятий — курс без дня поступления и дня выписки (TZ.md, FR-SCH-1) и без дней
        после выбытия (FR-PRG-9)."""
        return therapy_dates(self.start_date, self.end_date, self.gaps)

    def is_therapy_day(self, day: date) -> bool:
        return is_therapy_day(self.start_date, self.end_date, day, self.gaps)

    def is_absent(self, day: date) -> bool:
        return is_absent(self.gaps, day)

    @cached_property
    def withdrawal(self) -> "Withdrawal | None":
        """Текущее выбытие: пациент выбыл и не восстановлен."""
        return next((item for item in self.withdrawals.all() if item.returned_on is None), None)

    @property
    def has_schedule_conflicts(self) -> bool:
        return any(issue.get("conflict") for issue in self.schedule_issues)

    def is_finished(self, today: date | None = None) -> bool:
        return self.end_date < (today or date.today())


def withdrawn_on(day: date) -> Exists:
    """Условие для запросов: пациент выбыл и в этот день не лечится (FR-PRG-9)."""
    return Exists(
        Withdrawal.objects.filter(program=OuterRef("pk"), date_from__lte=day).filter(
            Q(returned_on__isnull=True) | Q(returned_on__gt=day)
        )
    )


class WithdrawalReason(models.TextChoices):
    EARLY_DISCHARGE = "early_discharge", "досрочная выписка"
    REFUSAL = "refusal", "отказ"
    TRANSFER = "transfer", "перевод"
    LEFT = "left", "самовольный уход"
    OTHER = "other", "другое"


class Withdrawal(models.Model):
    """Выбытие пациента раньше срока (TZ.md, FR-PRG-9, решение 62).

    С ``date_from`` у пациента нет занятий. Восстановление ставит ``returned_on`` — первый
    день, когда он снова лечится: дни отсутствия остаются пустыми, расписание их не заполняет.
    """

    program = models.ForeignKey(
        Program, on_delete=models.CASCADE, related_name="withdrawals", verbose_name="программа"
    )
    date_from = models.DateField("выбыл с", help_text="Первый день без занятий.")
    reason = models.CharField("причина", max_length=20, choices=WithdrawalReason.choices)
    note = models.CharField("комментарий", max_length=200, blank=True)
    returned_on = models.DateField("восстановлен с", null=True, blank=True)
    created_at = models.DateTimeField("отмечено", auto_now_add=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "выбытие"
        verbose_name_plural = "выбытия"
        ordering = ["date_from"]
        constraints = [
            # Выбыть второй раз можно только после восстановления.
            models.UniqueConstraint(
                fields=["program"],
                condition=Q(returned_on__isnull=True),
                name="withdrawal_one_open_per_program",
            ),
        ]

    def __str__(self) -> str:
        return f"Выбыл {self.date_from:%d.%m} ({self.get_reason_display()})"


class Prescription(models.Model):
    """Назначение врача (TZ.md, FR-PRG-3).

    Пустые start_date / cancel_date означают «с первого дня курса» / «до конца курса» —
    так назначение не устаревает при сдвиге дат программы.
    cancel_date — первый день, когда процедура уже не проводится.
    """

    program = models.ForeignKey(
        Program, on_delete=models.CASCADE, related_name="prescriptions", verbose_name="программа"
    )
    procedure = models.ForeignKey(
        Procedure,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="prescriptions",
        verbose_name="процедура",
        help_text="Пусто — строка из листа назначений не распознана.",
    )
    raw_text = models.TextField("текст из листа назначений", blank=True)
    duration_min = models.PositiveSmallIntegerField(
        "длительность, мин", null=True, blank=True, validators=[MinValueValidator(1)]
    )
    per_day = models.PositiveSmallIntegerField(
        "раз в день", default=1, validators=[MinValueValidator(1)]
    )
    start_date = models.DateField("с даты", null=True, blank=True)
    cancel_date = models.DateField(
        "отменено с даты", null=True, blank=True, help_text="Первый день без процедуры."
    )
    in_card = models.BooleanField("в карту", default=True)
    # Запись врача «Бассейн» не меняем: группу (верхняя/нижняя конечность, спина) выбирает
    # специалист ФР по времени её занятий — она хранится отдельно (TZ.md, FR-PRG-3).
    pool_group = models.ForeignKey(
        Procedure,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pool_prescriptions",
        limit_choices_to={"kind": ProcedureKind.POOL},
        verbose_name="группа бассейна",
        help_text="Для назначения «Бассейн» без группы; выбирает специалист ФР.",
    )
    card_order = models.PositiveIntegerField("порядок в карте", default=0)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "назначение"
        verbose_name_plural = "назначения"
        ordering = ["card_order", "pk"]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        return str(self.procedure) if self.procedure else self.raw_text or "—"

    @property
    def effective_start(self) -> date:
        return self.start_date or self.program.start_date

    def clean(self) -> None:
        program = self.program
        errors = {}
        if self.start_date and not (program.start_date <= self.start_date <= program.end_date):
            errors["start_date"] = "Дата начала должна быть в пределах курса."
        if self.cancel_date:
            if self.cancel_date <= self.effective_start:
                errors["cancel_date"] = "Отменить можно не раньше следующего дня после начала."
            elif self.cancel_date > program.end_date:
                errors["cancel_date"] = "Дата отмены — не позже окончания курса."
        if (
            self.procedure
            and self.procedure.kind == ProcedureKind.INDIVIDUAL
            and self.per_day > program.department.max_individual_per_day
        ):
            errors["per_day"] = (
                "Индивидуальных занятий в день — не больше "
                f"{program.department.max_individual_per_day}."
            )
        if errors:
            raise ValidationError(errors)
