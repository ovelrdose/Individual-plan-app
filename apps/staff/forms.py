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


class ShiftPatternForm(BootstrapFormMixin, forms.Form):
    pattern = forms.ChoiceField(label="Шаблон", choices=ShiftPatternKind.choices)
    anchor_date = forms.DateField(
        label="Первый рабочий день цикла",
        widget=DateInput(),
        help_text="Для 2/2 — первый из двух рабочих дней. Для 5/2 и «каждый день» — любой.",
    )
    valid_from = forms.DateField(label="Действует с", widget=DateInput())


class InstructorForm(BootstrapFormMixin, forms.ModelForm):
    """Инструктор в «Справочниках» (FR-STF-1, решение 83). «Работает» нет — архив."""

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

    def validate_unique(self):
        # Занятость напарника не ошибка: сервис set_partner сам разорвёт его прежнюю пару.
        exclude = self._get_validation_exclusions()
        exclude.add("partner")
        try:
            self.instance.validate_unique(exclude=exclude)
        except forms.ValidationError as error:
            self._update_errors(error)
