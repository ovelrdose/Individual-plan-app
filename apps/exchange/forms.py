from pathlib import Path

from django import forms
from django.db.models import QuerySet

from apps.accounts.access import departments_for, is_admin, role_in
from apps.accounts.models import Role, User
from apps.common.forms import BootstrapFormMixin
from apps.org.models import Department
from apps.programs.services import doctors_for

from .services import MAX_FILE_SIZE


def import_departments(user: User) -> QuerySet[Department]:
    """Отделения, куда пользователь может загрузить лист: где он врач (администратор — все)."""
    departments = departments_for(user)
    if is_admin(user):
        return departments
    return departments.filter(memberships__user=user, memberships__role=Role.DOCTOR)


class SheetUploadForm(BootstrapFormMixin, forms.Form):
    file = forms.FileField(
        label="Лист назначений (.docx)",
        widget=forms.ClearableFileInput(attrs={"accept": ".docx"}),
    )
    department = forms.ModelChoiceField(
        queryset=Department.objects.none(),
        label="Отделение",
        empty_label=None,
        required=False,
        help_text="Программа появится в списке этого отделения — её увидят все его сотрудники.",
    )
    attending_doctor = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Лечащий врач",
        required=False,
        help_text="Нужен, если загружает не врач отделения (например, администратор).",
    )

    def __init__(self, *args, user: User, initial_department: Department | None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        departments = import_departments(user)
        self.fields["department"].queryset = departments
        # Отделение из шапки — по умолчанию, но не молча: поле видно всегда
        # (врач, работающий в двух отделениях, иначе мог создать программу «не там»).
        # Подставляем только отделение из шапки: молча выбрать «другое подходящее» — значит
        # создать программу не там, где пользователь её ждёт.
        self.default_department = (
            departments.filter(pk=initial_department.pk).first()
            if initial_department is not None
            else None
        )
        shown = self.default_department or departments.first()
        if shown is not None:
            self.fields["department"].initial = shown.pk
        doctors = User.objects.filter(
            is_active=True, memberships__department__in=departments, memberships__role=Role.DOCTOR
        ).distinct()
        self.fields["attending_doctor"].queryset = doctors.order_by("short_name", "last_name")
        self.fields["attending_doctor"].label_from_instance = lambda doctor: str(doctor)
        self.needs_doctor_choice = any(role_in(user, d) != Role.DOCTOR for d in departments)
        if not self.needs_doctor_choice:
            del self.fields["attending_doctor"]

    def clean_file(self):
        file = self.cleaned_data["file"]
        if Path(file.name).suffix.lower() != ".docx":
            raise forms.ValidationError("Нужен файл Word в формате .docx.")
        if file.size > MAX_FILE_SIZE:
            raise forms.ValidationError("Файл больше 5 МБ — это не похоже на лист назначений.")
        return file

    def clean(self):
        cleaned = super().clean()
        if "department" in self.errors:
            return cleaned
        # Без явного выбора — отделение из шапки (оно же показано в поле по умолчанию).
        department = cleaned.get("department") or self.default_department
        cleaned["department"] = department
        if department is None:
            self.add_error("department", "Выберите отделение.")
            return cleaned
        if role_in(self.user, department) == Role.DOCTOR:
            cleaned["chosen_doctor"] = self.user
            return cleaned
        doctor = cleaned.get("attending_doctor")
        if doctor is None:
            self.add_error("attending_doctor", "Выберите лечащего врача.")
        elif not doctors_for(department).filter(pk=doctor.pk).exists():
            self.add_error("attending_doctor", "Этот врач не работает в выбранном отделении.")
        else:
            cleaned["chosen_doctor"] = doctor
        return cleaned
