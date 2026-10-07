from datetime import date

import pytest

from apps.programs.domain import (
    course_dates,
    course_end,
    fold,
    is_absent,
    is_therapy_day,
    room_label,
    room_sort_key,
    sex_from_name,
    surname,
    therapy_dates,
)


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (11, date(2026, 10, 15)),
        (15, date(2026, 10, 19)),
        (20, date(2026, 10, 24)),
        (1, date(2026, 10, 5)),
    ],
)
def test_course_end_counts_first_day(days, expected):
    assert course_end(date(2026, 10, 5), days) == expected


def test_course_end_across_year_and_leap_february():
    assert course_end(date(2026, 12, 25), 11) == date(2027, 1, 4)
    assert course_end(date(2028, 2, 20), 15) == date(2028, 3, 5)


def test_course_end_rejects_zero():
    with pytest.raises(ValueError):
        course_end(date(2026, 10, 5), 0)


def test_course_dates():
    assert course_dates(date(2026, 10, 5), date(2026, 10, 7)) == [
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    ]
    assert course_dates(date(2026, 10, 5), date(2026, 10, 4)) == []


def test_room_sort_key():
    rooms = ["12", "5а", "9", "5", "Б/н", "10"]

    assert sorted(rooms, key=room_sort_key) == ["5", "5а", "9", "10", "12", "Б/н"]


def test_fold_and_surname():
    assert fold("СЕМЁНОВ") == "семенов"
    assert surname("  Тестов Тест Тестович ") == "Тестов"
    assert surname("") == ""


@pytest.mark.parametrize(
    ("full_name", "sex"),
    [
        ("Тестов Тест Иванович", "М"),
        ("Тестов Тест Ильич", "М"),
        ("Тестов Тест Кузьмич", "М"),
        ("Тестова Анна Ивановна", "Ж"),
        ("Тестова Анна Ильинична", "Ж"),
        ("Тестова Анна Никитична", "Ж"),
        ("ТЕСТОВА АННА СЕРГЕЕВНА", "Ж"),
        ("Тестов Мамед Али оглы", "М"),
        ("Тестова Лейла Али кызы", "Ж"),
        ("Тестов Тест", ""),
        ("Test Test Test", ""),
        ("", ""),
    ],
)
def test_sex_from_name(full_name, sex):
    assert sex_from_name(full_name) == sex


def test_therapy_dates_skip_admission_and_discharge():
    """В день поступления и в день выписки занятий нет (TZ.md, FR-SCH-1)."""
    start, end = date(2026, 10, 5), date(2026, 10, 15)

    days = therapy_dates(start, end)

    assert (days[0], days[-1], len(days)) == (date(2026, 10, 6), date(2026, 10, 14), 9)
    assert not is_therapy_day(start, end, start)
    assert not is_therapy_day(start, end, end)
    assert is_therapy_day(start, end, date(2026, 10, 6))


@pytest.mark.parametrize("days", [1, 2])
def test_short_course_has_no_therapy_days(days):
    start = date(2026, 10, 5)
    assert therapy_dates(start, course_end(start, days)) == []


class TestAbsence:
    """Дни после выбытия — не дни занятий; после восстановления — снова дни занятий (FR-PRG-9)."""

    start, end = date(2026, 10, 5), date(2026, 10, 19)

    def test_open_gap(self):
        gaps = [(date(2026, 10, 8), None)]
        assert is_absent(gaps, date(2026, 10, 8)) and is_absent(gaps, date(2026, 10, 30))
        assert not is_absent(gaps, date(2026, 10, 7))
        assert therapy_dates(self.start, self.end, gaps) == [date(2026, 10, 6), date(2026, 10, 7)]

    def test_closed_gap(self):
        gaps = [(date(2026, 10, 8), date(2026, 10, 12))]
        days = therapy_dates(self.start, self.end, gaps)
        assert date(2026, 10, 11) not in days and date(2026, 10, 12) in days
        assert is_therapy_day(self.start, self.end, date(2026, 10, 7), gaps)
        assert not is_therapy_day(self.start, self.end, date(2026, 10, 9), gaps)


@pytest.mark.parametrize(
    ("room", "label"), [("9", "9п"), ("12", "12п"), ("5а", "5а"), (" 5а ", "5а")]
)
def test_room_label(room, label):
    """«Nп», а вариант палаты — как на бумаге: «5а Плиев», не «5ап»."""
    assert room_label(room) == label
