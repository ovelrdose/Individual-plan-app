"""Время группы из листа (preferred_time) — ссылкой на занятие группы (решение 81)."""

from django.db import migrations


def copy_preferred_time(apps, schema_editor) -> None:
    """Действующее занятие «действующей группы» в это время: для «Бассейн» без группы — группы
    бассейна, иначе — самой процедуры. Занятия нет — выбора нет, время подберётся само."""
    Prescription = apps.get_model("programs", "Prescription")
    GroupSession = apps.get_model("catalog", "GroupSession")
    items = Prescription.objects.filter(preferred_time__isnull=False).select_related("procedure")
    for item in items:
        procedure = item.procedure
        if procedure is None:
            continue
        group_id = item.pool_group_id if procedure.group_choice else procedure.pk
        session = GroupSession.objects.filter(
            procedure_id=group_id, is_active=True, start_time=item.preferred_time
        ).first()
        if session is not None:
            item.group_session = session
            item.save(update_fields=["group_session"])


def copy_back(apps, schema_editor) -> None:
    Prescription = apps.get_model("programs", "Prescription")
    for item in Prescription.objects.filter(group_session__isnull=False).select_related(
        "group_session"
    ):
        item.preferred_time = item.group_session.start_time
        item.save(update_fields=["preferred_time"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0006_group_abbreviation_synonyms"),
        ("programs", "0011_prescription_group_session"),
    ]

    operations = [migrations.RunPython(copy_preferred_time, copy_back)]
