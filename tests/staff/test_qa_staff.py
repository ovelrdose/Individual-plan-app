"""QA шага 3.1: смены, распорядок и разовые блоки — граничные случаи, права, ввод (TZ.md §4.6).

Дополняет test_domain / test_services / test_views: здесь — то, что они не проверяют.
Все данные вымышленные.
"""

import html
import re
from datetime import date, time, timedelta

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.catalog.models import GroupSession, Procedure, ProcedureKind
from apps.staff import services
from apps.staff.domain import Busy, DutyKind
from apps.staff.models import (
    Instructor,
    InstructorBlock,
    InstructorDuty,
    ShiftException,
    ShiftPattern,
)

pytestmark = pytest.mark.django_db

HTMX = {"HTTP_HX_REQUEST": "true"}
MON = date(2026, 10, 5)
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)
FUTURE = date(2099, 1, 10)  # для разовых блоков: экран показывает только ближайшие


def make_pattern(instructor, kind="5/2", valid_from=date(2026, 9, 1), valid_to=None, anchor=None):
    return ShiftPattern.objects.create(
        instructor=instructor,
        pattern=kind,
        anchor_date=anchor or valid_from,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def calendar_of(instructor, start, end):
    return services.instructor_calendars([instructor], start, end)[instructor.pk]


def working_days(instructor, start, end) -> list[bool]:
    cal = calendar_of(instructor, start, end)
    count = (end - start).days + 1
    return [cal.is_working(start + timedelta(days=n)) for n in range(count)]


@pytest.fixture
def rehab_client(client, rehab):
    client.force_login(rehab)
    return client


@pytest.fixture
def volkov(instructors):
    return instructors["Волков"]


# --- Шаблоны смен: граничные случаи (FR-STF-2, решение 51) -------------------------------


class TestTwoTwoEdges:
    def test_dates_before_anchor_inside_period(self, volkov):
        # Якорь позже начала периода: цикл идёт назад от якоря.
        make_pattern(volkov, "2/2", valid_from=date(2026, 9, 28), anchor=MON)

        works = working_days(volkov, date(2026, 9, 27), MON)

        # 27.09 по циклу рабочий, но до начала периода — нерабочий.
        assert works == [False, True, False, False, True, True, False, False, True]

    def test_change_in_middle_of_cycle(self, rehab, volkov):
        services.change_shift_pattern(rehab, volkov, "2/2", date(2026, 10, 1), date(2026, 9, 1))

        # 07.10 по старому циклу — выходной; новый цикл начинается с него.
        services.change_shift_pattern(rehab, volkov, "2/2", date(2026, 10, 7), date(2026, 10, 7))

        old, new = volkov.shift_patterns.order_by("valid_from")
        assert old.valid_to == date(2026, 10, 6) and new.valid_to is None
        works = working_days(volkov, MON, SUN)
        # 05–06 — по старому, 07–08 — по новому, 09–10 — выходные, 11 — рабочий.
        assert works == [True, True, True, True, False, False, True]

    def test_valid_to_inclusive_at_range_edge(self, volkov):
        make_pattern(volkov, "2/2", valid_from=date(2026, 10, 1), valid_to=MON, anchor=MON)

        # Запрос календаря, начинающийся ровно с последнего дня периода, видит шаблон.
        assert calendar_of(volkov, MON, MON).is_working(MON)
        tuesday = MON + timedelta(days=1)
        # 06.10 по циклу рабочий, но период закончился 05.10.
        assert not calendar_of(volkov, tuesday, tuesday).is_working(tuesday)

    def test_new_pattern_after_closed_one_leaves_gap(self, rehab, volkov):
        make_pattern(volkov, "daily", valid_from=date(2026, 9, 1), valid_to=date(2026, 9, 30))

        services.change_shift_pattern(rehab, volkov, "daily", MON, MON)

        assert volkov.shift_patterns.count() == 2
        assert working_days(volkov, date(2026, 9, 30), MON) == [True] + [False] * 4 + [True]

    def test_pattern_before_existing_one_is_an_error(self, rehab, volkov):
        services.change_shift_pattern(rehab, volkov, "5/2", MON, MON)

        with pytest.raises(services.StaffError, match=r"05\.10\.2026"):
            services.change_shift_pattern(rehab, volkov, "2/2", MON, date(2026, 9, 1))
        assert volkov.shift_patterns.count() == 1


class TestFiveTwoAndExceptions:
    def test_weekend_off_and_exception_working(self, rehab, volkov, slots):
        make_pattern(volkov, "5/2")
        InstructorDuty.objects.create(
            instructor=volkov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=MON
        )

        services.set_working(rehab, volkov, SAT, True, "подмена")

        cal = calendar_of(volkov, SAT, SUN)
        assert cal.is_working(SAT) and not cal.by_pattern(SAT)
        # В рабочий по исключению день распорядок действует.
        assert cal.day(SAT).busy == {slots["15:00"].pk: Busy(DutyKind.METHOD_WORK)}
        assert cal.day(SAT).is_free(slots["9:10"].pk)
        # Воскресенье без исключения: занятости нет, но и свободных слотов нет.
        sunday = cal.day(SUN)
        assert not sunday.working and sunday.busy == {}
        assert sunday.free_slots([s.pk for s in slots.values()]) == []

    def test_sick_on_two_two_working_day(self, rehab, volkov):
        make_pattern(volkov, "2/2", anchor=MON)

        services.set_working(rehab, volkov, MON, False, "больничный")

        assert working_days(volkov, MON, MON + timedelta(days=1)) == [False, True]

    def test_exception_without_any_pattern(self, rehab, volkov):
        item = services.set_working(rehab, volkov, MON, True)

        assert item is not None and item.is_working
        assert working_days(volkov, MON - timedelta(days=1), MON) == [False, True]

    def test_toggle_writes_history_with_author(self, rehab, volkov):
        make_pattern(volkov, "5/2")

        services.toggle_shift_day(rehab, volkov, SAT)
        services.toggle_shift_day(rehab, volkov, SAT)

        records = list(ShiftException.history.order_by("history_date"))
        assert [r.history_type for r in records] == ["+", "-"]
        assert {r.history_user for r in records} == {rehab}

    def test_exception_matching_new_pattern_is_not_kept(self, rehab, volkov):
        # Решение 51: исключение без причины, совпавшее с новым шаблоном, удаляется;
        # с причиной («подмена») — остаётся как запись о событии.
        services.change_shift_pattern(rehab, volkov, "5/2", date(2026, 9, 1), date(2026, 9, 1))
        services.set_working(rehab, volkov, SAT, True)
        services.set_working(rehab, volkov, SUN, True, "подмена")
        services.set_working(rehab, volkov, date(2026, 10, 3), True)  # до смены шаблона

        services.change_shift_pattern(rehab, volkov, "daily", MON, MON)

        cells = services.shift_month(rehab, 2026, 10)[0].cells
        saturday, sunday = cells[SAT.day - 1], cells[SUN.day - 1]
        assert saturday.working and saturday.exception is None
        assert sunday.working and sunday.exception is not None
        assert cells[2].exception is not None
        removed = ShiftException.history.filter(history_type="-").get()
        assert removed.date == SAT and removed.history_user == rehab


# --- Распорядок и разовые блоки (FR-STF-5) -------------------------------------------------


class TestDutyPeriods:
    def test_duty_starting_and_ending_mid_month(self, volkov, slots):
        make_pattern(volkov, "daily")
        InstructorDuty.objects.create(
            instructor=volkov,
            slot=slots["13:00"],
            kind="BOS",
            valid_from=date(2026, 10, 15),
            valid_to=date(2026, 10, 20),
        )
        cal = calendar_of(volkov, date(2026, 10, 1), date(2026, 10, 31))

        busy = {d: bool(cal.day(date(2026, 10, d)).busy) for d in (14, 15, 20, 21)}

        assert busy == {14: False, 15: True, 20: True, 21: False}

    def test_duty_loaded_when_range_starts_on_its_last_day(self, volkov, slots):
        make_pattern(volkov, "daily")
        InstructorDuty.objects.create(
            instructor=volkov, slot=slots["13:00"], kind="BOS", valid_from=MON, valid_to=SAT
        )

        assert calendar_of(volkov, SAT, SAT).day(SAT).busy
        assert not calendar_of(volkov, SUN, SUN).day(SUN).busy

    def test_end_and_start_new_duty_same_slot_next_day(self, rehab, volkov, slots):
        make_pattern(volkov, "daily")
        duty = services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=MON
            ),
        )
        services.end_duty(rehab, duty, MON)  # последний день — день начала: допустимо
        services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov,
                slot=slots["15:00"],
                kind="BOS",
                valid_from=MON + timedelta(days=1),
            ),
        )
        cal = calendar_of(volkov, MON, SAT)

        assert cal.day(MON).busy[slots["15:00"].pk].kind is DutyKind.METHOD_WORK
        assert cal.day(MON + timedelta(days=1)).busy[slots["15:00"].pk].kind is DutyKind.BOS

    def test_end_duty_cannot_be_extended_into_next_one(self, rehab, volkov, slots):
        first = services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=MON
            ),
        )
        services.end_duty(rehab, first, MON)
        services.save_duty(
            rehab,
            InstructorDuty(instructor=volkov, slot=slots["15:00"], kind="BOS", valid_from=SAT),
        )

        with pytest.raises(Exception, match="уже есть «БОС»"):
            services.end_duty(rehab, first, SUN)


