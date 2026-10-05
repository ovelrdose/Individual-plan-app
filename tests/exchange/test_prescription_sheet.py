from datetime import date
from io import BytesIO

import pytest

from apps.exchange.prescription_sheet import Sheet, SheetError, parse_sheet
from tests.sheets import make_sheet

GROUPS = "Групповые занятия ЛФК\n- Эрго общая\n– I can нога\n30 мин 2 р/д е/д"


class TestHeader:
    def test_fields(self):
        sheet = parse_sheet(
            make_sheet(fio="Тестова Анна Сергеевна", ib="77", shrm=5, dx="I69.4", room="5а")
        )

        assert (sheet.full_name, sheet.history_number, sheet.shrm, sheet.diagnosis, sheet.room) == (
            "Тестова Анна Сергеевна",
            "77",
            5,
            "I69.4",
            "5а",
        )
        assert sheet.warnings == []

    def test_different_values_in_repeated_header(self):
        sheet = parse_sheet(make_sheet(ib="5402", second_header={"ib": "513402", "room": "7"}))

        assert sheet.history_number == "5402"
        assert sheet.room == "5а"
        assert "«ИБ»: 5402, 513402" in sheet.warnings[0]
        assert "«палата»: 5а, 7" in sheet.warnings[1]

    def test_same_repeated_header_is_not_a_warning(self):
        assert parse_sheet(make_sheet(second_header={})).warnings == []

    def test_optional_fields(self):
        sheet = parse_sheet(
            make_sheet(header="ФИО  Тестов Тест Тестович      ШРМ 3          Палата № 9")
        )

        assert (sheet.full_name, sheet.history_number, sheet.diagnosis) == (
            "Тестов Тест Тестович",
            "",
            "",
        )

    def test_missing_required_fields(self):
        with pytest.raises(SheetError, match="ШРМ, палата"):
            parse_sheet(make_sheet(header="ФИО  Тестов Тест Тестович      ИБ №5"))

    def test_no_header(self):
        with pytest.raises(SheetError, match="ФИО"):
            parse_sheet(make_sheet(header="Просто текст"))


class TestTable:
    def test_groups_become_separate_rows(self):
        sheet = parse_sheet(make_sheet(rows=[("02.10.26", GROUPS, "")]))

        assert [(r.raw_text, r.match_text, r.duration_min, r.per_day) for r in sheet.rows] == [
            ("Групповые занятия ЛФК: Эрго общая", "Эрго общая", 30, 1),
            ("Групповые занятия ЛФК: I can нога", "I can нога", 30, 1),
        ]
        # «2 р/д» на две группы — каждая раз в день, предупреждать не о чем (решение 42).
        assert sheet.warnings == []

    def test_group_list_frequency_not_matching_groups_is_a_warning(self):
        text = "Групповые занятия ЛФК\n- Эрго общая\n- I can нога\n- Нейро-тренинг\n30 мин 2 р/д"
        sheet = parse_sheet(make_sheet(rows=[("02.10.26", text, "")]))

        assert [r.per_day for r in sheet.rows] == [1, 1, 1]
        (warning,) = sheet.warnings
        assert "2 р/д на 3 группы — каждая группа поставлена 1 раз в день" in warning

    @pytest.mark.parametrize(
        ("text", "duration", "per_day"),
        [
            ("Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", 30, 1),
            ("Степпер 15 минут 1 р/д, ежедневно", 15, 1),
            ("Thera Trainer Tigo 15 мин 2 р/д е/д", 15, 2),
            ("Массаж", None, 1),
        ],
    )
    def test_duration_and_frequency(self, text, duration, per_day):
        (row,) = parse_sheet(make_sheet(rows=[("02.10.26", text, "")])).rows

        assert (row.raw_text, row.duration_min, row.per_day) == (text, duration, per_day)

    def test_dates(self):
        sheet = parse_sheet(
            make_sheet(
                rows=[
                    ("03.10.26", "Имитрон 15 мин", ""),
                    ("02.10.2026", "Степпер 15 мин", "09.10.26"),
                    ("", "Алмаг 20 мин", "31.02.26"),  # несуществующая дата — игнорируется
                ]
            )
        )

        assert [(r.prescribed_on, r.cancel_date) for r in sheet.rows] == [
            (date(2026, 10, 3), None),
            (date(2026, 10, 2), date(2026, 10, 9)),
            (None, None),
        ]
        assert sheet.start_date == date(2026, 10, 2)

    def test_empty_rows_are_skipped(self):
        sheet = parse_sheet(
            make_sheet(rows=[("", "", ""), ("02.10.26", "Имитрон", ""), ("", " \n ", "")])
        )

        assert [r.raw_text for r in sheet.rows] == ["Имитрон"]

    def test_no_physio_rows_is_a_warning(self):
        sheet = parse_sheet(make_sheet(rows=[]))

        assert sheet.rows == []
        assert sheet.start_date is None
        assert "нет назначений" in sheet.warnings[0]

    def test_consultations(self):
        sheet = parse_sheet(
            make_sheet(
                rows=[("02.10.26", "Имитрон", "")],
                diagnostics=[
                    "Консультация терапевта, медицинского психолога, медицинского логопеда, "
                    "эрготерапевта и физического терапевта.",
                    "Гликемический профиль на 03.10.26",
                ],
            )
        )
        consultations = [r for r in sheet.rows if r.consultation]

        assert [r.match_text for r in consultations] == [
            "терапевта",
            "медицинского психолога",
            "медицинского логопеда",
            "эрготерапевта",
            "физического терапевта",
        ]
        assert consultations[1].raw_text == "Консультация медицинского психолога"
        assert sheet.rows[0].raw_text == "Имитрон", "физиотерапия идёт раньше консультаций"

    def test_no_physio_table(self):
        with pytest.raises(SheetError, match="Физиотерапия"):
            parse_sheet(make_sheet(with_table=False))

    def test_not_a_docx(self):
        with pytest.raises(SheetError, match=r"\.docx"):
            parse_sheet(BytesIO(b"definitely not a zip"))


def test_session_roundtrip():
    sheet = parse_sheet(
        make_sheet(
            rows=[("02.10.26", "Имитрон", "05.10.26")], diagnostics=["Консультация психолога"]
        )
    )

    restored = Sheet.from_dict(sheet.to_dict())

    assert restored == sheet


def test_zip_with_broken_document():
    import zipfile

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<broken")
    buffer.seek(0)

    with pytest.raises(SheetError):
        parse_sheet(buffer)
