"""Текущие пациенты по сводкам (manage.py load_current_patients): раскладка с картинки, ШРМ по
длине курса, сопоставление со сводкой и защита.

Сама сборка требует настоящих инструкторов (load_board_instructors) — в тестах их нет; её
проверяет запуск на рабочей базе (15.10.2026: расписание совпало с картинкой клетка в клетку).
"""

from collections import defaultdict
from datetime import date
from itertools import combinations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.exchange.patient_summary import SummaryPatient
from apps.scheduling.management.commands.load_current_patients import (
    DUTIES,
    EQUIPMENT,
    INDIVIDUAL,
    login_for,
    match,
    minutes,
    shrm_for,
)


def spans() -> dict[str, list[tuple[int, int, bool]]]:
    result: dict[str, list[tuple[int, int, bool]]] = defaultdict(list)
    for at, _column, _room, surname, _note in INDIVIDUAL:
        result[surname].append((minutes(at), minutes(at) + 30, True))
    for at, _item, _room, surname, _note in EQUIPMENT:
        result[surname].append((minutes(at), minutes(at) + 15, False))
    return result


def patient(surname: str, room: str = "1") -> SummaryPatient:
    return SummaryPatient(
        full_name=f"{surname} А.А.",
        room=room,
        department="",
        sex="М",
        age=50,
        history_number="",
        admitted=date(2026, 10, 10),
        discharge=None,
        doctor="Докторова А.А.",
    )


def everyone() -> list[SummaryPatient]:
    return [patient(name) for name in sorted(spans())]


class TestLayout:
    def test_patient_rules(self):
        """Не больше двух индивидуальных, не два места сразу, без двух индивидуальных подряд."""
        for surname, items in spans().items():
            assert sum(1 for *_x, individual in items if individual) <= 2, surname
            for a, b in combinations(sorted(items), 2):
                assert a[1] <= b[0], surname
                if a[2] and b[2]:
                    assert b[0] - a[1] > 10, surname

    def test_cells_free_of_duties(self):
        duties = {(column, at) for column, at, *_rest in DUTIES}
        cells = [(column, at) for at, column, *_rest in INDIVIDUAL]

        assert not duties & set(cells)
        assert len(cells) == len(set(cells)), "в ячейке по одному пациенту, как на картинке"

    def test_equipment_capacity(self):
        per_time: dict[tuple[str, str], int] = defaultdict(int)
        for at, item, *_rest in EQUIPMENT:
            per_time[(at, item)] += 1

        assert max(per_time.values()) <= 2


class TestHelpers:
    @pytest.mark.parametrize(
        ("days", "shrm"), [(10, 3), (11, 3), (12, 4), (15, 4), (16, 5), (21, 5), (28, 5)]
    )
    def test_shrm_by_course_length(self, days, shrm):
        assert shrm_for(days) == shrm

    def test_login(self):
        assert login_for("Щукина Ё.Ж.") == "shchukina"
        assert login_for("Объездова А.А.") == "obezdova"


class TestMatch:
    def test_layout_to_patients(self):
        persons = {p.patient.surname: p for p in match([*everyone(), patient("Безрасписанов")])}

        board_only = persons["Безрасписанов"]
        assert board_only.individual == [] and board_only.equipment == []
        assert sum(len(p.individual) for p in persons.values()) == len(INDIVIDUAL)
        assert sum(len(p.equipment) for p in persons.values()) == len(EQUIPMENT)

    def test_room_from_layout(self):
        at, column, room, surname, _note = INDIVIDUAL[0]
        others = [p for p in everyone() if p.surname != surname]

        persons = {p.patient.surname: p for p in match([*others, patient(surname, room="ПИТ")])}

        assert persons[surname].room == room
        assert persons[surname].individual[0][:2] == (at, column)

    def test_room_kept_when_layout_has_none(self):
        surname = next(s for _at, _i, room, s, _n in EQUIPMENT if not room)
        others = [p for p in everyone() if p.surname != surname]

        persons = {p.patient.surname: p for p in match([*others, patient(surname, room="2")])}

        assert persons[surname].room == "2"

    def test_missing_in_summary(self):
        surname = INDIVIDUAL[0][3]

        with pytest.raises(CommandError, match=f"Нет в сводках: .*{surname}"):
            match([p for p in everyone() if p.surname != surname])

    def test_namesakes_rejected(self):
        with pytest.raises(CommandError, match="однофамильцы"):
            match([*everyone(), patient(INDIVIDUAL[0][3], room="9")])


class TestCommand:
    def test_only_today_or_next_weekday(self, db):
        with pytest.raises(CommandError, match="только на сегодня"):
            call_command("load_current_patients", "--yes", "--date", "2020-01-01")

    def test_unreadable_summary(self, db, tmp_path):
        path = tmp_path / "сводка.docx"
        path.write_bytes(b"not a docx")

        with pytest.raises(CommandError, match=r"сводка\.docx: Файл не читается"):
            call_command("load_current_patients", "--yes", "--summary", str(path))