class TestClearAndBlocks:
    def test_clear_frees_only_that_slot_and_day(self, rehab, volkov, slots):
        make_pattern(volkov, "daily")
        for key in ("9:10", "15:00"):
            InstructorDuty.objects.create(
                instructor=volkov, slot=slots[key], kind="METHOD_WORK", valid_from=MON
            )
        services.save_block(
            rehab, InstructorBlock(instructor=volkov, date=SAT, slot=slots["9:10"], kind="CLEAR")
        )
        cal = calendar_of(volkov, MON, SUN)

        assert cal.day(SAT).is_free(slots["9:10"].pk)
        assert not cal.day(SAT).is_free(slots["15:00"].pk)
        assert not cal.day(SUN).is_free(slots["9:10"].pk)
        assert not cal.day(SAT - timedelta(days=1)).is_free(slots["9:10"].pk)

    def test_clear_on_slot_without_duty_is_harmless(self, rehab, volkov, slots):
        make_pattern(volkov, "daily")
        services.save_block(
            rehab, InstructorBlock(instructor=volkov, date=SAT, slot=slots["9:10"], kind="CLEAR")
        )
        day = calendar_of(volkov, SAT, SAT).day(SAT)
        assert day.busy == {} and day.is_free(slots["9:10"].pk)

    def test_clear_on_day_off_does_not_make_slot_free(self, rehab, volkov, slots):
        make_pattern(volkov, "5/2")
        InstructorDuty.objects.create(
            instructor=volkov, slot=slots["9:10"], kind="BOS", valid_from=MON
        )
        services.save_block(
            rehab, InstructorBlock(instructor=volkov, date=SUN, slot=slots["9:10"], kind="CLEAR")
        )
        assert not calendar_of(volkov, SUN, SUN).day(SUN).is_free(slots["9:10"].pk)

    def test_block_on_day_off_applies_only_when_working(self, rehab, volkov, slots):
        make_pattern(volkov, "5/2")
        services.save_block(
            rehab,
            InstructorBlock(
                instructor=volkov, date=SUN, slot=slots["13:00"], kind="OTHER", label="Комиссия"
            ),
        )
        assert calendar_of(volkov, SUN, SUN).day(SUN).busy == {}

        services.set_working(rehab, volkov, SUN, True, "подмена")

        busy = calendar_of(volkov, SUN, SUN).day(SUN).busy
        assert busy == {slots["13:00"].pk: Busy(DutyKind.OTHER, "Комиссия", one_off=True)}

    def test_block_replaces_duty_in_slot(self, rehab, volkov, slots, ergo_session):
        make_pattern(volkov, "daily")
        InstructorDuty.objects.create(
            instructor=volkov,
            slot=slots["9:10"],
            kind="GROUP_LEAD",
            group_session=ergo_session,
            valid_from=MON,
        )
        services.save_block(
            rehab, InstructorBlock(instructor=volkov, date=SAT, slot=slots["9:10"], kind="BOS")
        )
        cal = calendar_of(volkov, SAT, SUN)
        assert cal.day(SAT).busy[slots["9:10"].pk] == Busy(DutyKind.BOS, one_off=True)
        assert cal.day(SUN).busy[slots["9:10"].pk].text == "Эрго общая"

    def test_duplicate_block_rejected_by_database(self, volkov, slots):
        InstructorBlock.objects.create(instructor=volkov, date=SAT, slot=slots["9:10"], kind="BOS")
        with transaction.atomic(), pytest.raises(IntegrityError):
            InstructorBlock.objects.create(
                instructor=volkov, date=SAT, slot=slots["9:10"], kind="CLEAR"
            )

    def test_same_block_for_other_instructor_allowed(self, rehab, instructors, slots):
        for name in ("Волков", "Соколов"):
            services.save_block(
                rehab,
                InstructorBlock(
                    instructor=instructors[name], date=SAT, slot=slots["9:10"], kind="BOS"
                ),
            )
        assert InstructorBlock.objects.count() == 2


