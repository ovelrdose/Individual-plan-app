"""Синонимы-сокращения групп из листов назначений: «I-can н/к», «Эрго в/к» (TZ.md, решение 78).

load_initial_catalog уже заведённые процедуры не меняет (их правят в админке), поэтому на
рабочую базу синонимы дописываются здесь — только недостающие, правки администратора остаются.
"""

from django.db import migrations

SYNONYMS = {
    "I can нога": ["I can н/к"],
    "I can рука": ["I can в/к"],
    "Эрго кисть": ["Эрго в/к"],
}


def add_synonyms(apps, schema_editor) -> None:
    Procedure = apps.get_model("catalog", "Procedure")
    for procedure in Procedure.objects.filter(name__in=SYNONYMS, department=None):
        missing = [s for s in SYNONYMS[procedure.name] if s not in procedure.synonyms]
        if missing:
            procedure.synonyms = [*procedure.synonyms, *missing]
            procedure.save(update_fields=["synonyms"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0005_rename_board_labels"),
    ]

    operations = [
        migrations.RunPython(add_synonyms, migrations.RunPython.noop),
    ]
