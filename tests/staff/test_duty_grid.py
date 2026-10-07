"""Экран «Распорядок» (TZ.md, FR-STF-5): сетка инструкторы × слоты, клик «кистью».

Пара 2/2 Волков (работает 05–06.10, 09–10.10) / Лебедева (07–08.10), Соколов — 5/2.
"""

from datetime import date, time

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.scheduling import board
from apps.scheduling.board import FREE, BoardError, paint_duty
from apps.staff import domain
from apps.staff.models import BlockKind, DutyKind, InstructorBlock, InstructorDuty
from apps.staff.services import duty_grid
from tests.conftest import PASSWORD

WED = date(2026, 10, 7)
NEXT_WED = date(2026, 10, 14)


class TestShiftHours:
    @pytest.mark.parametrize(
        ("pattern", "end"),
        [
            (domain.Pattern.TWO_TWO, time(20)),
            (domain.Pattern.FIVE_TWO, time(17)),
            (domain.Pattern.DAILY, time(17)),
        ],
    )
    def test_hours(self, pattern, end):
        assert domain.shift_hours(pattern) == (time(8), end)

    def test_evening_slot_only_for_two_two(self):
        assert domain.slot_in_shift("2/2", time(19, 20), time(19, 50))
        assert not domain.slot_in_shift("5/2", time(18), time(18, 30))

    def test_slot_must_end_by_shift_end(self):
        assert domain.slot_in_shift("5/2", time(16, 20), time(16, 50))
        assert not domain.slot_in_shift("5/2", time(16, 40), time(17, 10))
        assert not domain.slot_in_shift("2/2", time(7, 30), time(8, 0))


def cells(rows, name):
    row = next(r for r in rows if r.instructor.short_name == name)
    return row, {f"{c.slot.start:%-H:%M}": c for c in row.cells}


@pytest.mark.django_db
class TestGrid:
    def test_pairs_side_by_side_and_shift_hours(self, rehab, staff):
        grid, rows = duty_grid(rehab, WED)
        assert len(grid) == 5
        assert [r.instructor.short_name for r in rows] == ["Волков", "Лебедева", "Соколов"]
        volkov, by_slot = cells(rows, "Волков")
        assert not volkov.working and volkov.hours == (time(8), time(20))
        assert by_slot["18:00"].in_shift
        sokolov, by_slot = cells(rows, "Соколов")
        assert sokolov.working and sokolov.pattern_text == "5/2 (пн–пт)"
        assert by_slot["9:10"].in_shift and not by_slot["18:00"].in_shift

    def test_duty_block_and_cleared(self, rehab, staff, slots):
        sokolov = staff["sokolov"]
        for key in ("9:10", "9:50"):
            InstructorDuty.objects.create(
                instructor=sokolov, slot=slots[key], kind=DutyKind.METHOD_WORK, valid_from=WED
            )
        InstructorBlock.objects.create(
            instructor=sokolov, slot=slots["9:10"], date=WED, kind=BlockKind.CLEAR
        )
        InstructorBlock.objects.create(
            instructor=sokolov, slot=slots["10:30"], date=WED, kind=BlockKind.BOS
        )
        _row, by_slot = cells(duty_grid(rehab, WED)[1], "Соколов")
        assert by_slot["9:10"].busy is None and by_slot["9:10"].cleared
        assert by_slot["9:50"].busy.text == "Метод. работа" and not by_slot["9:50"].busy.one_off
        assert by_slot["10:30"].busy.text == "БОС" and by_slot["10:30"].busy.one_off

    def test_duty_shown_on_day_off(self, rehab, staff, slots):
        """Распорядок задают и на дни, когда инструктор не на смене."""
        InstructorDuty.objects.create(
            instructor=staff["volkov"], slot=slots["9:10"], kind=DutyKind.BOS, valid_from=WED
        )
        _row, by_slot = cells(duty_grid(rehab, WED)[1], "Волков")
        assert by_slot["9:10"].busy.text == "БОС"

    def test_doctor_has_no_access(self, doctor, staff):
        with pytest.raises(PermissionDenied):
            duty_grid(doctor, WED)