class TestGroupLeadTiming:
    """Ведение группы: занятие группы должно попадать в слот (решение 51)."""

    def session(self, procedures, start: time, minutes: int = 30) -> GroupSession:
        return GroupSession.objects.create(
            procedure=procedures["group"], start_time=start, duration_min=minutes
        )

    def duty(self, volkov, slot, session) -> InstructorDuty:
        return InstructorDuty(
            instructor=volkov,
            slot=slot,
            kind="GROUP_LEAD",
            group_session=session,
            valid_from=MON,
        )

    def test_session_touching_slot_end_is_rejected(self, rehab, volkov, slots, procedures):
        # Слот 9:10–9:40, группа 9:40–10:10 — общих минут нет.
        session = self.session(procedures, time(9, 40))
        with pytest.raises(Exception, match="не попадает в слот"):
            services.save_duty(rehab, self.duty(volkov, slots["9:10"], session))

    def test_session_ending_at_slot_start_is_rejected(self, rehab, volkov, slots, procedures):
        session = self.session(procedures, time(10, 0))  # 10:00–10:30, слот 10:30–11:00
        with pytest.raises(Exception, match="не попадает в слот"):
            services.save_duty(rehab, self.duty(volkov, slots["10:30"], session))

    def test_session_exactly_in_slot(self, rehab, volkov, slots, ergo_session):
        services.save_duty(rehab, self.duty(volkov, slots["9:10"], ergo_session))
        assert InstructorDuty.objects.get().text == "Эрго общая"

    def test_view_rejects_pool_and_inactive_sessions(self, rehab_client, volkov, slots):
        pool = Procedure.objects.create(
            name="Бассейн: спина",
            card_label="Бассейн спина",
            kind=ProcedureKind.POOL,
            default_duration_min=30,
        )
        pool_session = GroupSession.objects.create(procedure=pool, start_time=time(9, 10))
        lfk = Procedure.objects.create(
            name="Нейро-тренинг",
            card_label="Нейро-тренинг",
            kind=ProcedureKind.LFK_GROUP,
            default_duration_min=30,
        )
        inactive = GroupSession.objects.create(
            procedure=lfk, start_time=time(9, 10), is_active=False
        )
        url = reverse("staff:duty_add", args=[volkov.pk])

        for session in (pool_session, inactive):
            data = {
                "duty-slot": slots["9:10"].pk,
                "duty-kind": "GROUP_LEAD",
                "duty-group_session": session.pk,
                "duty-valid_from": "2026-10-05",
            }
            response = rehab_client.post(url, data, **HTMX)
            assert response.status_code == 200
            assert "Выберите корректный вариант" in response.content.decode()
        assert not InstructorDuty.objects.exists()


# --- Календарь «Смены»: пары рядом и месяцы (FR-STF-4) -------------------------------------


