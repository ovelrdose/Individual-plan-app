from django import forms
from django.contrib import admin
from django.contrib.postgres.forms import SimpleArrayField
from django.db.models import Q
from simple_history.admin import SimpleHistoryAdmin

from apps.accounts.access import departments_for, is_admin
from apps.scheduling.services import replan_for_equipment, replan_for_group

from .models import SESSION_KINDS, Equipment, GroupSession, InstructorSlot, Procedure


class SynonymsField(SimpleArrayField):
    """Синонимы — по одному на строку: в названиях бывают запятые."""

    def __init__(self, **kwargs):
        kwargs.setdefault("widget", forms.Textarea(attrs={"rows": 4}))
        super().__init__(forms.CharField(max_length=100), delimiter="\n", **kwargs)

    def prepare_value(self, value):
        if isinstance(value, list):
            return "\n".join(value)
        return value

    def to_python(self, value):
        value = "\n".join(line.strip() for line in (value or "").splitlines() if line.strip())
        return super().to_python(value)


class ProcedureForm(forms.ModelForm):
    synonyms = SynonymsField(required=False, label="Синонимы", help_text="По одному на строку.")

    class Meta:
        model = Procedure
        fields = [
            "name",
            "card_label",
            "kind",
            "synonyms",
            "default_duration_min",
            "place",
            "equipment",
            "group_choice",
            "evening_individual",
            "department",
            "is_active",
        ]


class GroupSessionInline(admin.TabularInline):
    model = GroupSession
    extra = 0
    fields = ["start_time", "duration_min", "place", "is_active"]


@admin.register(Procedure)
class ProcedureAdmin(SimpleHistoryAdmin):
    form = ProcedureForm
    inlines = [GroupSessionInline]
    list_display = [
        "name",
        "card_label",
        "kind",
        "default_duration_min",
        "place",
        "department",
        "is_active",
    ]
    list_filter = ["kind", "is_active", "department"]
    search_fields = ["name", "card_label", "synonyms"]

    def get_queryset(self, request):
        # Специалист ФР видит общие процедуры и процедуры своих отделений.
        queryset = super().get_queryset(request)
        if is_admin(request.user):
            return queryset
        return queryset.filter(
            Q(department__isnull=True) | Q(department__in=departments_for(request.user))
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "department" and not is_admin(request.user):
            kwargs["queryset"] = departments_for(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_formset(self, request, form, formset, change):
        # Расписание программ следует за расписанием групп (TZ.md, FR-SCH-18). Программы
        # блокируются раньше, чем меняются занятия группы, — единый порядок блокировок.
        if formset.model is GroupSession and formset.has_changed():
            replan_for_group(
                form.instance.pk,
                save=lambda: super(ProcedureAdmin, self).save_formset(
                    request, form, formset, change
                ),
            )
        else:
            super().save_formset(request, form, formset, change)

    def get_inlines(self, request, obj):
        # Расписание есть только у групп ЛФК и бассейна.
        if obj is None or obj.kind not in SESSION_KINDS or obj.group_choice:
            return []
        return super().get_inlines(request, obj)


@admin.register(GroupSession)
class GroupSessionAdmin(SimpleHistoryAdmin):
    list_display = ["start_time", "procedure", "duration_min", "place", "is_active"]
    list_filter = ["procedure__kind", "is_active"]
    search_fields = ["procedure__name"]

    def save_model(self, request, obj, form, change):
        # Занятие могли перенести в другую группу: пересобираем программы обеих.
        old = GroupSession.objects.filter(pk=obj.pk).values_list("procedure_id", flat=True)
        groups = {obj.procedure_id, *old}
        replan_for_group(
            *groups,
            save=lambda: super(GroupSessionAdmin, self).save_model(request, obj, form, change),
        )

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("procedure")
        if is_admin(request.user):
            return queryset
        return queryset.filter(
            Q(procedure__department__isnull=True)
            | Q(procedure__department__in=departments_for(request.user))
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "procedure":
            kwargs["queryset"] = (
                ProcedureAdmin(Procedure, self.admin_site)
                .get_queryset(request)
                .filter(kind__in=SESSION_KINDS, group_choice=False)
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Equipment)
class EquipmentAdmin(SimpleHistoryAdmin):
    list_display = [
        "name",
        "window_start",
        "window_end",
        "step_min",
        "duration_min",
        "capacity",
        "is_active",
    ]
    list_filter = ["is_active"]

    def save_model(self, request, obj, form, change):
        # Сначала блокируются программы с тренажёром, потом меняется строка тренажёра.
        replan_for_equipment(
            obj.pk, save=lambda: super(EquipmentAdmin, self).save_model(request, obj, form, change)
        )


@admin.register(InstructorSlot)
class InstructorSlotAdmin(SimpleHistoryAdmin):
    list_display = ["__str__", "start", "end", "is_evening"]
    list_filter = ["is_evening"]
