from datetime import time

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q
from simple_history.models import HistoricalRecords

from apps.catalog.models import ProcedureKind

from . import domain


class Instructor(models.Model):
    """Инструктор ЛФК (TZ.md, FR-STF-1). Смены — ShiftPattern и ShiftException."""

    full_name = models.CharField("ФИО", max_length=150)
    short_name = models.CharField(
        "как в шахматке", max_length=60, unique=True, help_text="Например «Ким»."
    )
    partner = models.OneToOneField(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="напарник по 2/2",
        help_text="Напарники работают в одной колонке шахматки. Связь ставится с обеих сторон.",
    )
    display_order = models.PositiveSmallIntegerField("порядок в шахматке", default=0)
    is_active = models.BooleanField("работает", default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "инструктор"
        verbose_name_plural = "инструкторы"
        ordering = ["display_order", "short_name"]

    def __str__(self) -> str:
        return self.short_name

    def clean(self) -> None:
        if self.partner_id is not None and self.partner_id == self.pk:
            raise ValidationError({"partner": "Инструктор не может быть напарником сам себе."})

    @property
    def team_label(self) -> str:
        """Заголовок колонки шахматки: «Паршуков/ Ким» для пары, иначе фамилия."""
        if self.partner is None:
            return self.short_name
        first, second = sorted([self, self.partner], key=lambda i: (i.display_order, i.short_name))
        return f"{first.short_name}/ {second.short_name}"


class DateRange(Func):
    """daterange(valid_from, valid_to, '[]') — период действия для ограничений в БД.

    Пустой ``valid_to`` даёт бесконечную верхнюю границу — «бессрочно».
    """

    function = "DATERANGE"
    output_field = DateRangeField()


def _period(prefix: str = "valid") -> DateRange:
    return DateRange(F(f"{prefix}_from"), F(f"{prefix}_to"), RangeBoundary(inclusive_upper=True))


class ShiftPatternKind(models.TextChoices):
    TWO_TWO = domain.Pattern.TWO_TWO, "2/2"
    FIVE_TWO = domain.Pattern.FIVE_TWO, "5/2 (пн–пт)"
    DAILY = domain.Pattern.DAILY, "каждый день"


class DutyKind(models.TextChoices):
    METHOD_WORK = domain.DutyKind.METHOD_WORK, domain.DUTY_TEXT[domain.DutyKind.METHOD_WORK]
    GROUP_LEAD = domain.DutyKind.GROUP_LEAD, domain.DUTY_TEXT[domain.DutyKind.GROUP_LEAD]
    BOS = domain.DutyKind.BOS, domain.DUTY_TEXT[domain.DutyKind.BOS]
    OTHER = domain.DutyKind.OTHER, domain.DUTY_TEXT[domain.DutyKind.OTHER]


class BlockKind(models.TextChoices):
    METHOD_WORK = DutyKind.METHOD_WORK.value, DutyKind.METHOD_WORK.label
    GROUP_LEAD = DutyKind.GROUP_LEAD.value, DutyKind.GROUP_LEAD.label
    BOS = DutyKind.BOS.value, DutyKind.BOS.label
    OTHER = DutyKind.OTHER.value, DutyKind.OTHER.label
    CLEAR = domain.DutyKind.CLEAR, "Снять распорядок в этот день"


class PeriodMixin(models.Model):
    """Период действия ``valid_from…valid_to`` включительно; пустой конец — бессрочно."""

    valid_from = models.DateField("действует с")
    valid_to = models.DateField(
        "действует по", null=True, blank=True, help_text="Пусто — бессрочно."
    )

    class Meta:
        abstract = True

    def clean_period(self) -> None:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({"valid_to": "Конец периода раньше его начала."})

    @property
    def period_text(self) -> str:
        start = f"с {self.valid_from:%d.%m.%Y}"
        return f"{start} по {self.valid_to:%d.%m.%Y}" if self.valid_to else f"{start}, бессрочно"


class ShiftPattern(PeriodMixin):
    """Шаблон смены инструктора с периодом действия (TZ.md, FR-STF-2).

    Периоды одного инструктора не пересекаются — это гарантирует база (ExclusionConstraint);
    clean() даёт понятное сообщение раньше.
    """

    instructor = models.ForeignKey(
        Instructor,
        on_delete=models.PROTECT,
        related_name="shift_patterns",
        verbose_name="инструктор",
    )
    pattern = models.CharField("шаблон", max_length=10, choices=ShiftPatternKind.choices)
    anchor_date = models.DateField(
        "первый рабочий день цикла", help_text="Для 2/2: любой первый день из двух рабочих."
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = "шаблон смены"
        verbose_name_plural = "шаблоны смен"
        ordering = ["instructor", "valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="shift_pattern_period_valid",
            ),
            ExclusionConstraint(
                name="shift_pattern_no_overlap",
                expressions=[
                    ("instructor", RangeOperators.EQUAL),
                    (_period(), RangeOperators.OVERLAPS),
                ],
                violation_error_message="Периоды шаблонов смен инструктора пересекаются.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.instructor}: {self.get_pattern_display()} {self.period_text}"

    def clean(self) -> None:
        self.clean_period()
        if self.instructor_id and self.valid_from:
            others = ShiftPattern.objects.filter(instructor_id=self.instructor_id).exclude(
                pk=self.pk
            )
            for other in others:
                if domain.periods_overlap(
                    self.valid_from, self.valid_to, other.valid_from, other.valid_to
                ):
                    raise ValidationError(f"Пересекается с шаблоном {other.period_text}.")

    def to_domain(self) -> domain.ShiftRule:
        return domain.ShiftRule(
            domain.Pattern(self.pattern), self.anchor_date, self.valid_from, self.valid_to
        )


class ShiftException(models.Model):
    """Исключение из шаблона на дату: отпуск, болезнь, подмена (TZ.md, FR-STF-3)."""

    instructor = models.ForeignKey(
        Instructor,
        on_delete=models.PROTECT,
        related_name="shift_exceptions",
        verbose_name="инструктор",
    )
    date = models.DateField("дата")
    is_working = models.BooleanField("работает")
    reason = models.CharField("причина", max_length=100, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "исключение из смены"
        verbose_name_plural = "исключения из смен"
        ordering = ["date", "instructor"]
        constraints = [
            models.UniqueConstraint(fields=["instructor", "date"], name="unique_shift_exception"),
        ]

    def __str__(self) -> str:
        state = "работает" if self.is_working else "не работает"
        return f"{self.instructor} {self.date:%d.%m.%Y}: {state}"

    def to_domain(self) -> domain.ShiftOverride:
        return domain.ShiftOverride(self.date, self.is_working)


class DutyFieldsMixin(models.Model):
    """Общие поля обязанности: слот, вид, группа (для ведения группы), подпись (для «прочего»)."""

    slot = models.ForeignKey(
        "catalog.InstructorSlot", on_delete=models.PROTECT, related_name="+", verbose_name="слот"
    )
    group_session = models.ForeignKey(
        "catalog.GroupSession",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="занятие группы",
        help_text="Только для ведения группы.",
    )
    label = models.CharField("подпись", max_length=60, blank=True, help_text="Для «Прочее».")

    class Meta:
        abstract = True

    def clean_kind_fields(self) -> None:
        """Группа нужна ровно для ведения группы, подпись — для «прочего» (FR-STF-5)."""
        errors = {}
        if self.kind == DutyKind.GROUP_LEAD:
            if self.group_session_id is None:
                errors["group_session"] = "Выберите занятие группы, которое ведёт инструктор."
            elif self.group_session.procedure.kind != ProcedureKind.LFK_GROUP:
                # Бассейн с шахматкой инструкторов ЛФК не связан (глоссарий).
                errors["group_session"] = "Инструктор ведёт только группы ЛФК."
            elif self.slot_id is not None:
                session, slot = self.group_session, self.slot
                if not (session.start_time < slot.end and slot.start < session.end_time):
                    errors["group_session"] = (
                        f"Занятие группы {session.start_time:%H:%M} не попадает в слот {slot}."
                    )
        elif self.group_session_id is not None:
            errors["group_session"] = "Занятие группы указывают только для ведения группы."
        if self.kind == DutyKind.OTHER and not self.label.strip():
            errors["label"] = "Укажите, чем занят инструктор."
        if errors:
            raise ValidationError(errors)

    @property
    def display_label(self) -> str:
        """Подпись для домена и экранов: группа, текст «прочего» или пусто (тогда — вид)."""
        if self.kind == DutyKind.GROUP_LEAD and self.group_session_id:
            return self.group_session.procedure.name
        if self.kind == DutyKind.OTHER:
            return self.label
        return ""

    @property
    def text(self) -> str:
        return self.display_label or self.get_kind_display()

    @property
    def group_span(self) -> tuple[time, time] | None:
        """Время занятия группы: ведение группы занимает все слоты, с которыми оно пересекается."""
        if self.kind == DutyKind.GROUP_LEAD and self.group_session_id:
            return self.group_session.start_time, self.group_session.end_time
        return None


class InstructorDuty(DutyFieldsMixin, PeriodMixin):
    """Постоянный распорядок: что инструктор делает в слоте каждый рабочий день (FR-STF-5).

    В одном слоте у инструктора не больше одной обязанности на пересекающийся период —
    гарантирует база.
    """

    instructor = models.ForeignKey(
        Instructor, on_delete=models.PROTECT, related_name="duties", verbose_name="инструктор"
    )
    kind = models.CharField("вид", max_length=20, choices=DutyKind.choices)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "обязанность распорядка"
        verbose_name_plural = "распорядок инструкторов"
        ordering = ["instructor", "slot__start", "valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="instructor_duty_period_valid",
            ),
            ExclusionConstraint(
                name="instructor_duty_no_overlap",
                expressions=[
                    ("instructor", RangeOperators.EQUAL),
                    ("slot", RangeOperators.EQUAL),
                    (_period(), RangeOperators.OVERLAPS),
                ],
                violation_error_message="В этом слоте у инструктора уже есть обязанность.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.instructor} {self.slot}: {self.text}"

    def clean(self) -> None:
        self.clean_period()
        self.clean_kind_fields()
        if self.instructor_id and self.slot_id and self.valid_from:
            others = InstructorDuty.objects.filter(
                instructor_id=self.instructor_id, slot_id=self.slot_id
            ).exclude(pk=self.pk)
            for other in others:
                if domain.periods_overlap(
                    self.valid_from, self.valid_to, other.valid_from, other.valid_to
                ):
                    raise ValidationError(
                        f"В слоте {self.slot} уже есть «{other.text}» {other.period_text}."
                    )

    def to_domain(self) -> domain.Duty:
        return domain.Duty(
            self.slot_id,
            domain.DutyKind(self.kind),
            self.valid_from,
            self.valid_to,
            self.display_label,
            self.group_span,
        )


class InstructorBlock(DutyFieldsMixin):
    """Разовое изменение слота на дату: другая обязанность или «снять распорядок» (FR-STF-5)."""

    instructor = models.ForeignKey(
        Instructor, on_delete=models.PROTECT, related_name="blocks", verbose_name="инструктор"
    )
    date = models.DateField("дата")
    kind = models.CharField("вид", max_length=20, choices=BlockKind.choices)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "разовый блок"
        verbose_name_plural = "разовые блоки"
        ordering = ["date", "slot__start", "instructor"]
        constraints = [
            models.UniqueConstraint(
                fields=["instructor", "date", "slot"], name="unique_instructor_block"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.instructor} {self.date:%d.%m.%Y} {self.slot}: {self.text}"

    def clean(self) -> None:
        self.clean_kind_fields()

    def to_domain(self) -> domain.Block:
        return domain.Block(
            self.date,
            self.slot_id,
            domain.DutyKind(self.kind),
            self.display_label,
            self.group_span,
        )
