from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from simple_history.admin import SimpleHistoryAdmin

from .models import Instructor, InstructorBlock, InstructorDuty, ShiftException, ShiftPattern
from .services import save_instructor


class InstructorForm(forms.ModelForm):
    class Meta:
        model = Instructor
        fields = ["full_name", "short_name", "partner", "display_order", "is_active"]

    def validate_unique(self):
        # Занятость напарника не ошибка: сервис set_partner сам разорвёт его прежнюю пару.
        exclude = self._get_validation_exclusions()
        exclude.add("partner")
        try:
            self.instance.validate_unique(exclude=exclude)
        except ValidationError as error:
            self._update_errors(error)


@admin.register(Instructor)
class InstructorAdmin(SimpleHistoryAdmin):
    form = InstructorForm
    list_display = ["short_name", "full_name", "partner", "display_order", "is_active"]
    list_editable = ["display_order"]
    list_filter = ["is_active"]
    search_fields = ["short_name", "full_name"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "partner":
            queryset = Instructor.objects.filter(is_active=True)
            object_id = request.resolver_match.kwargs.get("object_id")
            if object_id:
                queryset = queryset.exclude(pk=object_id)
            kwargs["queryset"] = queryset
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        # Пару ставит сервис — он поддерживает симметрию A↔B и блокирует участников по pk
        # раньше любого изменения (тот же порядок, что у подбора расписания). Так же — правка
        # порядка прямо в списке (list_editable).
        obj._history_user = request.user
        save_instructor(
            obj,
            form.cleaned_data.get("partner", obj.partner),
            relink="partner" in form.changed_data,
        )


# Смены и распорядок правят на экранах «Смены» и «Распорядок» (services проверяют права и пишут
# журнал); в админке — для администратора, если нужно поправить запись напрямую.


@admin.register(ShiftPattern)
class ShiftPatternAdmin(SimpleHistoryAdmin):
    list_display = ["instructor", "pattern", "anchor_date", "valid_from", "valid_to"]
    list_filter = ["pattern", "instructor"]


@admin.register(ShiftException)
class ShiftExceptionAdmin(SimpleHistoryAdmin):
    list_display = ["instructor", "date", "is_working", "reason"]
    list_filter = ["is_working", "instructor"]
    date_hierarchy = "date"


@admin.register(InstructorDuty)
class InstructorDutyAdmin(SimpleHistoryAdmin):
    list_display = [
        "instructor",
        "slot",
        "kind",
        "group_session",
        "label",
        "valid_from",
        "valid_to",
    ]
    list_filter = ["kind", "instructor"]


@admin.register(InstructorBlock)
class InstructorBlockAdmin(SimpleHistoryAdmin):
    list_display = ["instructor", "date", "slot", "kind", "group_session", "label"]
    list_filter = ["kind", "instructor"]
    date_hierarchy = "date"
