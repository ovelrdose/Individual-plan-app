"""Форма отделения: длительность курса — отдельные поля по ШРМ, не сырой JSON (FR-ORG-3)."""

from django import forms

from apps.common.forms import BootstrapFormMixin

from .models import Department, default_course_days

# Поле формы → ключ в course_days.
COURSE_FIELDS = {"course_3": "3", "course_4": "4", "course_5": "5", "course_uk": "УК"}


class DepartmentForm(BootstrapFormMixin, forms.ModelForm):
    course_3 = forms.IntegerField(label="ШРМ 3", min_value=1)
    course_4 = forms.IntegerField(label="ШРМ 4", min_value=1)
    course_5 = forms.IntegerField(label="ШРМ 5", min_value=1)
    course_uk = forms.IntegerField(label="УК", min_value=1)

    class Meta:
        model = Department
        fields = ["name", "code", "max_individual_per_day"]
        help_texts = {"code": "Латиницей, без пробелов: например omr4."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        days = self.instance.course_days or default_course_days()
        for name, key in COURSE_FIELDS.items():
            self.fields[name].initial = days.get(key)

    def clean(self):
        cleaned = super().clean()
        if all(cleaned.get(name) for name in COURSE_FIELDS):
            self.instance.course_days = {key: cleaned[name] for name, key in COURSE_FIELDS.items()}
        return cleaned
