"""Карточка инструктора переехала из «Справочников» в раздел «Инструкторы» (решение 87): её
правят по праву «Инструкторы». Кто правил её по праву «Справочники», получает и это право —
доступ ни у кого не пропадает."""

import django.contrib.postgres.fields
from django.db import migrations, models


def grant_staff(apps, schema_editor) -> None:
    User = apps.get_model("accounts", "User")
    for user in User.objects.filter(rights__contains=["catalog"]).exclude(
        rights__contains=["staff"]
    ):
        user.rights = [*user.rights, "staff"]
        user.save(update_fields=["rights"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_staff_only_superusers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="rights",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(
                    choices=[
                        (
                            "programs_edit",
                            "Программы: создавать и править, загружать листы назначений",
                        ),
                        ("programs_delete", "Программы: удалять"),
                        ("schedule", "Расписание пациентов: индивидуальные, группы и тренажёры"),
                        ("group_schedule", "Групповые занятия: расписание групп"),
                        ("staff", "Инструкторы: состав, пары, смены и распорядок"),
                        ("catalog", "Справочники: процедуры, тренажёры"),
                        ("users", "Пользователи и отделения (видит все отделения)"),
                    ],
                    max_length=30,
                ),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.RunPython(grant_staff, migrations.RunPython.noop),
    ]
