from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Prescription, Program


class ReadOnlyMixin:
    """Только просмотр и история. Все правки — на экранах системы: только там работают
    проверки курса и пересчёт даты окончания."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PrescriptionInline(ReadOnlyMixin, admin.TabularInline):
    model = Prescription
    extra = 0


@admin.register(Program)
class ProgramAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    inlines = [PrescriptionInline]
    list_display = ["full_name", "room", "shrm", "start_date", "end_date", "source", "department"]
    list_filter = ["department", "shrm", "source"]
    search_fields = ["full_name", "history_number"]
    date_hierarchy = "start_date"