@pytest.mark.django_db
class TestPaintPermanent:
    def test_set_and_toggle_off(self, rehab, staff, slots):
        sokolov, slot = staff["sokolov"], slots["9:10"]
        paint_duty(rehab, sokolov, slot, WED, DutyKind.METHOD_WORK, once=False)
        duty = InstructorDuty.objects.get(instructor=sokolov)
        assert (duty.kind, duty.valid_from, duty.valid_to) == (DutyKind.METHOD_WORK, WED, None)
        # Повторный клик тем же видом в ту же дату — распорядка как не было.
        paint_duty(rehab, sokolov, slot, WED, DutyKind.METHOD_WORK, once=False)
        assert not InstructorDuty.objects.filter(instructor=sokolov).exists()

    def test_other_kind_replaces_from_date(self, rehab, staff, slots):
        sokolov, slot = staff["sokolov"], slots["9:10"]
        paint_duty(rehab, sokolov, slot, WED, DutyKind.METHOD_WORK, once=False)
        paint_duty(rehab, sokolov, slot, NEXT_WED, DutyKind.OTHER, once=False, label="Обход")
        old, new = InstructorDuty.objects.filter(instructor=sokolov).order_by("valid_from")
        assert old.valid_to == date(2026, 10, 13)
        assert (new.kind, new.label, new.valid_from) == (DutyKind.OTHER, "Обход", NEXT_WED)

    def test_free_ends_duty(self, rehab, staff, slots):
        sokolov, slot = staff["sokolov"], slots["9:10"]
        paint_duty(rehab, sokolov, slot, WED, DutyKind.BOS, once=False)
        paint_duty(rehab, sokolov, slot, NEXT_WED, FREE, once=False)
        assert InstructorDuty.objects.get(instructor=sokolov).valid_to == date(2026, 10, 13)

    def test_free_on_empty_slot(self, rehab, staff, slots):
        with pytest.raises(BoardError, match="нет постоянного распорядка"):
            paint_duty(rehab, staff["sokolov"], slots["9:10"], WED, FREE, once=False)

    def test_out_of_shift_for_five_two(self, rehab, staff, slots):
        with pytest.raises(BoardError, match="с 8:00 до 17:00"):
            paint_duty(rehab, staff["sokolov"], slots["18:00"], WED, DutyKind.BOS, once=False)

    def test_evening_for_two_two(self, rehab, staff, slots):
        paint_duty(rehab, staff["volkov"], slots["18:00"], WED, DutyKind.BOS, once=False)
        assert InstructorDuty.objects.filter(instructor=staff["volkov"]).exists()

    def test_no_shift_pattern(self, rehab, staff, slots):
        with pytest.raises(BoardError, match="нет шаблона смены"):
            paint_duty(
                rehab, staff["sokolov"], slots["9:10"], date(2026, 9, 1), DutyKind.BOS, once=False
            )

    def test_other_needs_label(self, rehab, staff, slots):
        with pytest.raises(BoardError, match="Укажите"):
            paint_duty(rehab, staff["sokolov"], slots["9:10"], WED, DutyKind.OTHER, once=False)

    def test_doctor_cannot_paint(self, doctor, staff, slots):
        with pytest.raises(BoardError):
            paint_duty(doctor, staff["sokolov"], slots["9:10"], WED, DutyKind.BOS, once=False)