class TestPairsSideBySide:
    def names(self, user) -> list[str]:
        return [row.instructor.short_name for row in services.shift_month(user, 2026, 10)]

    def test_partner_with_lower_order_goes_first(self, rehab, instructors):
        lebedeva = instructors["Лебедева"]
        lebedeva.display_order = 5
        lebedeva.save()
        assert self.names(rehab) == ["Лебедева", "Волков", "Соколов"]

    def test_two_pairs_and_single(self, rehab, instructors):
        a = Instructor.objects.create(short_name="Андреев", full_name="А", display_order=20)
        b = Instructor.objects.create(short_name="Борисова", full_name="Б", display_order=50)
        services.set_partner(a, b)
        assert self.names(rehab) == ["Волков", "Лебедева", "Андреев", "Борисова", "Соколов"]

    def test_inactive_partner_hidden(self, rehab, instructors):
        lebedeva = instructors["Лебедева"]
        lebedeva.is_active = False
        lebedeva.save()
        assert self.names(rehab) == ["Волков", "Соколов"]

    def test_pair_marked_on_screens(self, rehab_client, instructors):
        page = rehab_client.get(reverse("staff:shifts"), {"month": "2026-10"}).content.decode()
        assert "Пара 2/2: Волков/ Лебедева" in page
        duties = rehab_client.get(reverse("staff:duties")).content.decode()
        assert "(пара Волков/ Лебедева)" in duties


class TestMonths:
    @pytest.mark.parametrize(
        ("month", "title", "previous", "following", "days"),
        [
            ("2026-12", "Декабрь 2026", "2026-11", "2027-01", 31),
            ("2027-01", "Январь 2027", "2026-12", "2027-02", 31),
            ("2027-02", "Февраль 2027", "2027-01", "2027-03", 28),
            ("2028-02", "Февраль 2028", "2028-01", "2028-03", 29),
        ],
    )
    def test_navigation(self, rehab_client, volkov, month, title, previous, following, days):
        response = rehab_client.get(reverse("staff:shifts"), {"month": month})

        assert response.status_code == 200
        ctx = response.context
        assert (ctx["month_title"], ctx["previous_month"], ctx["next_month"]) == (
            title,
            previous,
            following,
        )
        assert len(ctx["days"]) == days
        assert all(len(row.cells) == days for row in ctx["rows"])
        page = response.content.decode()
        assert f"?month={previous}" in page and f"?month={following}" in page

    def test_two_two_continues_over_new_year(self, rehab, volkov):
        make_pattern(volkov, "2/2", valid_from=date(2026, 12, 1), anchor=date(2026, 12, 30))

        dec = services.shift_month(rehab, 2026, 12)[0].cells
        jan = services.shift_month(rehab, 2027, 1)[0].cells

        assert [c.working for c in dec[-2:]] == [True, True]  # 30, 31 декабря
        assert [c.working for c in jan[:4]] == [False, False, True, True]

    def test_two_two_over_leap_day(self, rehab, volkov):
        make_pattern(volkov, "2/2", valid_from=date(2028, 1, 1), anchor=date(2028, 2, 28))

        feb = services.shift_month(rehab, 2028, 2)[0].cells
        mar = services.shift_month(rehab, 2028, 3)[0].cells

        assert [c.working for c in feb[-2:]] == [True, True]  # 28, 29 февраля
        assert [c.working for c in mar[:4]] == [False, False, True, True]

    def test_pattern_change_mid_month_shown(self, rehab, rehab_client, volkov):
        services.change_shift_pattern(rehab, volkov, "5/2", date(2026, 9, 1), date(2026, 9, 1))
        services.change_shift_pattern(
            rehab, volkov, "daily", date(2026, 10, 15), date(2026, 10, 15)
        )

        response = rehab_client.get(reverse("staff:shifts"), {"month": "2026-10"})

        row = response.context["rows"][0]
        assert row.pattern.pattern == "daily"
        assert not row.cells[SAT.day - 1].working  # 10.10 — суббота, ещё 5/2
        assert row.cells[16].working  # 17.10 — суббота, уже каждый день
        assert "с 15.10.2026" in response.content.decode()

    @pytest.mark.parametrize(
        "month", ["abc", "2026-00", "2026-1-1", "", "99999-01", "0-1", "2026", "-1-5", "2026-1a"]
    )
    def test_bad_month_does_not_fail(self, rehab_client, volkov, month):
        assert rehab_client.get(reverse("staff:shifts"), {"month": month}).status_code == 200

    def test_exception_cell_marked(self, rehab, rehab_client, volkov):
        make_pattern(volkov, "5/2")
        services.set_working(rehab, volkov, SAT, True, "подмена")

        page = rehab_client.get(reverse("staff:shifts"), {"month": "2026-10"}).content.decode()

        assert "Волков, 10.10.2026: рабочий (исключение)" in page
        assert 'Волков, 11.10.2026: выходной"' in page


# --- Права: матрица по всем URL и действиям (решения 46, 51) -------------------------------


@pytest.fixture
def objects(instructors, slots):
    volkov = instructors["Волков"]
    make_pattern(volkov, "5/2")
    duty = InstructorDuty.objects.create(
        instructor=volkov, slot=slots["15:00"], kind="BOS", valid_from=MON
    )
    block = InstructorBlock.objects.create(
        instructor=volkov, date=FUTURE, slot=slots["9:10"], kind="BOS"
    )
    return {"instructor": volkov, "duty": duty, "block": block, "slots": slots}


