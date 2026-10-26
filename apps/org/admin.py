from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Department


@admin.register(Department)
class DepartmentAdmin(SimpleHistoryAdmin):
    list_display = ["name", "code", "max_individual_per_day", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "code"]
