"""Админка Django — только у суперпользователя (TZ.md, решение 83): у остальных снимаем
``is_staff``. Дальше его держит сигнал ``staff_follows_superuser``."""

from django.db import migrations


def drop_staff(apps, schema_editor) -> None:
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_superuser=False, is_staff=True).update(is_staff=False)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_user_rights"),
    ]

    operations = [
        migrations.RunPython(drop_staff, migrations.RunPython.noop),
    ]
