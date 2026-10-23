from datetime import time

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from simple_history.models import HistoricalRecords

from apps.org.models import Department

from .domain import add_minutes, ends_same_day, equipment_starts


class Equipment(models.Model):
    """Тренажёр с окном работы (TZ.md, FR-CAT-4, FR-CAT-4a)."""

    name = models.CharField("название", max_length=60, unique=True)
    window_start = models.TimeField("окно с", default=time(13, 0))
    window_end = models.TimeField("окно до", default=time(15, 0))
    step_min = models.PositiveSmallIntegerField(
        "шаг записи, мин", default=15, validators=[MinValueValidator(1)]
    )
    duration_min = models.PositiveSmallIntegerField(
        "длительность, мин", default=15, validators=[MinValueValidator(1)]
    )
    capacity = models.PositiveSmallIntegerField(
        "пациентов на одно время",
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Столько ставит подбор. Вручную можно записать больше — с пометкой.",
    )
    is_active = models.BooleanField("действует", default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "тренажёр"
        verbose_name_plural = "тренажёры"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if self.window_start and self.window_end and self.window_end <= self.window_start:
            raise ValidationError(
                {"window_end": "Окно должно заканчиваться позже, чем начинается."}
            )
        has_window = self.window_start and self.window_end and self.duration_min and self.step_min
        if has_window and not self.start_times():
            raise ValidationError(
                {"duration_min": "В окно не помещается ни одной записи такой длительности."}
            )

    def start_times(self) -> list[time]:
        return equipment_starts(
            self.window_start, self.window_end, self.step_min, self.duration_min
        )


class ProcedureKind(models.TextChoices):
    LFK_GROUP = "LFK_GROUP", "Группа ЛФК"
    # Группы дневного стационара (Gruppy_DS.xlsx): назначают пациентам любого отделения,
    # с инструкторами и шахматкой не связаны — как бассейн.
    DS_GROUP = "DS_GROUP", "Группа ДС"
    POOL = "POOL", "Бассейн"
    INDIVIDUAL = "INDIVIDUAL", "Индивидуальное занятие"
    EQUIPMENT = "EQUIPMENT", "Тренажёр"
    CARD_ONLY = "CARD_ONLY", "Только строка в карте"


# Виды, у которых есть расписание групп (GroupSession).
SESSION_KINDS = (ProcedureKind.LFK_GROUP, ProcedureKind.DS_GROUP, ProcedureKind.POOL)
# Виды, которые подбор ставит в расписание программы сам (TZ.md §7.3, шаги 1–4).
SCHEDULED_KINDS = (
    ProcedureKind.LFK_GROUP,
    ProcedureKind.DS_GROUP,
    ProcedureKind.POOL,
    ProcedureKind.INDIVIDUAL,
    ProcedureKind.EQUIPMENT,
)


class SectionBehavior(models.TextChoices):
    """Как группы раздела ведут себя в расписании — один из видов групп (решение 82).
    Значения совпадают с ``ProcedureKind``: расписание по-прежнему смотрит на вид процедуры."""

    LFK_GROUP = ProcedureKind.LFK_GROUP.value, "Ведёт инструктор"
    DS_GROUP = ProcedureKind.DS_GROUP.value, "Без инструктора"
    POOL = ProcedureKind.POOL.value, "Бассейн"


BEHAVIOR_HELP = {
    SectionBehavior.LFK_GROUP: "Группу может вести инструктор ЛФК — её занятие занимает его "
    "ячейку в расписании.",
    SectionBehavior.DS_GROUP: "На расписание инструкторов не влияет; в листе назначений "
    "распознаётся последней.",
    SectionBehavior.POOL: "Из таких групп выбирают группу для назначения «Бассейн» без уточнения.",
}

# Названия разделов, которые заводятся сами, если группы такого вида ещё некуда положить.
DEFAULT_SECTION_NAMES = {
    SectionBehavior.LFK_GROUP: "ЛФК",
    SectionBehavior.DS_GROUP: "ДС",
    SectionBehavior.POOL: "Бассейн",
}


class Section(models.Model):
    """Раздел экрана «Групповые занятия» — вкладка с группами одного поведения
    (TZ.md, FR-CAT-1a, решение 82)."""

    name = models.CharField("название", max_length=60)
    behavior = models.CharField("поведение", max_length=20, choices=SectionBehavior.choices)
    order = models.PositiveSmallIntegerField("порядок", default=0)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "раздел групп"
        verbose_name_plural = "разделы групп"
        ordering = ["order", "name"]
        constraints = [
            # «ДС» и «дс» — один раздел: вкладки с одинаковыми названиями путали бы.
            models.UniqueConstraint(
                Lower("name"),
                name="unique_section_name_ci",
                violation_error_message="Раздел с таким названием уже есть.",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if not self.pk:
            return
        old = Section.objects.filter(pk=self.pk).values_list("behavior", flat=True).first()
        if old and old != self.behavior and self.procedures.exists():
            # Поведение раздела — это вид его групп, а по виду работает расписание (решение 82).
            raise ValidationError(
                {"behavior": "В разделе есть группы — поведение раздела не меняется."}
            )

    @property
    def behavior_help(self) -> str:
        return BEHAVIOR_HELP.get(self.behavior, "")

    @classmethod
    def default_for(cls, kind: str) -> "Section":
        """Первый раздел с поведением ``kind``; если такого нет — заводится новый в конце."""
        section = cls.objects.filter(behavior=kind).order_by("order", "pk").first()
        if section is not None:
            return section
        base = DEFAULT_SECTION_NAMES[SectionBehavior(kind)]
        name, number = base, 1
        while cls.objects.filter(name__iexact=name).exists():
            number += 1
            name = f"{base} {number}"
        last = cls.objects.aggregate(models.Max("order"))["order__max"]
        return cls.objects.create(name=name, behavior=kind, order=(last or 0) + 1)


class Procedure(models.Model):
    """Процедура из назначения (TZ.md, FR-CAT-1, FR-CAT-2)."""

    name = models.CharField("название", max_length=100)
    card_label = models.CharField(
        "как печатается в карте", max_length=60, help_text="Например «Группа Эрго общая»."
    )
    synonyms = ArrayField(
        models.CharField(max_length=100),
        verbose_name="синонимы",
        default=list,
        blank=True,
        help_text="Как процедура пишется в листе назначений. Регистр, пробелы, точки и тире "
        "при сравнении не учитываются.",
    )
    kind = models.CharField("вид", max_length=20, choices=ProcedureKind.choices)
    default_duration_min = models.PositiveSmallIntegerField(
        "длительность по умолчанию, мин", null=True, blank=True
    )
    place = models.CharField("место проведения", max_length=60, blank=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="procedures",
        verbose_name="тренажёр",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="procedures",
        verbose_name="отделение",
        help_text="Пусто — процедура общая для всего центра.",
    )
    # Только группировка на экране «Групповые занятия»: поведение в расписании задаёт ``kind``,
    # он всегда равен поведению раздела (решение 82).
    section = models.ForeignKey(
        Section,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="procedures",
        verbose_name="раздел",
        help_text="Вкладка на экране «Групповые занятия». Только у групп.",
    )
    # Явный признак, а не «бассейн без занятий»: иначе случайно добавленное занятие молча
    # превратило бы «Бассейн» в группу и сломало выбор группы во всех программах.
    group_choice = models.BooleanField(
        "группу выбирает специалист ФР",
        default=False,
        help_text="Для «Бассейн» без группы: своего расписания нет, группу бассейна выбирает "
        "специалист ФР в расписании программы.",
    )
    # Мото-Л и Артромот проводятся на индивидуальном занятии, но только вечером: это не
    # отдельное занятие, а условие для одного из индивидуальных (TZ.md, FR-SCH-8).
    evening_individual = models.BooleanField(
        "вечернее индивидуальное",
        default=False,
        help_text="Проводится на одном из индивидуальных занятий пациента, только с 18:00. "
        "Своего расписания и строки в карте нет.",
    )
    is_active = models.BooleanField(
        "действует",
        default=True,
        help_text="Группу выключают на экране «Групповые занятия»: там выключаются и её занятия.",
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = "процедура"
        verbose_name_plural = "процедуры"
        ordering = ["kind", "name"]
        constraints = [
            # Без учёта регистра: «Эрго общая» и «эрго общая» в листе назначений неразличимы.
            models.UniqueConstraint(
                Lower("name"),
                "department",
                name="unique_procedure_name_ci_per_department",
                nulls_distinct=False,
                violation_error_message="Процедура с таким названием уже есть в этом отделении "
                "или среди общих.",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:
        # Группа без раздела не видна на экране «Групповые занятия»: заведённая в админке,
        # командой или тестом, она попадает в первый раздел своего вида (решение 82).
        if self.section_id is None and self.is_group:
            self.section = Section.default_for(self.kind)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "section"}
        super().save(*args, **kwargs)

    @property
    def is_group(self) -> bool:
        """Группа с расписанием — у неё есть раздел (решение 82)."""
        return self.kind in SESSION_KINDS and not self.group_choice

    @property
    def is_generic_pool(self) -> bool:
        """«Бассейн» без группы: в листе назначений тип не указан, группу бассейна выбирает
        специалист ФР (TZ.md, FR-CAT-2, FR-PRG-3)."""
        return self.kind == ProcedureKind.POOL and self.group_choice

    def clean_fields(self, exclude=None) -> None:
        # Пустые строки убираем до проверки полей, иначе они дают ошибку «поле пустое».
        self.synonyms = [item.strip() for item in self.synonyms if item and item.strip()]
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        if self.kind == ProcedureKind.EQUIPMENT and self.equipment_id is None:
            raise ValidationError({"equipment": "Для тренажёра выберите тренажёр."})
        if self.kind != ProcedureKind.EQUIPMENT and self.equipment_id is not None:
            raise ValidationError({"equipment": "Тренажёр указывается только для вида «Тренажёр»."})
        if self.group_choice and self.kind != ProcedureKind.POOL:
            raise ValidationError({"group_choice": "Выбор группы бывает только у бассейна."})
        if self.evening_individual and self.kind != ProcedureKind.CARD_ONLY:
            raise ValidationError(
                {"evening_individual": "Вечернее индивидуальное — вид «Только строка в карте»."}
            )
        if self.section_id is not None:
            if not self.is_group:
                raise ValidationError({"section": "Раздел бывает только у групп с расписанием."})
            if self.kind != self.section.behavior:
                raise ValidationError(
                    {
                        "section": f"В разделе «{self.section}» группы с поведением "
                        f"«{self.section.get_behavior_display()}» — выберите раздел с тем же "
                        "поведением."
                    }
                )
        if self.group_choice and self.pk and self.sessions.exists():
            raise ValidationError(
                {"group_choice": "У процедуры есть расписание — это уже группа бассейна."}
            )


class GroupSession(models.Model):
    """Ежедневное занятие группы ЛФК, ДС или бассейна (TZ.md, FR-CAT-3). Вместимость
    не ограничена."""

    procedure = models.ForeignKey(
        Procedure, on_delete=models.PROTECT, related_name="sessions", verbose_name="группа"
    )
    start_time = models.TimeField("начало")
    duration_min = models.PositiveSmallIntegerField(
        "длительность, мин", default=30, validators=[MinValueValidator(1)]
    )
    place = models.CharField(
        "место проведения", max_length=60, blank=True, help_text="Пусто — как у группы."
    )
    is_active = models.BooleanField("действует", default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "занятие группы"
        verbose_name_plural = "групповые занятия"
        ordering = ["start_time", "procedure__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["procedure", "start_time"], name="unique_session_start"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.start_time:%H:%M} {self.procedure}"

    def clean(self) -> None:
        if self.procedure_id and self.procedure.kind not in SESSION_KINDS:
            raise ValidationError(
                {"procedure": "Расписание бывает только у групп ЛФК, ДС и бассейна."}
            )
        if self.procedure_id and self.procedure.group_choice:
            raise ValidationError(
                {"procedure": "У «Бассейн» без группы нет расписания — выберите группу бассейна."}
            )
        if (
            self.start_time
            and self.duration_min
            and not ends_same_day(self.start_time, self.duration_min)
        ):
            # Расписание групп — в пределах суток: конец после полуночи не посчитать.
            raise ValidationError(
                {
                    "duration_min": f"Занятие с {self.start_time:%H:%M} на {self.duration_min} мин "
                    "заканчивается после полуночи — уменьшите длительность или время начала."
                }
            )

    @property
    def end_time(self) -> time:
        return add_minutes(self.start_time, self.duration_min)

    @property
    def effective_place(self) -> str:
        return self.place or self.procedure.place


class InstructorSlot(models.Model):
    """Слот сетки инструкторов — одна сетка на центр (TZ.md, FR-CAT-5)."""

    start = models.TimeField("начало", unique=True)
    end = models.TimeField("конец")
    is_evening = models.BooleanField(
        "вечерний",
        default=False,
        help_text="Подбор не использует вечерние слоты — только ручная запись.",
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = "слот инструкторов"
        verbose_name_plural = "сетка слотов инструкторов"
        ordering = ["start"]

    def __str__(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M}"

    def clean(self) -> None:
        if self.start and self.end:
            if self.end <= self.start:
                raise ValidationError({"end": "Слот должен заканчиваться позже, чем начинается."})
            overlapping = InstructorSlot.objects.filter(start__lt=self.end, end__gt=self.start)
            if self.pk:
                overlapping = overlapping.exclude(pk=self.pk)
            if overlapping.exists():
                raise ValidationError(f"Слот пересекается со слотом {overlapping.first()}.")
