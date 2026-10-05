"""Смены, распорядок и разовые блоки: модели, сервисы, права (TZ.md, FR-STF-2…6)."""

from datetime import date, time

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import Role
from apps.staff import services
from apps.staff.domain import Busy, DutyKind
from apps.staff.models import (
    BlockKind,
    InstructorBlock,
    InstructorDuty,
    ShiftException,
    ShiftPattern,
)

pytestmark = pytest.mark.django_db

MON = date(2026, 10, 5)
SAT = date(2026, 10, 10)


def pattern(instructor, kind="5/2", valid_from=date(2026, 10, 1), valid_to=None, anchor=None):
    return ShiftPattern.objects.create(
        instructor=instructor,
        pattern=kind,
        anchor_date=anchor or valid_from,
        valid_from=valid_from,
        valid_to=valid_to,
    )


class TestPermissions:
    def test_rehab_of_any_department_and_admin_can_manage(
        self, rehab, admin_user, make_user, other_department
    ):
        other_rehab = make_user("rehab2", (other_department, Role.REHAB))
        assert services.can_manage_staff(rehab)
        assert services.can_manage_staff(other_rehab)
        assert services.can_manage_staff(admin_user)

    def test_doctor_cannot(self, doctor, instructors, slots):
        volkov = instructors["Волков"]
        calls = [
            lambda: services.shift_month(doctor, 2026, 10),
            lambda: services.change_shift_pattern(doctor, volkov, "2/2", MON, MON),
            lambda: services.set_working(doctor, volkov, MON, False),
            lambda: services.toggle_shift_day(doctor, volkov, MON),
            lambda: services.save_duty(
                doctor,
                InstructorDuty(instructor=volkov, slot=slots["9:10"], kind="BOS", valid_from=MON),
            ),
        ]
        for call in calls:
            with pytest.raises(PermissionDenied):
                call()
        assert not ShiftPattern.objects.exists()
        assert not InstructorDuty.objects.exists()


class TestShiftPattern:
    def test_change_closes_previous_pattern(self, rehab, instructors):
        volkov = instructors["Волков"]
        services.change_shift_pattern(rehab, volkov, "5/2", date(2026, 9, 1), date(2026, 9, 1))

        new = services.change_shift_pattern(rehab, volkov, "2/2", MON, MON)

        old = volkov.shift_patterns.get(valid_from=date(2026, 9, 1))
        assert old.valid_to == date(2026, 10, 4)
        assert new.valid_to is None and new.pattern == "2/2"
        assert old.history.first().history_user == rehab
        calendar = services.instructor_calendars([volkov], date(2026, 10, 3), date(2026, 10, 8))
        works = [calendar[volkov.pk].is_working(date(2026, 10, d)) for d in range(3, 9)]
        # 03–04.10 — выходные по 5/2; с 05.10 — 2/2 от понедельника.
        assert works == [False, False, True, True, False, False]

    def test_same_start_is_corrected_in_place(self, rehab, instructors):
        volkov = instructors["Волков"]
        services.change_shift_pattern(rehab, volkov, "5/2", MON, MON)

        services.change_shift_pattern(rehab, volkov, "2/2", date(2026, 10, 6), MON)

        assert volkov.shift_patterns.count() == 1
        assert volkov.shift_patterns.get().anchor_date == date(2026, 10, 6)

    def test_later_pattern_is_an_error(self, rehab, instructors):
        volkov = instructors["Волков"]
        services.change_shift_pattern(rehab, volkov, "5/2", SAT, SAT)

        with pytest.raises(services.StaffError, match=r"10\.10\.2026"):
            services.change_shift_pattern(rehab, volkov, "2/2", MON, MON)

    def test_overlap_rejected_by_clean_and_database(self, instructors):
        volkov = instructors["Волков"]
        pattern(volkov, valid_from=MON, valid_to=SAT)
        overlapping = ShiftPattern(
            instructor=volkov, pattern="daily", anchor_date=SAT, valid_from=SAT
        )

        with pytest.raises(ValidationError, match="Пересекается"):
            overlapping.full_clean()
        with transaction.atomic(), pytest.raises(IntegrityError):
            overlapping.save()
        # У другого инструктора тот же период — можно.
        pattern(instructors["Соколов"], valid_from=MON, valid_to=SAT)

    def test_period_end_before_start(self, instructors):
        item = ShiftPattern(
            instructor=instructors["Волков"],
            pattern="daily",
            anchor_date=MON,
            valid_from=SAT,
            valid_to=MON,
        )
        with pytest.raises(ValidationError, match="Конец периода"):
            item.full_clean()

    def test_str(self, instructors):
        item = pattern(instructors["Волков"], valid_from=MON)
        assert str(item) == "Волков: 5/2 (пн–пт) с 05.10.2026, бессрочно"


