"""Экран «Групповые занятия»: вкладки групп и тренажёров, времена одной группы рядом."""

from datetime import time

import pytest
from django.urls import reverse

from apps.catalog.models import Equipment, GroupSession, Procedure, ProcedureKind, Section


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
    """Вкладка на раздел (решение 82): первым — «ЛФК», группы ДС — во вкладке «ДС»."""
    client.force_login(rehab)
    page = reverse("scheduling:group_schedule")

    html = client.get(page).content.decode()

    assert "Новая группа" in html and 'id="group-panel" class="d-none"' in html
    assert "Тренажёр-тест" not in html  # тренажёры — на своей вкладке
    table = html[html.index("<table") :]
    assert "ЛФК: колено" in table and "ДС: острая спина" not in table
    assert "занятий в день" not in table  # выключенное 12:00 у колена — в архиве
    assert "В архиве (1)" in table

    ds = Section.objects.get(name="ДС")
    html = client.get(page, {"section": ds.pk}).content.decode()
    table = html[html.index("<table") :]
    assert "ДС: острая спина" in table and "ЛФК: колено" not in table
    # Времена одной группы подряд, по порядку.
    assert table.index("8:15–8:45") < table.index("16:30–17:00")
    assert table.count("занятий в день: 2") == 1


def test_tabs_in_section_order_with_counts(client, rehab, groups):
    client.force_login(rehab)

    html = client.get(reverse("scheduling:group_schedule")).content.decode()

    tabs = html[html.index("nav-tabs") : html.index("Новый раздел")]
    assert tabs.index("ЛФК") < tabs.index("ДС") < tabs.index("Бассейн") < tabs.index("Тренажёры")
    # Число — действующие занятия: выключенное 12:00 у колена в архиве и не считается.
    assert 'aria-current="page">ЛФК <span class="badge text-bg-secondary">1</span>' in tabs
    assert 'ДС <span class="badge text-bg-secondary">2</span>' in tabs


def test_equipment_tab(client, rehab, groups):
    client.force_login(rehab)

    html = client.get(reverse("scheduling:group_schedule") + "?tab=equipment").content.decode()

    assert "Тренажёр-тест" in html
    assert "Добавить занятие группы" not in html
    assert 'aria-current="page">Тренажёры' in html


def test_edit_cancel_returns_fragment(client, rehab, groups):
    """«Отмена» в строке берёт фрагмент со страницы своей вкладки (hx-select)."""
    client.force_login(rehab)
    page = reverse("scheduling:group_schedule")

    assert 'id="group-sessions"' in client.get(page).content.decode()
    assert 'id="equipment-table"' in client.get(page + "?tab=equipment").content.decode()