def action_payload(name: str, objects) -> dict:
    slots = objects["slots"]
    return {
        "staff:shift_toggle": {"day": "2026-10-05", "month": "2026-10"},
        "staff:shift_pattern": {
            "month": "2026-10",
            "pattern-pattern": "daily",
            "pattern-anchor_date": "2026-10-05",
            "pattern-valid_from": "2026-10-05",
        },
        "staff:duty_add": {
            "duty-slot": slots["11:10"].pk,
            "duty-kind": "METHOD_WORK",
            "duty-valid_from": "2026-10-05",
        },
        "staff:duty_edit": {
            "edit-slot": slots["13:00"].pk,
            "edit-kind": "OTHER",
            "edit-label": "Взлом",
            "edit-valid_from": "2026-10-05",
        },
        "staff:duty_end": {f"end{objects['duty'].pk}-valid_to": "2026-10-06"},
        "staff:block_add": {
            "block-date": "2099-01-11",
            "block-slot": slots["10:30"].pk,
            "block-kind": "BOS",
        },
        "staff:block_delete": {},
    }.get(name, {})


# (метод, имя URL, объект для pk)
ACTIONS = [
    ("get", "staff:shifts", None),
    ("post", "staff:shift_toggle", "instructor"),
    ("get", "staff:shift_pattern", "instructor"),
    ("post", "staff:shift_pattern", "instructor"),
    ("get", "staff:duties", None),
    ("post", "staff:duty_add", "instructor"),
    ("get", "staff:duty_edit", "duty"),
    ("post", "staff:duty_edit", "duty"),
    ("post", "staff:duty_end", "duty"),
    ("post", "staff:block_add", "instructor"),
    ("post", "staff:block_delete", "block"),
]
ACTION_IDS = [f"{method}-{name.split(':')[1]}" for method, name, _ in ACTIONS]


def call(client, objects, method, name, target):
    url = reverse(name, args=[objects[target].pk] if target else [])
    if method == "get":
        return client.get(url, **HTMX)
    return client.post(url, action_payload(name, objects), **HTMX)


def snapshot() -> tuple:
    return (
        list(ShiftPattern.objects.values_list("pk", "pattern", "valid_from", "valid_to")),
        list(ShiftException.objects.values_list("pk", "is_working")),
        list(InstructorDuty.objects.values_list("pk", "slot", "kind", "label", "valid_to")),
        list(InstructorBlock.objects.values_list("pk", "slot", "kind")),
    )


class TestPermissionMatrix:
    @pytest.mark.parametrize(("method", "name", "target"), ACTIONS, ids=ACTION_IDS)
    def test_doctor_forbidden_and_nothing_changes(
        self, client, doctor, objects, method, name, target
    ):
        client.force_login(doctor)
        before = snapshot()

        response = call(client, objects, method, name, target)

        assert response.status_code == 403
        assert snapshot() == before

    @pytest.mark.parametrize(("method", "name", "target"), ACTIONS, ids=ACTION_IDS)
    def test_user_without_role_forbidden(self, client, make_user, objects, method, name, target):
        client.force_login(make_user("nobody"))
        before = snapshot()
        assert call(client, objects, method, name, target).status_code == 403
        assert snapshot() == before

    @pytest.mark.parametrize(("method", "name", "target"), ACTIONS, ids=ACTION_IDS)
    def test_anonymous_redirected(self, client, objects, method, name, target):
        before = snapshot()

        response = call(client, objects, method, name, target)

        assert response.status_code == 302
        assert reverse("login") in response.url
        assert snapshot() == before

    @pytest.mark.parametrize(("method", "name", "target"), ACTIONS, ids=ACTION_IDS)
    def test_rehab_of_other_department_allowed(
        self, client, make_user, other_department, objects, method, name, target
    ):
        client.force_login(make_user("rehab_omr1", (other_department, Role.REHAB)))
        assert call(client, objects, method, name, target).status_code == 200

    @pytest.mark.parametrize(("method", "name", "target"), ACTIONS, ids=ACTION_IDS)
    def test_admin_allowed(self, client, admin_user, objects, method, name, target):
        client.force_login(admin_user)
        assert call(client, objects, method, name, target).status_code == 200

    def test_rehab_of_inactive_department_forbidden(self, client, make_user, other_department):
        other_department.is_active = False
        other_department.save()
        client.force_login(make_user("rehab_closed", (other_department, Role.REHAB)))
        assert client.get(reverse("staff:shifts")).status_code == 403

    def test_doctor_who_is_rehab_elsewhere_allowed(self, client, doctor, other_department, objects):
        Membership.objects.create(user=doctor, department=other_department, role=Role.REHAB)
        client.force_login(doctor)
        assert client.get(reverse("staff:duties")).status_code == 200

    def test_doctor_gets_403_not_404_for_missing_objects(self, client, doctor):
        client.force_login(doctor)
        for name in ("staff:shift_toggle", "staff:duty_end", "staff:block_delete"):
            assert client.post(reverse(name, args=[999999]), **HTMX).status_code == 403

    def test_rehab_actions_are_logged_with_author(self, rehab_client, rehab, objects):
        duty = objects["duty"]
        rehab_client.post(
            reverse("staff:duty_edit", args=[duty.pk]),
            action_payload("staff:duty_edit", objects),
            **HTMX,
        )
        assert duty.history.first().history_user == rehab
        assert duty.history.first().label == "Взлом"