class TestSetWorking:
    def test_sick_day_creates_exception_and_back_removes_it(self, rehab, instructors):
        volkov = instructors["Волков"]
        pattern(volkov)

        item = services.set_working(rehab, volkov, MON, False, "больничный")

        assert item.reason == "больничный" and not item.is_working
        assert item.history.first().history_user == rehab
        calendar = services.instructor_calendars([volkov], MON, MON)[volkov.pk]
        assert not calendar.is_working(MON)

        assert services.set_working(rehab, volkov, MON, True) is None
        assert not ShiftException.objects.exists()
        assert ShiftException.history.first().history_type == "-"

    def test_same_as_pattern_needs_no_exception(self, rehab, instructors):
        volkov = instructors["Волков"]
        pattern(volkov)
        assert services.set_working(rehab, volkov, MON, True) is None
        assert not ShiftException.objects.exists()

    def test_toggle_twice_returns_to_pattern(self, rehab, instructors):
        volkov = instructors["Волков"]
        pattern(volkov)

        services.toggle_shift_day(rehab, volkov, SAT)
        assert ShiftException.objects.get().is_working

        services.toggle_shift_day(rehab, volkov, SAT)
        assert not ShiftException.objects.exists()

    def test_unique_per_day(self, instructors):
        volkov = instructors["Волков"]
        ShiftException.objects.create(instructor=volkov, date=MON, is_working=False)
        with transaction.atomic(), pytest.raises(IntegrityError):
            ShiftException.objects.create(instructor=volkov, date=MON, is_working=True)


class TestShiftMonth:
    def test_rows_pairs_side_by_side(self, rehab, instructors):
        volkov, sokolov = instructors["Волков"], instructors["Соколов"]
        pattern(volkov, "2/2", anchor=date(2026, 10, 1))
        pattern(sokolov, "5/2")
        services.set_working(rehab, sokolov, SAT, True, "подмена")

        rows = services.shift_month(rehab, 2026, 10)

        assert [row.instructor.short_name for row in rows] == ["Волков", "Лебедева", "Соколов"]
        assert len(rows[0].cells) == 31
        assert [cell.working for cell in rows[0].cells[:4]] == [True, True, False, False]
        assert rows[0].pattern.pattern == "2/2"
        assert rows[1].pattern is None and not any(cell.working for cell in rows[1].cells)
        saturday = rows[2].cells[9]
        assert saturday.day == SAT and saturday.working and saturday.exception is not None

    def test_inactive_instructor_hidden(self, rehab, instructors):
        sokolov = instructors["Соколов"]
        sokolov.is_active = False
        sokolov.save()
        names = [row.instructor.short_name for row in services.shift_month(rehab, 2026, 10)]
        assert "Соколов" not in names


