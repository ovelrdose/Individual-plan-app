from datetime import date

import pytest

from apps.programs.domain import (
    course_dates,
    course_end,
    fold,
    room_sort_key,
    sex_from_name,
    surname,
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
