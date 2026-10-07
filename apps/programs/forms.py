from django import forms

from apps.catalog.services import visible_procedures
from apps.common.forms import BootstrapFormMixin, DateInput
from apps.org.models import Department

from .models import Prescription, Program, WithdrawalReason
from .services import doctors_for


class ProgramForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Program
        fields = [
            "full_name",
            "sex",
            "age",
            "history_number",
            "room",
            "shrm",
            "diagnosis",
            "attending_doctor",
            "start_date",
            "end_date",
        ]
        widgets = {"start_date": DateInput(), "end_date": DateInput()}

    def __init__(self, *args, department: Department, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["attending_doctor"].queryset = doctors_for(department)
        self.fields["attending_doctor"].label_from_instance = lambda user: str(user)
        self.fields["end_date"].required = False
        self.fields[
            "end_date"
        ].help_text = "Пусто — по ШРМ. Другая дата — продление или досрочное окончание."
        self._end_was_manual = self.instance.end_date_manual

    def clean(self):
        cleaned = super().clean()
        # Расчётную дату окончания врач не трогал — она пересчитается в save_program.
        # Без этого форма сверила бы новую дату начала со старой датой окончания.
        if "end_date" not in self.changed_data and not self._end_was_manual:
            cleaned["end_date"] = None
        return cleaned


class PrescriptionForm(BootstrapFormMixin, forms.ModelForm):
    """Новое назначение: процедура выбирается поиском и приходит скрытым полем."""

    procedure = forms.ModelChoiceField(
        queryset=None,
        widget=forms.HiddenInput,
        error_messages={"required": "Выберите процедуру из списка."},
    )

    class Meta:
        model = Prescription
        fields = ["procedure", "duration_min", "per_day", "start_date", "cancel_date", "in_card"]
        widgets = {"start_date": DateInput(), "cancel_date": DateInput()}
        labels = {"duration_min": "Мин", "per_day": "Раз/день"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["procedure"].queryset = visible_procedures(self.instance.program.department)
        # Выбор процедуры в подсказке подставляет её длительность (Alpine.js, x-ref).
        self.fields["duration_min"].widget.attrs["x-ref"] = "duration"

    @property
    def procedure_label(self) -> str:
        """Название выбранной процедуры — чтобы после ошибки выбор остался на экране."""
        procedure = getattr(self, "cleaned_data", {}).get("procedure")
        return procedure.name if procedure else ""


class PrescriptionEditForm(BootstrapFormMixin, forms.ModelForm):
    """Правка строки, в том числе выбор процедуры для нераспознанной строки из листа."""

    class Meta:
        model = Prescription
        fields = ["procedure", "duration_min", "per_day", "start_date", "cancel_date", "in_card"]
        widgets = {"start_date": DateInput(), "cancel_date": DateInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        procedure = self.fields["procedure"]
        procedure.queryset = visible_procedures(self.instance.program.department).order_by(
            "kind", "name"
        )
        procedure.required = True
        procedure.empty_label = "— выберите процедуру —"
        procedure.label_from_instance = lambda p: f"{p.name} · {p.get_kind_display()}"


class WithdrawalForm(BootstrapFormMixin, forms.Form):
    """Выбытие пациента (FR-PRG-9): с какого дня у него нет занятий и почему."""

    date_from = forms.DateField(
        label="Выбыл с", widget=DateInput(), help_text="Первый день без занятий."
    )
    reason = forms.ChoiceField(label="Причина", choices=WithdrawalReason.choices)
    note = forms.CharField(label="Комментарий", max_length=200, required=False)
