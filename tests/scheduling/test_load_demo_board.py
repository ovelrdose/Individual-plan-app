"""Демо-шахматка с картинки (manage.py load_demo_board): разбор по пациентам и защита.

Сама сборка требует настоящих инструкторов (load_board_instructors) — в тестах их нет; её
проверяет запуск на рабочей базе.
"""

from itertools import pairwise

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.scheduling.management.commands.load_demo_board import EQUIPMENT, INDIVIDUAL, people


def test_namesakes_only_where_rules_require():
    persons = people()
    by_name: dict[tuple, int] = {}
    for person in persons:
        key = (person.room, person.surname)
        by_name[key] = by_name.get(key, 0) + 1

    # Маничев: индивидуальное 13:00–13:30 и Имитрон в 13:15; Кутькин и Дубровская — по три
    # индивидуальных. Остальные повторы — один пациент.
    assert {key for key, count in by_name.items() if count > 1} == {
        ("11", "Маничев"),
        ("14", "Кутькин"),
        ("9", "Дубровская"),
    }
    assert len(persons) == 38
    assert sum(len(p.individual) for p in persons) == len(INDIVIDUAL)
    assert sum(len(p.equipment) for p in persons) == len(EQUIPMENT)
    for person in persons:
        assert len(person.individual) <= 2
        spans = sorted(person.busy)
        assert all(a[1] <= b[0] for a, b in pairwise(spans))


def test_only_in_debug(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="Только для разработки"):
        call_command("load_demo_board", "--yes")


def test_only_today_or_tomorrow(settings, db):
    settings.DEBUG = True
    with pytest.raises(CommandError, match="только на сегодня"):
        call_command("load_demo_board", "--yes", "--date", "2020-01-01")
