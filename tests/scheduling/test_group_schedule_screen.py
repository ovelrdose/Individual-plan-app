"""Экран «Групповые занятия»: вкладки групп и тренажёров, времена одной группы рядом."""

from datetime import time

import pytest
from django.urls import reverse

from apps.catalog.models import Equipment, GroupSession, Procedure, ProcedureKind


@pytest.fixture
def groups(db) -> None:
    spine = Procedure.objects.create(
        name="ДС: острая спина", card_label="ДС: острая спина", kind=ProcedureKind.DS_GROUP
    )
    knee = Procedure.objects.create(
        name="ЛФК: колено", card_label="ЛФК: колено", kind=ProcedureKind.LFK_GROUP
    )
    for group, start in ((spine, time(8, 15)), (knee, time(10, 0)), (spine, time(16, 30))):
        GroupSession.objects.create(procedure=group, start_time=start)
    GroupSession.objects.create(procedure=knee, start_time=time(12, 0), is_active=False)
    Equipment.objects.create(name="Тренажёр-тест")


def test_groups_tab(client, rehab, groups):
    client.force_login(rehab)

    html = client.get(reverse("scheduling:group_schedule")).content.decode()

    assert "Добавить занятие группы" in html
    assert "Тренажёр-тест" not in html  # тренажёры — на своей вкладке
    html = html[html.index("<table") :]  # названия групп есть и в списке формы
    # ЛФК раньше ДС, времена одной группы подряд, хотя 10:00 между 8:15 и 16:30.
    assert html.index("ЛФК: колено") < html.index("ДС: острая спина")
    assert html.index("8:15–8:45") < html.index("16:30–17:00")
    assert html.count("занятий в день: 2") == 1  # выключенное 12:00 у колена не считается


def test_equipment_tab(client, rehab, groups):
    client.force_login(rehab)

    html = client.get(reverse("scheduling:group_schedule") + "?tab=equipment").content.decode()

    assert "Тренажёр-тест" in html
    assert "Добавить занятие группы" not in html
    assert 'aria-current="page">\n        Тренажёры' in html


def test_edit_cancel_returns_fragment(client, rehab, groups):
    """«Отмена» в строке берёт фрагмент со страницы своей вкладки (hx-select)."""
    client.force_login(rehab)
    page = reverse("scheduling:group_schedule")

    assert 'id="group-sessions"' in client.get(page).content.decode()
    assert 'id="equipment-table"' in client.get(page + "?tab=equipment").content.decode()
