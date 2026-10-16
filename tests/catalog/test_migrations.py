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
