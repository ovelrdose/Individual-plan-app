from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import LoginFailure, Membership, User


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 1


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [MembershipInline]
    list_display = [
        "username",
        "short_name",
        "last_name",
        "first_name",
        "is_active",
        "is_superuser",
    ]
    search_fields = ["username", "short_name", "last_name", "first_name"]
    fieldsets = (
        *BaseUserAdmin.fieldsets[:1],
        ("Личные данные", {"fields": ("last_name", "first_name", "short_name", "email")}),
        *BaseUserAdmin.fieldsets[2:],
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "short_name", "password1", "password2"),
            },
        ),
    )


@admin.register(LoginFailure)
class LoginFailureAdmin(admin.ModelAdmin):
    list_display = ["username", "created_at"]
    search_fields = ["username"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
