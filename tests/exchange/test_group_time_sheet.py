"""Время группы из листа назначений (TZ.md, решение 81, FR-IMP-15): «в 15:00» — выбор
занятия, строка без времени выбор не трогает, неизвестное время — предупреждение.

Листы — из tests/sheets.py (вымышленные данные). Справочник — расписания групп заказчика:
у «ДС: коленный сустав» занятия 10:30 и 15:00.
"""

from datetime import date, time

import pytest

from apps.catalog.models import GroupSession, Procedure
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import (
    apply_sheet_update,
    import_prescription_sheet,
    preview_sheet_update,
)
from apps.scheduling import board_days
from apps.scheduling.services import choose_group_time, choose_pool_group
from tests.sheets import make_sheet

pytestmark = pytest.mark.usefixtures("full_catalog")

TUE = date(2026, 10, 6)
KNEE = "Групповые занятия ЛФК:\nКолено{} – 179 каб.\n30 мин 1 р/д"


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


def sheet(text: str):
    return parse_sheet(make_sheet(rows=[("05.10.26", text, "")]))


def do_import(doctor, department, text: str):
    return import_prescription_sheet(doctor, department, sheet(text), attending_doctor=doctor)


def knee_session(start: time) -> GroupSession:
    return GroupSession.objects.get(procedure__name="ДС: коленный сустав", start_time=start)


def starts(item) -> set[time]:
    return set(item.bookings.values_list("start", flat=True))


class TestImport:
    def test_without_time_no_choice(self, doctor, department):
        program = do_import(doctor, department, KNEE.format(""))

        item = program.prescriptions.get()
        assert item.group_session is None
        assert not any("нет занятия" in w for w in program.import_warnings)

    def test_time_of_disabled_session_is_not_chosen(self, doctor, department):
        late = knee_session(time(15, 0))
        late.is_active = False
        late.save()

        program = do_import(doctor, department, KNEE.format(" в 15:00"))

        item = program.prescriptions.get()
        assert item.group_session is None
        assert starts(item) == {time(10, 30)}
        assert any("нет занятия в 15:00" in w and "10:30" in w for w in program.import_warnings)

    def test_chosen_time_is_whole_course(self, doctor, department):
        program = do_import(doctor, department, KNEE.format(" в 15:00"))

        item = program.prescriptions.get()
        therapy = {day for day in program.course_dates() if program.is_therapy_day(day)}
        assert set(item.bookings.values_list("date", flat=True)) == therapy
        assert starts(item) == {time(15, 0)}


class TestUpdate:
    def test_manual_choice_kept_by_sheet_without_time(self, doctor, rehab, department):
        program = do_import(doctor, department, KNEE.format(""))
        item = program.prescriptions.get()
        choose_group_time(rehab, item, knee_session(time(15, 0)))

        preview = preview_sheet_update(doctor, program, sheet(KNEE.format("")))
        apply_sheet_update(doctor, program, sheet(KNEE.format("")))

        assert [line.state for line in preview.lines] == ["same"]
        item.refresh_from_db()
        assert item.group_session == knee_session(time(15, 0))
        assert starts(item) == {time(15, 0)}

    def test_same_sheet_time_overrides_manual_choice(self, doctor, rehab, department):
        """Лист «в 15:00», специалист выбрал 10:30, загружен тот же лист снова: в
        предпросмотре видно «было 10:30 → стало 15:00» — время из листа снова главное."""
        program = do_import(doctor, department, KNEE.format(" в 15:00"))
        item = program.prescriptions.get()
        choose_group_time(rehab, item, knee_session(time(10, 30)))

        preview = preview_sheet_update(doctor, program, sheet(KNEE.format(" в 15:00")))

        line = preview.lines[0]
        assert line.state == "changed"
        assert "в 10:30" in line.old and "в 15:00" in line.new

    def test_pool_time_needs_pool_group(self, doctor, rehab, department):
        """«Бассейн в 9:00» без группы бассейна — время учесть нельзя; после выбора группы
        специалистом ФР та же строка листа выбирает занятие этой группы."""
        text = "Бассейн в {} 30 мин 1 р/д"
        program = do_import(doctor, department, text.format("09:00"))
        item = program.prescriptions.get()
        assert item.procedure.is_generic_pool and item.group_session is None
        warning = next(w for w in program.import_warnings if "09:00" in w)
        assert "не учтено — сначала выберите группу бассейна" in warning
        assert "нет занятий" not in warning

        group = (
            Procedure.objects.filter(kind="POOL", group_choice=False, sessions__is_active=True)
            .distinct()
            .first()
        )
        start = group.sessions.filter(is_active=True).order_by("start_time").first().start_time
        choose_pool_group(rehab, item, group)
        apply_sheet_update(doctor, program, sheet(text.format(f"{start:%H:%M}")))

        item.refresh_from_db()
        assert item.pool_group == group
        assert item.group_session is not None and item.group_session.start_time == start
        assert starts(item) == {start}
