"""Формы экрана «Справочники» (TZ.md FR-CAT-7, решение 83)."""

from django import forms

from apps.accounts.access import departments_for
from apps.accounts.models import User
from apps.common.forms import BootstrapFormMixin, StringListField

from .models import Procedure, ProcedureKind
from .services import NEW_KINDS


class ProcedureForm(BootstrapFormMixin, forms.ModelForm):
    """Процедура не-группа: название, подпись, синонимы, вид, длительность, место, отделение,
    признаки. «Действует» нет — «Удалить» / «Вернуть из архива»."""

    synonyms = StringListField(
        label="Синонимы", required=False, help_text="Как процедуру пишут в листе назначений."
    )

    class Meta:
        model = Procedure
        fields = [
            "name",
            "card_label",
            "synonyms",
            "kind",
            "default_duration_min",
            "place",
            "department",
            "group_choice",
            "evening_individual",
        ]
        labels = {"default_duration_min": "Длительность, мин"}

    def __init__(self, *args, user: User, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["card_label"].required = False
        self.fields["card_label"].help_text = "Пусто — как название."
        kind = self.fields["kind"]
        if self.instance.pk and self.instance.kind == ProcedureKind.EQUIPMENT:
            # Тренажёр заводится и выключается в «Групповых занятиях» — вид не меняется.
            kind.choices = [(ProcedureKind.EQUIPMENT.value, ProcedureKind.EQUIPMENT.label)]
            kind.disabled = True
        else:
            kind.choices = [(value.value, value.label) for value in NEW_KINDS]
        self.fields[
            "kind"
        ].help_text = "«Бассейн» — только без группы (группу выбирает специалист ФР)."
        department = self.fields["department"]
        department.queryset = departments_for(user).order_by("name")
        department.empty_label = "Общая для центра"
        self.fields["default_duration_min"].widget.attrs["min"] = 1
