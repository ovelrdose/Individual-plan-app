from django import forms
from django.db.models import QuerySet

from apps.catalog.models import GroupSession, ProcedureKind
from apps.common.forms import BootstrapFormMixin, DateInput

from .models import ShiftPatternKind


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
