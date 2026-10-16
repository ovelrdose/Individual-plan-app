from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from apps.accounts.admin_access import RightAdminMixin
from apps.accounts.models import Right

from .models import Department


@admin.register(Department)
class DepartmentAdmin(RightAdminMixin, SimpleHistoryAdmin):
    right = Right.USERS

    list_display = ["name", "code", "max_individual_per_day", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "code"]
