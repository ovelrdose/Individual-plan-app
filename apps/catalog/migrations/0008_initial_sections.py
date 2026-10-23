"""Стартовые разделы «Групповых занятий» — «ЛФК», «ДС», «Бассейн» — и привязка к ним
существующих групп по виду (TZ.md, FR-CAT-1a, решение 82). «Бассейн» без группы
(``group_choice``) — не группа, раздела у него нет."""

from django.db import migrations

SECTIONS = (
    ("ЛФК", "LFK_GROUP"),
    ("ДС", "DS_GROUP"),
    ("Бассейн", "POOL"),
)


def add_sections(apps, schema_editor) -> None:
    Section = apps.get_model("catalog", "Section")
    Procedure = apps.get_model("catalog", "Procedure")
    for order, (name, behavior) in enumerate(SECTIONS, start=1):
        section = Section.objects.filter(behavior=behavior).order_by("order", "pk").first()
        if section is None:
            section = Section.objects.create(name=name, behavior=behavior, order=order)
        Procedure.objects.filter(kind=behavior, group_choice=False, section=None).update(
            section=section
        )


def remove_sections(apps, schema_editor) -> None:
    Procedure = apps.get_model("catalog", "Procedure")
    Section = apps.get_model("catalog", "Section")
    Procedure.objects.update(section=None)
    Section.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0007_section"),
    ]

    operations = [
        migrations.RunPython(add_sections, remove_sections),
    ]
