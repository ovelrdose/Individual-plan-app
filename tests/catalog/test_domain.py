from datetime import datetime, time

import pytest

from apps.catalog.domain import (
    add_minutes,
    equipment_starts,
    excel_time,
    minutes_between,
    normalize_name,
)


def test_add_minutes():
    assert add_minutes(time(9, 50), 30) == time(10, 20)
    with pytest.raises(ValueError):
        add_minutes(time(23, 50), 20)


def test_minutes_between():
    assert minutes_between(time(11, 40), time(13, 0)) == 80


class TestEquipmentStarts:
    def test_default_window(self):
        starts = equipment_starts(time(13, 0), time(15, 0), 15, 15)

        assert starts[0] == time(13, 0)
        assert starts[-1] == time(14, 45)
        assert len(starts) == 8

    def test_last_start_must_finish_in_window(self):
        starts = equipment_starts(time(13, 0), time(15, 0), 15, 30)

        assert starts[-1] == time(14, 30)

    def test_window_too_short(self):
        assert equipment_starts(time(13, 0), time(13, 10), 15, 15) == []

    @pytest.mark.parametrize(("step", "duration"), [(0, 15), (15, 0)])
    def test_invalid_step_or_duration(self, step, duration):
        with pytest.raises(ValueError):
            equipment_starts(time(13, 0), time(15, 0), step, duration)


class TestExcelTime:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.3819444, time(9, 10)),  # хвосты из файлов заказчика
            (0.381944444444, time(9, 10)),
            (0.5, time(12, 0)),
            (0, time(0, 0)),
            (time(9, 10, 30), time(9, 10)),
            (datetime(2026, 10, 5, 13, 40), time(13, 40)),
        ],
    )
    def test_values(self, value, expected):
        assert excel_time(value) == expected

    @pytest.mark.parametrize("value", ["9:10", 1.5, None])
    def test_rejects(self, value):
        with pytest.raises(ValueError):
            excel_time(value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ST – 150", "st150"),
        ("st-150", "st150"),
        ("Нейро - тренинг", "нейротренинг"),
        ("Вест. гимнастика", "вестгимнастика"),
        ("I can нога ", "icanнога"),
        ("Ёлка", "елка"),
    ],
)
def test_normalize_name(text, expected):
    assert normalize_name(text) == expected
