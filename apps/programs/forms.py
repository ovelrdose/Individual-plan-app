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


class _ProcedurePicker:
    """Процедура выбирается поиском (catalog:procedure_search) и приходит скрытым полем."""

    def _setup_procedure(self) -> None:
        procedure = self.fields["procedure"]
        procedure.queryset = visible_procedures(self.instance.program.department)
        procedure.required = True
        procedure.widget = forms.HiddenInput()
        procedure.error_messages["required"] = "Выберите процедуру из списка."
        # Выбор процедуры в подсказке подставляет её длительность (Alpine.js, x-ref).
        self.fields["duration_min"].widget.attrs["x-ref"] = "duration"
        self.fields["per_day"].widget.attrs.update({"x-ref": "perDay", "min": 1})
        # Общая подсказка к датам — одна под обоими полями (панель назначения).
        self.fields["cancel_date"].help_text = ""

    @property
    def procedure_label(self) -> str:
        """Название выбранной процедуры — чтобы после ошибки выбор остался на экране."""
        if self.is_bound:
            procedure = getattr(self, "cleaned_data", {}).get("procedure")
            return procedure.name if procedure else ""
        return self.instance.procedure.name if self.instance.procedure else ""

    @property
    def procedure_id(self) -> str:
        value = self["procedure"].value()
        return "" if value is None else str(value)


class PrescriptionForm(_ProcedurePicker, BootstrapFormMixin, forms.ModelForm):
    """Новое назначение."""

    class Meta:
        model = Prescription
        fields = ["procedure", "duration_min", "per_day", "start_date", "cancel_date", "in_card"]
        widgets = {"start_date": DateInput(), "cancel_date": DateInput()}
        labels = {"duration_min": "Мин", "per_day": "Раз в день", "cancel_date": "Отменено с"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_procedure()


class PrescriptionEditForm(_ProcedurePicker, BootstrapFormMixin, forms.ModelForm):
    """Правка строки, в том числе выбор процедуры для нераспознанной строки из листа."""

    class Meta:
        model = Prescription
        fields = [
            "procedure",
            "duration_min",
            "per_day",
            "start_date",
            "cancel_date",
            "in_card",
            "preferred_time",
        ]
        widgets = {"start_date": DateInput(), "cancel_date": DateInput()}
        labels = {"duration_min": "Мин", "per_day": "Раз в день", "cancel_date": "Отменено с"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_procedure()
        # Время группы — из её занятий (решение 71); другое время модель всё равно отклонит.
        procedure = self.instance.procedure
        times = (
            sorted(procedure.sessions.filter(is_active=True).values_list("start_time", flat=True))
            if procedure
            else []
        )
        current = self.instance.preferred_time
        if current and current not in times:
            times = sorted([*times, current])
        self.session_times = times
        self.fields["preferred_time"].help_text = "«Любое» — подбор выберет сам."
        self.fields["preferred_time"].widget = forms.Select(
            choices=[("", "любое"), *((t.isoformat(), f"{t:%H:%M}") for t in times)],
            attrs={"class": "form-select", ":disabled": "id != original"},
        )


class WithdrawalForm(BootstrapFormMixin, forms.Form):
    """Выбытие пациента (FR-PRG-9): с какого дня у него нет занятий и почему."""

    date_from = forms.DateField(
        label="Выбыл с", widget=DateInput(), help_text="Первый день без занятий."
    )
    reason = forms.ChoiceField(label="Причина", choices=WithdrawalReason.choices)
    note = forms.CharField(label="Комментарий", max_length=200, required=False)
