from django import forms
from django.contrib import admin
from django.contrib.postgres.forms import SimpleArrayField
from django.db.models import Q
from simple_history.admin import SimpleHistoryAdmin

from apps.accounts.access import departments_for, is_admin
from apps.accounts.admin_access import RightAdminMixin
from apps.accounts.models import Right
from apps.scheduling.services import (
    replan_for_equipment,
    replan_for_group,
    reset_choices,
    save_section,
)

from .models import SESSION_KINDS, Equipment, GroupSession, InstructorSlot, Procedure, Section


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
            "section",
            "synonyms",
            "default_duration_min",
            "place",
            "equipment",
            "group_choice",
            "evening_individual",
            "department",
            "is_active",
        ]


class GroupSessionInline(RightAdminMixin, admin.TabularInline):
    model = GroupSession
    extra = 0
    fields = ["start_time", "duration_min", "place", "is_active"]


@admin.register(Section)
class SectionAdmin(RightAdminMixin, SimpleHistoryAdmin):
    """Разделы «Групповых занятий» (решение 82); обычно их ведут на самом экране. Сохранение —
    тем же сервисом, что на экране: он блокирует раздел, и смена поведения не разойдётся с
    видом параллельно добавленной группы."""

    right = Right.GROUP_SCHEDULE
    list_display = ["name", "behavior", "order"]
    ordering = ["order", "name"]

    def save_model(self, request, obj, form, change):
        save_section(request.user, obj)


@admin.register(Procedure)
class ProcedureAdmin(RightAdminMixin, SimpleHistoryAdmin):
    form = ProcedureForm
    inlines = [GroupSessionInline]
    list_display = [
        "name",
        "card_label",
        "kind",
        "section",
        "default_duration_min",
        "place",
        "department",
        "is_active",
    ]
    list_filter = ["kind", "section", "is_active", "department"]
    search_fields = ["name", "card_label", "synonyms"]

    def get_readonly_fields(self, request, obj=None):
        # Группу выключают на экране «Групповые занятия»: там выключаются её занятия и
        # заканчивается ведение группы инструкторами (решение 82).
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.section_id is not None:
            readonly.append("is_active")
        return readonly

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

            def save() -> None:
                # Выключенное или удалённое занятие больше не выбранное время (решение 81):
                # сбрасываем с записью в журнал до удаления (иначе SET_NULL — без журнала).
                deleted = [f.instance.pk for f in formset.deleted_forms if f.instance.pk]
                reset_choices(deleted, request.user)
                super(ProcedureAdmin, self).save_formset(request, form, formset, change)
                inactive = GroupSession.objects.filter(procedure=form.instance, is_active=False)
                reset_choices(list(inactive.values_list("pk", flat=True)), request.user)

            replan_for_group(form.instance.pk, save=save)
        else:
            super().save_formset(request, form, formset, change)

    def get_inlines(self, request, obj):
        # Расписание есть только у групп ЛФК и бассейна.
        if obj is None or obj.kind not in SESSION_KINDS or obj.group_choice:
            return []
        return super().get_inlines(request, obj)


@admin.register(GroupSession)
class GroupSessionAdmin(RightAdminMixin, SimpleHistoryAdmin):
    list_display = ["start_time", "procedure", "duration_min", "place", "is_active"]
    list_filter = ["procedure__kind", "is_active"]
    search_fields = ["procedure__name"]

    def save_model(self, request, obj, form, change):
        # Занятие могли перенести в другую группу: пересобираем программы обеих.
        old = GroupSession.objects.filter(pk=obj.pk).values_list("procedure_id", flat=True)
        groups = {obj.procedure_id, *old}

        def save() -> None:
            super(GroupSessionAdmin, self).save_model(request, obj, form, change)
            # Выключили или увели в другую группу — выбор этого времени сбрасывается (решение 81).
            if not obj.is_active or set(old) - {obj.procedure_id}:
                reset_choices([obj.pk], request.user)

        replan_for_group(*groups, save=save)

    def delete_model(self, request, obj):
        self._delete(request, [obj])

    def delete_queryset(self, request, queryset):
        self._delete(request, list(queryset))

    def _delete(self, request, sessions: list[GroupSession]) -> None:
        """Удаление занятия пересобирает расписание его группы; выбор этого времени
        сбрасывается с записью в журнал (решение 81)."""
        ids = [item.pk for item in sessions]

        def delete() -> None:
            reset_choices(ids, request.user)
            for item in sessions:
                item._history_user = request.user
                item.delete()

        replan_for_group(*{item.procedure_id for item in sessions}, save=delete)

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
class EquipmentAdmin(RightAdminMixin, SimpleHistoryAdmin):
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
class InstructorSlotAdmin(RightAdminMixin, SimpleHistoryAdmin):
    # Сетка слотов задаёт шахматку — её меняет только суперпользователь.
    read_only = True
    list_display = ["__str__", "start", "end", "is_evening"]
    list_filter = ["is_evening"]