@pytest.mark.django_db
class TestPaintOnce:
    def test_block_and_toggle_off(self, rehab, staff, slots):
        sokolov, slot = staff["sokolov"], slots["9:10"]
        paint_duty(rehab, sokolov, slot, WED, DutyKind.BOS, once=True)
        block = InstructorBlock.objects.get(instructor=sokolov)
        assert (block.date, block.kind) == (WED, BlockKind.BOS)
        assert not InstructorDuty.objects.exists()
        paint_duty(rehab, sokolov, slot, WED, DutyKind.BOS, once=True)
        assert not InstructorBlock.objects.exists()

    def test_free_clears_duty_on_date_and_back(self, rehab, staff, slots):
        sokolov, slot = staff["sokolov"], slots["9:10"]
        paint_duty(rehab, sokolov, slot, WED, DutyKind.METHOD_WORK, once=False)
        paint_duty(rehab, sokolov, slot, NEXT_WED, FREE, once=True)
        assert InstructorBlock.objects.get(instructor=sokolov).kind == BlockKind.CLEAR
        assert InstructorDuty.objects.get(instructor=sokolov).valid_to is None
        # Повторный клик «Свободно» возвращает слот к распорядку.
        paint_duty(rehab, sokolov, slot, NEXT_WED, FREE, once=True)
        assert not InstructorBlock.objects.exists()

    def test_free_replaces_one_off_over_duty(self, rehab, staff, slots):
        sokolov, slot = staff["sokolov"], slots["9:10"]
        paint_duty(rehab, sokolov, slot, WED, DutyKind.METHOD_WORK, once=False)
        paint_duty(rehab, sokolov, slot, WED, DutyKind.BOS, once=True)
        paint_duty(rehab, sokolov, slot, WED, FREE, once=True)
        assert InstructorBlock.objects.get(instructor=sokolov).kind == BlockKind.CLEAR


@pytest.mark.django_db
class TestViews:
    def login(self, client, user):
        client.login(username=user.username, password=PASSWORD)

    def test_page(self, client, rehab, staff):
        self.login(client, rehab)
        response = client.get(reverse("staff:duties"), {"day": "2026-10-07"})
        assert response.status_code == 200
        text = response.content.decode()
        assert "Соколов" in text and "Постоянно с даты" in text and 'value="2026-10-07"' in text

    def test_doctor_forbidden(self, client, doctor, staff):
        self.login(client, doctor)
        assert client.get(reverse("staff:duties")).status_code == 403

    def test_paint_returns_grid(self, client, rehab, staff, slots):
        self.login(client, rehab)
        data = {
            "instructor": staff["sokolov"].pk,
            "slot": slots["9:10"].pk,
            "day": "2026-10-07",
            "kind": "BOS",
            "mode": "always",
        }
        response = client.post(reverse("staff:duty_paint"), data)
        assert response.status_code == 200
        assert 'id="duty-grid"' in response.content.decode()
        assert InstructorDuty.objects.filter(instructor=staff["sokolov"], kind="BOS").exists()

    def test_paint_error_in_fragment(self, client, rehab, staff, slots):
        self.login(client, rehab)
        data = {
            "instructor": staff["sokolov"].pk,
            "slot": slots["18:00"].pk,
            "day": "2026-10-07",
            "kind": "BOS",
            "mode": "once",
        }
        response = client.post(reverse("staff:duty_paint"), data)
        assert response.status_code == 200
        assert "вне смены" in response.content.decode()
        assert not InstructorBlock.objects.exists()

    def test_group_lead_with_session(self, client, rehab, staff, slots, procedures):
        from apps.catalog.models import GroupSession

        session = GroupSession.objects.create(
            procedure=procedures["group"], start_time=time(9, 10), duration_min=30
        )
        self.login(client, rehab)
        data = {
            "instructor": staff["sokolov"].pk,
            "slot": slots["9:10"].pk,
            "day": "2026-10-07",
            "kind": "GROUP_LEAD",
            "session": session.pk,
            "mode": "always",
        }
        client.post(reverse("staff:duty_paint"), data)
        duty = InstructorDuty.objects.get(instructor=staff["sokolov"])
        assert duty.group_session == session
        assert board.duty_at(staff["sokolov"], slots["9:10"], WED) == duty
