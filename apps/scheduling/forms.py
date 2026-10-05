from django import forms

from apps.accounts.models import User
from apps.catalog.models import Equipment, GroupSession
from apps.common.forms import BootstrapFormMixin

from .services import group_schedule_procedures


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%H:%M")


class GroupSessionForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = GroupSession
        fields = ["procedure", "start_time", "duration_min", "place", "is_active"]
        widgets = {"start_time": TimeInput()}

    def __init__(self, *args, user: User, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["procedure"].queryset = group_schedule_procedures(user)
        if self.instance.pk:
            # Группу у занятия не меняют (services.save_group_session).
            self.fields["procedure"].disabled = True


class EquipmentForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Equipment
        fields = ["window_start", "window_end", "step_min", "duration_min", "capacity", "is_active"]
        widgets = {"window_start": TimeInput(), "window_end": TimeInput()}