class TestAdminAccess:
    def test_rehab_has_no_shift_models_in_admin(self, client, rehab):
        client.force_login(rehab)
        rehab.refresh_from_db()
        assert rehab.is_staff
        assert client.get(reverse("admin:staff_instructor_changelist")).status_code == 200
        for model in ("shiftpattern", "shiftexception", "instructorduty", "instructorblock"):
            assert client.get(reverse(f"admin:staff_{model}_changelist")).status_code == 403

    def test_admin_overlapping_pattern_shows_error(self, client, admin_user, volkov):
        make_pattern(volkov, "5/2")
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:staff_shiftpattern_add"),
            {
                "instructor": volkov.pk,
                "pattern": "daily",
                "anchor_date": "2026-10-05",
                "valid_from": "2026-10-05",
            },
        )

        assert response.status_code == 200
        assert "Пересекается с шаблоном" in response.content.decode()
        assert ShiftPattern.objects.count() == 1

    def test_admin_group_lead_without_session_shows_error(self, client, admin_user, volkov, slots):
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:staff_instructorduty_add"),
            {
                "instructor": volkov.pk,
                "slot": slots["9:10"].pk,
                "kind": "GROUP_LEAD",
                "valid_from": "2026-10-05",
            },
        )

        assert response.status_code == 200
        assert "Выберите занятие группы" in response.content.decode()
        assert not InstructorDuty.objects.exists()


# --- HTMX: фрагменты и ошибки в том же фрагменте ------------------------------------------


