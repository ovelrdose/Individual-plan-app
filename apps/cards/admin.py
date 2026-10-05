from django.contrib import admin

from .models import CardExport


@admin.register(CardExport)
class CardExportAdmin(admin.ModelAdmin):
    list_display = ["program", "user", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["program__full_name"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
