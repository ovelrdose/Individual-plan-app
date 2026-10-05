from django import forms

from apps.catalog.models import GroupSession, InstructorSlot, ProcedureKind
from apps.common.forms import BootstrapFormMixin, DateInput

from .models import BlockKind, InstructorBlock, InstructorDuty, ShiftPatternKind


class GroupSessionChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj: GroupSession) -> str:
        return f"{obj.start_time:%H:%M} {obj.procedure.name}"


class SlotChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj: InstructorSlot) -> str:
        return f"{obj.start:%H:%M}–{obj.end:%H:%M}" + (" (вечер)" if obj.is_evening else "")


def _lead_sessions():
    # Ведёт инструктор ЛФК только группы ЛФК: бассейн с шахматкой не связан (глоссарий).
    return GroupSession.objects.filter(
        is_active=True, procedure__kind=ProcedureKind.LFK_GROUP, procedure__is_active=True
    ).select_related("procedure")


class ShiftPatternForm(BootstrapFormMixin, forms.Form):
    pattern = forms.ChoiceField(label="Шаблон", choices=ShiftPatternKind.choices)
    anchor_date = forms.DateField(
        label="Первый рабочий день цикла",
        widget=DateInput(),
        help_text="Для 2/2 — первый из двух рабочих дней. Для 5/2 и «каждый день» — любой.",
    )
    valid_from = forms.DateField(label="Действует с", widget=DateInput())


class DutyFieldsForm(BootstrapFormMixin, forms.ModelForm):
    """Общие поля обязанности: слот, вид, группа, подпись."""

    slot = SlotChoice(label="Слот", queryset=InstructorSlot.objects.all())
    group_session = GroupSessionChoice(
        label="Группа", queryset=_lead_sessions(), required=False, empty_label="—"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group_session"].queryset = _lead_sessions()
        # Alpine показывает группу и подпись только для своего вида (шаблоны staff/_*.html).
        self.fields["kind"].widget.attrs["x-model"] = "kind"

    # Поля, которые Alpine прячет в зависимости от вида: их ошибки показываем вверху формы.
    HIDEABLE = ("group_session", "label")

    def clean(self) -> dict:
        data = super().clean()
        kind = data.get("kind")
        # Скрытые поля всё равно отправляются: пользователь выбрал группу, затем сменил вид на
        # БОС. Лишнее молча отбрасываем — иначе ошибка оказалась бы в невидимом поле.
        if kind != BlockKind.GROUP_LEAD:
            data["group_session"] = None
            self.instance.group_session = None
        if kind != BlockKind.OTHER:
            data["label"] = ""
            self.instance.label = ""
        return data

    def _post_clean(self) -> None:
        super()._post_clean()
        for name in self.HIDEABLE:
            if name in self._errors:
                for message in self._errors.pop(name):
                    self.add_error(None, f"{self.fields[name].label}: {message}")


class DutyForm(DutyFieldsForm):
    class Meta:
        model = InstructorDuty
        fields = ["slot", "kind", "group_session", "label", "valid_from", "valid_to"]
        widgets = {"valid_from": DateInput(), "valid_to": DateInput()}


class BlockForm(DutyFieldsForm):
    class Meta:
        model = InstructorBlock
        fields = ["date", "slot", "kind", "group_session", "label"]
        widgets = {"date": DateInput()}


class EndDutyForm(forms.Form):
    valid_to = forms.DateField(label="Последний день", widget=DateInput())