class TestHtmxFragments:
    @pytest.mark.parametrize(
        ("method", "name", "target", "root"),
        [
            ("post", "staff:shift_toggle", "instructor", 'id="shift-calendar"'),
            ("get", "staff:shift_pattern", "instructor", 'id="shift-calendar"'),
            ("post", "staff:shift_pattern", "instructor", 'id="shift-calendar"'),
            ("post", "staff:duty_add", "instructor", 'id="duties"'),
            ("get", "staff:duty_edit", "duty", 'id="duties"'),
            ("post", "staff:duty_edit", "duty", 'id="duties"'),
            ("post", "staff:duty_end", "duty", 'id="duties"'),
            ("post", "staff:block_add", "instructor", 'id="blocks"'),
            ("post", "staff:block_delete", "block", 'id="blocks"'),
        ],
    )
    def test_action_returns_whole_fragment(self, rehab_client, objects, method, name, target, root):
        response = call(rehab_client, objects, method, name, target)

        page = response.content.decode()
        assert response.status_code == 200
        assert "<html" not in page and "navbar" not in page
        assert root in page

    def test_pattern_form_invalid_values(self, rehab_client, volkov):
        make_pattern(volkov, "5/2")
        url = reverse("staff:shift_pattern", args=[volkov.pk])
        data = {
            "month": "2026-10",
            "pattern-pattern": "3/1",
            "pattern-anchor_date": "abc",
            "pattern-valid_from": "",
        }

        response = rehab_client.post(url, data, **HTMX)

        page = response.content.decode()
        assert response.status_code == 200
        assert response.context["pattern_for"] == volkov
        assert "Выберите корректный вариант" in page
        assert "Введите правильную дату" in page
        assert "Обязательное поле" in page
        assert volkov.shift_patterns.count() == 1

    def test_pattern_form_keeps_month(self, rehab_client, volkov):
        url = reverse("staff:shift_pattern", args=[volkov.pk])
        response = rehab_client.get(url, {"month": "2027-02"}, **HTMX)
        assert response.context["month_title"] == "Февраль 2027"
        assert 'name="month" value="2027-02"' in response.content.decode()

    def test_duty_add_invalid_values(self, rehab_client, volkov, slots):
        url = reverse("staff:duty_add", args=[volkov.pk])
        cases = [
            {"duty-slot": "abc", "duty-kind": "BOS", "duty-valid_from": "2026-10-05"},
            {"duty-slot": 999999, "duty-kind": "BOS", "duty-valid_from": "2026-10-05"},
            {"duty-slot": slots["9:10"].pk, "duty-kind": "CLEAR", "duty-valid_from": "2026-10-05"},
            {"duty-slot": slots["9:10"].pk, "duty-kind": "BOS", "duty-valid_from": "abc"},
            {
                "duty-slot": slots["9:10"].pk,
                "duty-kind": "BOS",
                "duty-valid_from": "2026-10-05",
                "duty-valid_to": "2026-10-01",
            },
            {
                "duty-slot": slots["9:10"].pk,
                "duty-kind": "OTHER",
                "duty-label": "   ",
                "duty-valid_from": "2026-10-05",
            },
            {},
        ]
        for data in cases:
            response = rehab_client.post(url, data, **HTMX)
            assert response.status_code == 200, data
            assert "invalid-feedback" in response.content.decode() or "alert-danger" in (
                response.content.decode()
            ), data
            assert response.context["add_form"].errors, data
        assert not InstructorDuty.objects.exists()

    def test_block_add_invalid_values(self, rehab_client, volkov, slots, ergo_session):
        url = reverse("staff:block_add", args=[volkov.pk])
        cases = [
            {"block-date": "abc", "block-slot": slots["9:10"].pk, "block-kind": "BOS"},
            {"block-date": "2099-01-10", "block-slot": 999999, "block-kind": "BOS"},
            {"block-date": "2099-01-10", "block-slot": slots["9:10"].pk, "block-kind": "NOPE"},
            {
                "block-date": "2099-01-10",
                "block-slot": slots["9:10"].pk,
                "block-kind": "GROUP_LEAD",
            },
            {
                "block-date": "2099-01-10",
                "block-slot": slots["10:30"].pk,
                "block-kind": "GROUP_LEAD",
                "block-group_session": ergo_session.pk,
            },
        ]
        for data in cases:
            response = rehab_client.post(url, data, **HTMX)
            assert response.status_code == 200, data
            assert response.context["block_form"].errors, data
        assert not InstructorBlock.objects.exists()

    def test_block_group_lead_shown_by_group_name(self, rehab_client, volkov, slots, ergo_session):
        url = reverse("staff:block_add", args=[volkov.pk])
        data = {
            "block-date": "2099-01-10",
            "block-slot": slots["9:10"].pk,
            "block-kind": "GROUP_LEAD",
            "block-group_session": ergo_session.pk,
        }
        response = rehab_client.post(url, data, **HTMX)
        assert "Эрго общая" in response.content.decode()
        assert InstructorBlock.objects.get().text == "Эрго общая"

    def test_end_duty_bad_date_and_overlap(self, rehab_client, rehab, volkov, slots):
        first = services.save_duty(
            rehab,
            InstructorDuty(instructor=volkov, slot=slots["15:00"], kind="BOS", valid_from=MON),
        )
        services.end_duty(rehab, first, MON)
        services.save_duty(
            rehab,
            InstructorDuty(
                instructor=volkov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=SAT
            ),
        )
        url = reverse("staff:duty_end", args=[first.pk])

        bad = rehab_client.post(url, {f"end{first.pk}-valid_to": "abc"}, **HTMX)
        overlap = rehab_client.post(url, {f"end{first.pk}-valid_to": "2026-10-20"}, **HTMX)

        assert bad.status_code == overlap.status_code == 200
        assert "Укажите последний день" in bad.content.decode()
        assert "уже есть «Метод. работа»" in overlap.content.decode()
        first.refresh_from_db()
        assert first.valid_to == MON

    def test_duty_edit_ignores_instructor_field(self, rehab_client, instructors, slots):
        volkov, sokolov = instructors["Волков"], instructors["Соколов"]
        duty = InstructorDuty.objects.create(
            instructor=volkov, slot=slots["15:00"], kind="BOS", valid_from=MON
        )
        rehab_client.post(
            reverse("staff:duty_edit", args=[duty.pk]),
            {
                "edit-instructor": sokolov.pk,
                "edit-slot": slots["15:00"].pk,
                "edit-kind": "BOS",
                "edit-valid_from": "2026-10-05",
            },
            **HTMX,
        )
        duty.refresh_from_db()
        assert duty.instructor == volkov

    def test_duty_edit_empty_post_keeps_form(self, rehab_client, volkov, slots):
        duty = InstructorDuty.objects.create(
            instructor=volkov, slot=slots["15:00"], kind="BOS", valid_from=MON
        )
        response = rehab_client.post(reverse("staff:duty_edit", args=[duty.pk]), {}, **HTMX)
        assert response.status_code == 200
        assert response.context["editing"] == duty

    def test_label_is_escaped(self, rehab_client, volkov, slots):
        data = {
            "duty-slot": slots["9:10"].pk,
            "duty-kind": "OTHER",
            "duty-label": "<script>alert(1)</script>",
            "duty-valid_from": "2026-10-05",
        }
        response = rehab_client.post(reverse("staff:duty_add", args=[volkov.pk]), data, **HTMX)
        page = response.content.decode()
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_kind_value_cannot_break_alpine_expression(self, rehab_client, volkov, slots):
        data = {
            "duty-slot": slots["9:10"].pk,
            "duty-kind": "x' + alert(1) + '",
            "duty-valid_from": "2026-10-05",
        }
        response = rehab_client.post(reverse("staff:duty_add", args=[volkov.pk]), data, **HTMX)

        page = response.content.decode()
        add_url = reverse("staff:duty_add", args=[volkov.pk])
        x_data = re.search(rf'hx-post="{add_url}"[^>]*?x-data="([^"]*)"', page, re.S)
        expression = html.unescape(x_data.group(1))
        # Внутри JS-строки не должно остаться неэкранированной одинарной кавычки.
        assert re.fullmatch(r"\{ kind: '(?:[^'\\]|\\.)*' \}", expression), expression

    def test_error_for_hidden_group_field_is_visible(
        self, rehab_client, volkov, slots, ergo_session
    ):
        # Пользователь выбрал «Ведение группы» и группу, затем сменил вид на БОС: скрытый
        # select всё равно отправляется — лишняя группа молча отбрасывается.
        url = reverse("staff:duty_add", args=[volkov.pk])
        data = {
            "duty-slot": slots["9:10"].pk,
            "duty-kind": "BOS",
            "duty-group_session": ergo_session.pk,
            "duty-label": "забытая подпись",
            "duty-valid_from": "2026-10-05",
        }
        rehab_client.post(url, data, **HTMX)

        duty = InstructorDuty.objects.get()
        assert (duty.kind, duty.group_session, duty.label) == ("BOS", None, "")

        # Ошибка поля, которое может быть скрыто, видна вне скрытого блока.
        data = {
            "duty-slot": slots["10:30"].pk,
            "duty-kind": "GROUP_LEAD",
            "duty-group_session": ergo_session.pk,
            "duty-valid_from": "2026-10-05",
        }
        page = rehab_client.post(url, data, **HTMX).content.decode()

        assert InstructorDuty.objects.count() == 1
        message = "не попадает в слот"
        hidden_start = page.index("x-show=\"kind === 'GROUP_LEAD'\"")
        hidden_end = page.index("x-show=\"kind === 'OTHER'\"")
        assert message in page[:hidden_start] + page[hidden_end:]


# --- Неверный ввод: чужие и несуществующие id, мусор вместо даты --------------------------


