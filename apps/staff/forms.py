from django import forms
from django.db.models import QuerySet

from apps.catalog.models import GroupSession, ProcedureKind
from apps.common.forms import BootstrapFormMixin, DateInput

from .models import Instructor, ShiftPatternKind


def board_sessions() -> QuerySet[GroupSession]:
    """Группы, которые может вести инструктор, — для «кисти» «Ведение группы».

    Только группы ЛФК: бассейн с шахматкой не связан (глоссарий).
    """
    return (
        GroupSession.objects.filter(
            is_active=True, procedure__kind=ProcedureKind.LFK_GROUP, procedure__is_active=True
        )
        .select_related("procedure")
        .order_by("start_time", "procedure__name")
    )


class InstructorForm(BootstrapFormMixin, forms.ModelForm):
    """Карточка инструктора (FR-STF-1, решения 83, 87): фамилия, ФИО, напарник 2/2, порядок
    и шаблон смены с даты. «Работает» нет — архив."""

    pattern = forms.ChoiceField(
        label="Шаблон смены",
        choices=[("", "не задан — не работает"), *ShiftPatternKind.choices],
        required=False,
    )
    anchor_date = forms.DateField(
        label="Первый рабочий день цикла",
        widget=DateInput(),
        required=False,
        help_text="Для 2/2 — первый из двух рабочих дней.",
    )
    valid_from = forms.DateField(
        label="Шаблон действует с",
        widget=DateInput(),
        required=False,
        help_text="Прежний шаблон закончится накануне.",
    )

    class Meta:
        model = Instructor
        fields = ["short_name", "full_name", "partner", "display_order"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        partners = Instructor.objects.filter(is_active=True).order_by("display_order", "short_name")
        if self.instance.pk:
            partners = partners.exclude(pk=self.instance.pk)
        self.fields["partner"].queryset = partners
        self.fields["partner"].empty_label = "нет — работает один"
        # Шаблон, с которым открыли окно: менять смену, только если его действительно сменили.
        self.shown_pattern = (self.initial.get("pattern", ""), self.initial.get("anchor_date"))

    def clean(self):
        data = super().clean()
        if not data.get("pattern") and self.shown_pattern[0]:
            self.add_error(
                "pattern",
                "Шаблон не убирают: нерабочие дни отмечают в календаре, ушедшего — в архив.",
            )
        if data.get("pattern"):
            for name in ("anchor_date", "valid_from"):
                if not data.get(name):
                    self.add_error(name, "Укажите дату.")
        return data

    def pattern_change(self) -> dict | None:
        """Новый шаблон смены для ``change_shift_pattern`` или None, если его не меняли."""
        data = self.cleaned_data
        if not data.get("pattern"):
            return None
        if (data["pattern"], data["anchor_date"]) == self.shown_pattern:
            return None
        return {key: data[key] for key in ("pattern", "anchor_date", "valid_from")}

    def validate_unique(self):
        # Занятость напарника не ошибка: сервис set_partner сам разорвёт его прежнюю пару.
        exclude = self._get_validation_exclusions()
        exclude.add("partner")
        try:
            self.instance.validate_unique(exclude=exclude)
        except forms.ValidationError as error:
            self._update_errors(error)