class TestDuties:
    def test_group_lead_needs_session_in_slot(self, instructors, slots, ergo_session):
        volkov = instructors["Волков"]
        missing = InstructorDuty(
            instructor=volkov, slot=slots["9:10"], kind="GROUP_LEAD", valid_from=MON
        )
        with pytest.raises(ValidationError, match="Выберите занятие группы"):
            missing.full_clean()
        wrong_slot = InstructorDuty(
            instructor=volkov,
            slot=slots["10:30"],
            kind="GROUP_LEAD",
            group_session=ergo_session,
            valid_from=MON,
        )
        with pytest.raises(ValidationError, match="не попадает в слот"):
            wrong_slot.full_clean()
        extra = InstructorDuty(
            instructor=volkov,
            slot=slots["9:10"],
            kind="BOS",
            group_session=ergo_session,
            valid_from=MON,
        )
        with pytest.raises(ValidationError, match="только для ведения группы"):
            extra.full_clean()
        other = InstructorDuty(instructor=volkov, slot=slots["9:10"], kind="OTHER", valid_from=MON)
        with pytest.raises(ValidationError, match="чем занят"):
            other.full_clean()

    def test_one_duty_per_slot_and_period(self, rehab, instructors, slots):
        volkov = instructors["Волков"]
        services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=MON
            ),
        )
        second = InstructorDuty(instructor=volkov, slot=slots["15:00"], kind="BOS", valid_from=SAT)
        with pytest.raises(ValidationError, match=r"уже есть «Метод\. работа»"):
            services.save_duty(rehab, second)
        with transaction.atomic(), pytest.raises(IntegrityError):
            second.save()

    def test_end_duty_then_new_one(self, rehab, instructors, slots):
        volkov = instructors["Волков"]
        duty = services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=MON
            ),
        )
        with pytest.raises(services.StaffError, match="раньше нельзя"):
            services.end_duty(rehab, duty, date(2026, 10, 1))

        services.end_duty(rehab, duty, date(2026, 10, 9))
        services.save_duty(
            rehab,
            InstructorDuty(instructor=volkov, slot=slots["15:00"], kind="BOS", valid_from=SAT),
        )

        assert duty.history.first().valid_to == date(2026, 10, 9)
        assert duty.history.first().history_user == rehab
        assert [d.text for d in services.duties_of(volkov)] == ["Метод. работа", "БОС"]

    def test_calendar_busy_slots(self, rehab, instructors, slots, ergo_session):
        volkov = instructors["Волков"]
        pattern(volkov, "daily")
        services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov,
                slot=slots["9:10"],
                kind="GROUP_LEAD",
                group_session=ergo_session,
                valid_from=MON,
            ),
        )
        services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov, slot=slots["11:10"], kind="METHOD_WORK", valid_from=MON
            ),
        )
        services.save_block(
            rehab,
            InstructorBlock(instructor=volkov, date=SAT, slot=slots["9:10"], kind=BlockKind.CLEAR),
        )
        services.save_block(
            rehab,
            InstructorBlock(
                instructor=volkov, date=SAT, slot=slots["13:00"], kind="OTHER", label="Комиссия"
            ),
        )

        calendar = services.instructor_calendars(None, date(2026, 10, 4), SAT)[volkov.pk]

        monday = calendar.day(MON)
        assert monday.busy == {
            slots["9:10"].pk: Busy(DutyKind.GROUP_LEAD, "Эрго общая"),
            slots["11:10"].pk: Busy(DutyKind.METHOD_WORK),
        }
        saturday = calendar.day(SAT)
        assert saturday.is_free(slots["9:10"].pk)
        assert saturday.busy[slots["13:00"].pk].text == "Комиссия"
        assert not calendar.day(date(2026, 10, 4)).busy  # до начала распорядка

    def test_blocks_unique_and_deletable(self, rehab, instructors, slots):
        volkov = instructors["Волков"]
        block = services.save_block(
            rehab,
            InstructorBlock(instructor=volkov, date=SAT, slot=slots["9:10"], kind="BOS"),
        )
        with pytest.raises(services.StaffError, match="уже есть разовый блок"):
            services.save_block(
                rehab,
                InstructorBlock(instructor=volkov, date=SAT, slot=slots["9:10"], kind="CLEAR"),
            )
        assert [b.pk for b in services.upcoming_blocks(volkov, MON)] == [block.pk]
        assert not services.upcoming_blocks(volkov, date(2026, 10, 11)).exists()
        assert str(block) == "Волков 10.10.2026 09:10–09:40: БОС"

        services.delete_block(rehab, block)

        assert not InstructorBlock.objects.exists()
        assert InstructorBlock.history.first().history_user == rehab

    def test_block_rights(self, doctor, instructors, slots):
        block = InstructorBlock.objects.create(
            instructor=instructors["Волков"], date=SAT, slot=slots["9:10"], kind="BOS"
        )
        with pytest.raises(PermissionDenied):
            services.delete_block(doctor, block)
        with pytest.raises(PermissionDenied):
            services.end_duty(
                doctor,
                InstructorDuty(
                    instructor=instructors["Волков"], slot=slots["9:10"], kind="BOS", valid_from=MON
                ),
                SAT,
            )

    def test_slot_with_duty_is_protected(self, instructors, slots):
        from django.db.models import ProtectedError

        InstructorDuty.objects.create(
            instructor=instructors["Волков"], slot=slots["9:10"], kind="BOS", valid_from=MON
        )
        with pytest.raises(ProtectedError):
            slots["9:10"].delete()