class TestBadInput:
    def test_toggle_bad_requests(self, rehab_client, volkov):
        url = reverse("staff:shift_toggle", args=[volkov.pk])
        assert rehab_client.post(url, {"day": "abc"}, **HTMX).status_code == 404
        assert rehab_client.post(url, {}, **HTMX).status_code == 404
        assert rehab_client.post(url, {"day": "2026-02-30"}, **HTMX).status_code == 404
        assert rehab_client.get(url, {"day": "2026-10-05"}).status_code == 405
        missing = reverse("staff:shift_toggle", args=[999999])
        assert rehab_client.post(missing, {"day": "2026-10-05"}, **HTMX).status_code == 404
        assert not ShiftException.objects.exists()

    def test_inactive_instructor_is_not_found(self, rehab_client, rehab, instructors, slots):
        sokolov = instructors["Соколов"]
        duty = InstructorDuty.objects.create(
            instructor=sokolov, slot=slots["15:00"], kind="BOS", valid_from=MON
        )
        block = InstructorBlock.objects.create(
            instructor=sokolov, date=FUTURE, slot=slots["9:10"], kind="BOS"
        )
        sokolov.is_active = False
        sokolov.save()

        responses = [
            rehab_client.post(
                reverse("staff:shift_toggle", args=[sokolov.pk]), {"day": "2026-10-05"}, **HTMX
            ),
            rehab_client.get(reverse("staff:shift_pattern", args=[sokolov.pk]), **HTMX),
            rehab_client.post(reverse("staff:duty_add", args=[sokolov.pk]), **HTMX),
            rehab_client.get(reverse("staff:duty_edit", args=[duty.pk]), **HTMX),
            rehab_client.post(reverse("staff:duty_end", args=[duty.pk]), **HTMX),
            rehab_client.post(reverse("staff:block_add", args=[sokolov.pk]), **HTMX),
            rehab_client.post(reverse("staff:block_delete", args=[block.pk]), **HTMX),
            rehab_client.get(reverse("staff:duties"), {"instructor": sokolov.pk}),
        ]

        assert [r.status_code for r in responses] == [404] * len(responses)
        assert InstructorBlock.objects.exists()

    @pytest.mark.parametrize(
        "name", ["staff:duty_add", "staff:block_add", "staff:shift_pattern", "staff:duty_edit"]
    )
    def test_missing_ids(self, rehab_client, instructors, name):
        assert rehab_client.post(reverse(name, args=[999999]), **HTMX).status_code == 404

    def test_post_only_actions_reject_get(self, rehab_client, objects):
        for name, target in [
            ("staff:duty_add", "instructor"),
            ("staff:duty_end", "duty"),
            ("staff:block_add", "instructor"),
            ("staff:block_delete", "block"),
        ]:
            url = reverse(name, args=[objects[target].pk])
            assert rehab_client.get(url).status_code == 405, name
        assert InstructorBlock.objects.exists()

    @pytest.mark.parametrize("value", ["abc", "-1", "", "99999999999999999999"])
    def test_duties_instructor_param(self, rehab_client, instructors, value):
        response = rehab_client.get(reverse("staff:duties"), {"instructor": value})
        assert response.status_code in (200, 404)
        if value in ("abc", "-1", ""):
            assert response.status_code == 200
            assert response.context["selected"] == instructors["Волков"]

    def test_duties_page_huge_instructor_id_is_404(self, rehab_client, instructors):
        response = rehab_client.get(reverse("staff:duties"), {"instructor": "9" * 20})
        assert response.status_code == 404


# --- seed_dev ------------------------------------------------------------------------------


class TestSeedDevShifts:
    @pytest.fixture
    def seeded(self, settings):
        settings.DEBUG = True
        call_command("seed_dev", password="x-pass-123")

    @pytest.mark.parametrize("pair", [("Волков", "Лебедева"), ("Голубев", "Белова")])
    def test_pairs_cover_every_day_exactly_once(self, seeded, pair):
        first, second = (Instructor.objects.get(short_name=name) for name in pair)
        assert first.partner_id == second.pk
        start, end = date(2026, 9, 1), date(2026, 12, 31)
        calendars = services.instructor_calendars([first, second], start, end)

        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            working = [calendars[i.pk].is_working(day) for i in (first, second)]
            assert working.count(True) == 1, (pair, day)

    def test_five_two_from_september(self, seeded):
        sokolov = Instructor.objects.get(short_name="Соколов")
        assert working_days(sokolov, date(2026, 8, 31), date(2026, 9, 7)) == [
            False,  # 31.08 — до начала шаблона
            True,
            True,
            True,
            True,
            False,  # 05.09 — суббота
            False,
            True,
        ]

    def test_rerun_keeps_edits_from_screens(self, seeded, settings):
        from apps.accounts.models import User

        user = User.objects.get(username="rehab")
        volkov = Instructor.objects.get(short_name="Волков")
        services.change_shift_pattern(user, volkov, "daily", MON, MON)
        duty = InstructorDuty.objects.get(instructor__short_name="Зайцев")
        services.end_duty(user, duty, MON)

        call_command("seed_dev", password="x-pass-123")

        assert list(volkov.shift_patterns.values_list("pattern", flat=True)) == ["2/2", "daily"]
        assert InstructorDuty.objects.filter(instructor__short_name="Зайцев").count() == 1
        duty.refresh_from_db()
        assert duty.valid_to == MON

    def test_seeded_rehab_opens_screens(self, seeded, client):
        from apps.accounts.models import User

        client.force_login(User.objects.get(username="rehab"))
        assert client.get(reverse("staff:shifts"), {"month": "2026-10"}).status_code == 200
        assert client.get(reverse("staff:duties")).status_code == 200
        client.force_login(User.objects.get(username="doctor"))
        assert client.get(reverse("staff:shifts")).status_code == 403
