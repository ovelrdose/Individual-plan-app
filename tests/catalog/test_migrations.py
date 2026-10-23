from importlib import import_module

import pytest
from django.apps import apps

from apps.catalog.models import Procedure, ProcedureKind

migration = import_module("apps.catalog.migrations.0006_group_abbreviation_synonyms")


@pytest.mark.django_db
def test_abbreviation_synonyms_added_once_keeping_admin_edits():
    leg = Procedure.objects.create(
        name="I can нога", card_label="I can нога", kind=ProcedureKind.LFK_GROUP,
        synonyms=["I can нога", "своя правка"],
    )  # fmt: skip
    hand = Procedure.objects.create(
        name="Эрго кисть", card_label="Эрго кисть", kind=ProcedureKind.LFK_GROUP,
        synonyms=["Эрго в/к"],
    )  # fmt: skip

    migration.add_synonyms(apps, None)
    migration.add_synonyms(apps, None)

    leg.refresh_from_db()
    hand.refresh_from_db()
    assert leg.synonyms == ["I can нога", "своя правка", "I can н/к"]
    assert hand.synonyms == ["Эрго в/к"]


sections_migration = import_module("apps.catalog.migrations.0008_initial_sections")


@pytest.mark.django_db
def test_initial_sections_bind_existing_groups():
    """Разделы «ЛФК», «ДС», «Бассейн» и привязка групп по виду (решение 82); «Бассейн» без
    группы и не-группы остаются без раздела. Повторный запуск ничего не меняет."""
    from apps.catalog.models import Section

    made = {
        kind: Procedure.objects.create(name=kind, card_label=kind, kind=kind)
        for kind in ("LFK_GROUP", "DS_GROUP", "POOL", "INDIVIDUAL")
    }
    generic = Procedure.objects.create(
        name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
    )
    Procedure.objects.update(section=None)  # как до миграции
    Section.objects.all().delete()

    sections_migration.add_sections(apps, None)
    sections_migration.add_sections(apps, None)

    assert [(s.name, s.behavior, s.order) for s in Section.objects.all()] == [
        ("ЛФК", "LFK_GROUP", 1),
        ("ДС", "DS_GROUP", 2),
        ("Бассейн", "POOL", 3),
    ]
    for kind, procedure in made.items():
        procedure.refresh_from_db()
        expected = None if kind == "INDIVIDUAL" else kind
        assert (procedure.section.behavior if procedure.section else None) == expected
    generic.refresh_from_db()
    assert generic.section is None