class TestGroupLeadSpan:
    def test_long_group_closes_all_overlapping_slots(self, rehab, instructors, slots, procedures):
        from apps.catalog.models import GroupSession, InstructorSlot

        slot_950 = InstructorSlot.objects.create(start=time(9, 50), end=time(10, 20))
        session = GroupSession.objects.create(
            procedure=procedures["group"], start_time=time(9, 10), duration_min=60
        )
        volkov = instructors["Волков"]
        pattern(volkov, "daily")
        services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov,
                slot=slots["9:10"],
                kind="GROUP_LEAD",
                group_session=session,
                valid_from=MON,
            ),
        )
        services.save_block(
            rehab,
            InstructorBlock(instructor=volkov, date=SAT, slot=slot_950, kind="CLEAR"),
        )

        calendar = services.instructor_calendars([volkov], MON, SAT)[volkov.pk]

        monday = calendar.day(MON)
        assert set(monday.busy) == {slots["9:10"].pk, slot_950.pk}
        assert monday.busy[slot_950.pk].text == "Эрго общая"
        assert monday.is_free(slots["10:30"].pk)
        # CLEAR в любом из слотов снимает ведение группы целиком.
        assert calendar.day(SAT).busy == {}

    def test_only_lfk_groups(self, instructors, slots):
        from apps.catalog.models import GroupSession, Procedure, ProcedureKind

        pool = Procedure.objects.create(
            name="Бассейн: спина", card_label="Бассейн", kind=ProcedureKind.POOL
        )
        session = GroupSession.objects.create(procedure=pool, start_time=time(9, 10))
        duty = InstructorDuty(
            instructor=instructors["Волков"],
            slot=slots["9:10"],
            kind="GROUP_LEAD",
            group_session=session,
            valid_from=MON,
        )
        with pytest.raises(ValidationError, match="только группы ЛФК"):
            duty.full_clean()


class TestCalendarRange:
    def test_day_outside_loaded_range_is_an_error(self, instructors):
        volkov = instructors["Волков"]
        calendar = services.instructor_calendars([volkov], MON, SAT)[volkov.pk]
        with pytest.raises(ValueError, match="загружен"):
            calendar.day(date(2026, 10, 11))
        with pytest.raises(ValueError):
            calendar.is_working(date(2026, 10, 4))


class TestTeams:
    def test_build_teams(self, instructors):
        teams = services.build_teams()
        assert [team.label for team in teams] == ["Волков/ Лебедева", "Соколов"]
        volkov, lebedeva = instructors["Волков"], instructors["Лебедева"]
        assert teams[0].label == volkov.team_label
        assert teams[0].key == f"{volkov.pk}-{lebedeva.pk}"
        assert teams[1].member_ids == (instructors["Соколов"].pk,)

    def test_inactive_partner_leaves_single(self, instructors):
        lebedeva = instructors["Лебедева"]
        lebedeva.is_active = False
        lebedeva.save()
        assert [team.label for team in services.build_teams()] == ["Волков", "Соколов"]

    def test_candidates_of_pair(self, rehab, instructors, slots):
        from apps.staff.domain import team_candidates

        volkov, lebedeva = instructors["Волков"], instructors["Лебедева"]
        pattern(volkov, "2/2", anchor=MON)
        pattern(lebedeva, "2/2", anchor=date(2026, 10, 7))
        services.set_working(rehab, lebedeva, MON, True, "подмена")
        team = services.build_teams()[0]
        calendars = services.instructor_calendars(None, MON, SAT)
        slot = slots["9:10"].pk

        assert team_candidates(team, calendars, MON, slot) == [volkov.pk, lebedeva.pk]
        assert team_candidates(team, calendars, date(2026, 10, 7), slot) == [lebedeva.pk]
        services.set_working(rehab, lebedeva, date(2026, 10, 8), False)
        calendars = services.instructor_calendars(None, MON, SAT)
        assert team_candidates(team, calendars, date(2026, 10, 8), slot) == []
