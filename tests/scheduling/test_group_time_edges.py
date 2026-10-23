"""Время группы (TZ.md, решение 81): граничные случаи, которых нет в test_group_time.py.

Исходная ошибка (добавили 11:15 к 8:15 и 16:30 — выбор 11:15 не ставился), выходные и
пятница, «пациент не в двух местах» при добавлении назначения, подсветка после переноса
занятия, бассейн, права, «2 р/д» при трёх занятиях, отменённое назначение, страница
программы во всех вариантах блока и обновление строки назначения у автора.

Инструкторы вымышленные: пара 2/2 Волков / Лебедева, Соколов — 5/2. Сетка слотов: 9:10, 9:50,
10:30, 11:10 и вечер 18:00. Курс пациента 05.10–19.10.2026.
"""

from datetime import date, time

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.accounts.models import Preset
from apps.catalog.models import GroupSession, Procedure, ProcedureKind
from apps.programs.models import Prescription, WithdrawalReason
from apps.programs.services import add_prescription, withdraw
from apps.scheduling import board, board_days
from apps.scheduling.domain import IssueCode
from apps.scheduling.models import BoardPatient, Booking, BookingKind
from apps.scheduling.services import (
    ScheduleError,
    choose_group_time,
    choose_pool_group,
    group_time_choices,
    program_schedule,
    save_group_session,
)

MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
FRI, SAT, NEXT_MON = date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 12)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """Сегодня — вторник 06.10; тест может сдвинуть дату через ``today.set``."""

    class Today:
        value = TUE

        def set(self, day: date) -> None:
            self.value = day

    current = Today()
    monkeypatch.setattr(board_days, "today", lambda: current.value)
    return current


def make_group(name: str, *starts: time, kind: str = ProcedureKind.DS_GROUP) -> Procedure:
    group = Procedure.objects.create(name=name, card_label=name, kind=kind, default_duration_min=30)
    for start in starts:
        GroupSession.objects.create(procedure=group, start_time=start, duration_min=30)
    return group


def session(group: Procedure, start: time) -> GroupSession:
    return GroupSession.objects.get(procedure=group, start_time=start)


def add_group(user, program, group: Procedure, **fields) -> Prescription:
    return add_prescription(user, Prescription(program=program, procedure=group, **fields))


def starts(item: Prescription) -> set[time]:
    return set(item.bookings.values_list("start", flat=True))


def per_date(item: Prescription) -> dict[date, list[time]]:
    result: dict[date, list[time]] = {}
    for day, start in item.bookings.order_by("date", "start").values_list("date", "start"):
        result.setdefault(day, []).append(start)
    return result


def issue_codes(program) -> list[str]:
    program.refresh_from_db()
    return [issue["code"] for issue in program.schedule_issues]


def holds(program, day: date) -> int:
    entry = BoardPatient.objects.filter(program=program, date=day).first()
    return entry.unplaced if entry else 0


def no_overlaps(program) -> bool:
    items = list(program.bookings.order_by("date", "start"))
    return all(
        not (a.date == b.date and a.start < b.end and b.start < a.end)
        for i, a in enumerate(items)
        for b in items[i + 1 :]
    )


def individual(program, day: date) -> list[time]:
    return sorted(
        Booking.objects.filter(program=program, date=day, kind=BookingKind.INDIVIDUAL).values_list(
            "start", flat=True
        )
    )


# --- 1. Исходная ошибка: новое занятие 11:15 у «ДС: острая спина» -------------------------


class TestOriginalBug:
    def test_added_session_can_be_chosen_for_whole_course(self, make_program, doctor, rehab):
        """У «ДС: острая спина» 8:15 и 16:30, добавили 11:15 — выбор 11:15 ставит занятие
        в 11:15 во все дни занятий курса (раньше подбор всегда брал самое раннее)."""
        spine = make_group("ДС: острая спина", time(8, 15), time(16, 30))
        program = make_program()
        item = add_group(doctor, program, spine)
        assert starts(item) == {time(8, 15)}

        save_group_session(rehab, GroupSession(procedure=spine, start_time=time(11, 15)))
        assert starts(item) == {time(8, 15)}  # без выбора время не прыгает
        choose_group_time(doctor, item, session(spine, time(11, 15)))

        days = per_date(item)
        therapy = [day for day in program.course_dates() if program.is_therapy_day(day)]
        assert sorted(days) == therapy
        assert all(value == [time(11, 15)] for value in days.values())
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)

    def test_earlier_session_moves_nobody(self, make_program, doctor, rehab):
        """Добавили более раннее занятие: ни выбравший 16:30, ни стоящий автоматически
        на 8:15 не переезжают на 7:30."""
        spine = make_group("ДС: острая спина", time(8, 15), time(16, 30))
        chosen = add_group(doctor, make_program(room="1"), spine)
        auto = add_group(doctor, make_program(room="2", history_number="2"), spine)
        choose_group_time(doctor, chosen, session(spine, time(16, 30)))

        result = save_group_session(rehab, GroupSession(procedure=spine, start_time=time(7, 30)))

        assert {p.pk for p in result.programs} == {chosen.program_id, auto.program_id}
        assert starts(chosen) == {time(16, 30)}
        assert starts(auto) == {time(8, 15)}
        assert result.reset == [] and result.unpinned == [] and result.displaced == []
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(auto.program)
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(chosen.program)

    def test_choice_survives_unrelated_replan(self, make_program, doctor, procedures):
        """Правка другого назначения пересобирает программу — выбор остаётся."""
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15), time(16, 30))
        program = make_program()
        item = add_group(doctor, program, spine)
        choose_group_time(doctor, item, session(spine, time(16, 30)))

        GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 0))
        add_group(doctor, program, procedures["group"])

        item.refresh_from_db()
        assert item.group_session.start_time == time(16, 30)
        assert starts(item) == {time(16, 30)}


# --- 2. «Пациент не в двух местах» --------------------------------------------------------


@pytest.fixture
def seat(patient, rehab, staff, slots):
    """seat(day): пациент с индивидуальным в 11:10 у Соколова на ``day`` и следующей
    шахматке (правка сегодняшней переходит на следующий будний день)."""

    def make(day: date, room: str = "9"):
        board_days.ensure_boards()
        program = patient(room=room)
        Booking.objects.filter(program=program, kind=BookingKind.INDIVIDUAL).delete()
        BoardPatient.objects.filter(program=program).delete()
        board.place_patient(rehab, program, staff["sokolov"], slots["11:10"], day)
        return program

    return make


class TestNotInTwoPlaces:
    def test_friday_goes_to_monday(self, today, seat, doctor):
        today.set(FRI)
        program = seat(FRI)
        assert individual(program, FRI) == [time(11, 10)]
        assert individual(program, NEXT_MON) == [time(11, 10)]
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 10))
        item = add_group(doctor, program, spine)

        result = choose_group_time(doctor, item, session(spine, time(11, 10)))

        assert [(d.date, d.start) for d in result.displaced] == [
            (FRI, time(11, 10)),
            (NEXT_MON, time(11, 10)),
        ]
        assert holds(program, FRI) == 1 and holds(program, NEXT_MON) == 1
        assert individual(program, FRI) == [] and individual(program, NEXT_MON) == []
        assert no_overlaps(program)

    def test_saturday_only_monday(self, today, seat, doctor):
        today.set(SAT)
        program = seat(NEXT_MON)
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 10))
        item = add_group(doctor, program, spine)

        result = choose_group_time(doctor, item, session(spine, time(11, 10)))

        assert [(d.date, d.start) for d in result.displaced] == [(NEXT_MON, time(11, 10))]
        assert holds(program, NEXT_MON) == 1
        assert no_overlaps(program)
        # В выходной группа стоит в выбранное время, шахматки нет — индивидуальных нет.
        assert per_date(item)[SAT] == [time(11, 10)]

    def test_new_single_session_group_displaces_individual(self, seat, doctor):
        """Добавили назначение группы с единственным занятием 11:10 — группа встаёт в своё
        время, индивидуальное сегодня и завтра уходит в «Не распределены»."""
        program = seat(TUE)
        group = make_group("ДС: плечо", time(11, 10))

        item = add_group(doctor, program, group)

        assert starts(item) == {time(11, 10)}
        assert holds(program, TUE) == 1 and holds(program, WED) == 1
        assert no_overlaps(program)

    def test_auto_group_avoids_individual(self, seat, doctor):
        """Время не выбрано, есть 9:10 и 11:10: группа обходит индивидуальное в 11:10."""
        program = seat(TUE)
        group = make_group("ДС: плечо", time(11, 10), time(9, 10))

        item = add_group(doctor, program, group)

        assert starts(item) == {time(9, 10)}
        assert individual(program, TUE) == [time(11, 10)]
        assert holds(program, TUE) == 0

    def test_database_has_no_overlaps_for_all_patients(self, seat, doctor):
        programs = [seat(TUE, room=str(room)) for room in (1, 2)]
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 10))
        items = [add_group(doctor, program, spine) for program in programs]

        for item in items:
            choose_group_time(doctor, item, session(spine, time(11, 10)))

        assert all(no_overlaps(program) for program in programs)
        assert all(holds(program, TUE) == 1 for program in programs)


# --- 3. Подсветка в шахматке ---------------------------------------------------------------


class TestHighlight:
    def test_busy_json_follows_moved_session(self, seat, doctor, rehab):
        program = seat(TUE)
        spine = make_group("ДС: острая спина", time(8, 15), time(9, 10))
        item = add_group(doctor, program, spine)
        late = session(spine, time(9, 10))
        choose_group_time(doctor, item, late)
        assert "9:10" in board.busy_json(board.occupied({program.pk}, TUE)[program.pk])

        late.start_time = time(9, 50)
        save_group_session(rehab, late)

        busy = board.busy_json(board.occupied({program.pk}, TUE)[program.pk])
        assert "9:50 ДС: острая спина" in busy and "9:10 ДС" not in busy

    def test_busy_json_after_back_to_auto(self, seat, doctor):
        program = seat(TUE)
        spine = make_group("ДС: острая спина", time(8, 15), time(9, 10))
        item = add_group(doctor, program, spine)
        choose_group_time(doctor, item, session(spine, time(9, 10)))
        choose_group_time(doctor, item, session(spine, time(8, 15)))

        busy = board.busy_json(board.occupied({program.pk}, WED)[program.pk])

        assert "8:15 ДС: острая спина" in busy and "9:10" not in busy


# --- 5. Бассейн без группы -----------------------------------------------------------------


@pytest.fixture
def pools(db) -> dict[str, Procedure]:
    generic = Procedure.objects.create(
        name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
    )
    back = make_group("Бассейн спина", time(9, 0), time(14, 0), kind=ProcedureKind.POOL)
    leg = make_group("Бассейн нога", time(10, 0), time(15, 0), kind=ProcedureKind.POOL)
    return {"generic": generic, "back": back, "leg": leg}


class TestPool:
    def test_removing_group_resets_time(self, make_program, doctor, rehab, pools):
        program = make_program()
        item = add_group(doctor, program, pools["generic"])
        choose_pool_group(rehab, item, pools["back"])
        choose_group_time(rehab, item, session(pools["back"], time(14, 0)))

        choose_pool_group(rehab, item, None)

        item.refresh_from_db()
        assert item.pool_group is None and item.group_session is None
        assert not item.bookings.exists()
        row = group_time_choices(program)[0]
        assert row.group_missing and row.options == []

    def test_other_pool_group_time_is_rejected(self, client, make_program, doctor, rehab, pools):
        program = make_program()
        item = add_group(doctor, program, pools["generic"])
        choose_pool_group(rehab, item, pools["back"])
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:program_group_time", args=[program.pk, item.pk]),
            {"session": session(pools["leg"], time(15, 0)).pk},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert "действующее занятие этой группы" in response.content.decode()
        item.refresh_from_db()
        assert item.group_session is None

    def test_same_group_keeps_time(self, make_program, doctor, rehab, pools):
        program = make_program()
        item = add_group(doctor, program, pools["generic"])
        choose_pool_group(rehab, item, pools["back"])
        late = session(pools["back"], time(14, 0))
        choose_group_time(rehab, item, late)

        choose_pool_group(rehab, item, pools["back"])

        item.refresh_from_db()
        assert item.group_session == late and starts(item) == {time(14, 0)}

    def test_pool_block_renders_for_doctor_and_rehab(
        self, client, make_program, doctor, rehab, pools
    ):
        """Врач группу бассейна не выбирает (нет права расписания), но время выбирает."""
        program = make_program()
        item = add_group(doctor, program, pools["generic"])
        url = reverse("programs:detail", args=[program.pk])

        client.force_login(doctor)
        page = client.get(url).content.decode()
        assert "группа бассейна не выбрана — выбирает специалист ФР" in page

        choose_pool_group(rehab, item, pools["back"])
        page = client.get(url).content.decode()
        assert "Бассейн спина" in page
        assert reverse("scheduling:program_group_time", args=[program.pk, item.pk]) in page
        assert reverse("scheduling:program_pool_group", args=[program.pk, item.pk]) not in page

        client.force_login(rehab)
        page = client.get(url).content.decode()
        assert reverse("scheduling:program_pool_group", args=[program.pk, item.pk]) in page


# --- 6. Права ------------------------------------------------------------------------------


class TestRights:
    @pytest.fixture
    def item(self, make_program, doctor):
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15))
        return add_group(doctor, make_program(), spine)

    def test_doctor_cannot_after_withdrawal_rehab_can(self, item, doctor, rehab):
        program = item.program
        withdraw(rehab, program, WED, WithdrawalReason.EARLY_DISCHARGE)
        late = GroupSession.objects.get(start_time=time(11, 15))

        with pytest.raises(PermissionDenied):
            choose_group_time(doctor, item, late)
        choose_group_time(rehab, item, late)

        item.refresh_from_db()
        assert item.group_session == late
        # После выбытия занятий нет — выбор их не возвращает.
        assert not item.bookings.filter(date__gte=WED).exists()

    def test_schedule_only_right_is_enough(self, item, make_user, department):
        planner = make_user("planner", (department, Preset.DOCTOR), rights=["schedule"])
        late = GroupSession.objects.get(start_time=time(11, 15))

        choose_group_time(planner, item, late)

        item.refresh_from_db()
        assert item.group_session == late

    def test_view_matrix(self, client, item, make_user, department, other_department, rehab):
        url = reverse("scheduling:program_group_time", args=[item.program_id, item.pk])
        late = GroupSession.objects.get(start_time=time(11, 15))
        data = {"session": late.pk}

        assert client.post(url, data).status_code == 302  # без входа — на вход
        item.refresh_from_db()
        assert item.group_session is None

        client.force_login(make_user("viewer", (department, Preset.DOCTOR), rights=[]))
        assert client.post(url, data, HTTP_HX_REQUEST="true").status_code == 403

        client.force_login(make_user("outsider", (other_department, Preset.REHAB)))
        assert client.post(url, data, HTTP_HX_REQUEST="true").status_code == 404

        client.force_login(rehab)
        assert client.get(url).status_code == 405
        assert client.post(url, data, HTTP_HX_REQUEST="true").status_code == 200
        item.refresh_from_db()
        assert item.group_session == late

    def test_foreign_prescription_in_url_is_404(self, client, item, make_program, doctor):
        other = make_program(room="2", history_number="2")
        client.force_login(doctor)
        url = reverse("scheduling:program_group_time", args=[other.pk, item.pk])

        assert client.post(url, {"session": ""}, HTTP_HX_REQUEST="true").status_code == 404

    def test_viewer_sees_chosen_time_without_form(self, client, item, make_user, department):
        choose_group_time(item.program.attending_doctor, item, None)
        late = GroupSession.objects.get(start_time=time(11, 15))
        Prescription.objects.filter(pk=item.pk).update(group_session=late)
        client.force_login(make_user("viewer", (department, Preset.DOCTOR), rights=[]))

        page = client.get(reverse("programs:detail", args=[item.program_id])).content.decode()

        assert "Время групп" in page and "в 11:15" in page
        assert reverse("scheduling:program_group_time", args=[item.program_id, item.pk]) not in (
            page
        )


# --- 7. «2 р/д», отменённое назначение, программа без шахматок -----------------------------


class TestPerDayAndCancelled:
    def test_twice_a_day_three_sessions(self, make_program, doctor):
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15), time(16, 30))
        program = make_program()
        item = add_group(doctor, program, spine, per_day=2)
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(program)
        row = group_time_choices(program)[0]
        assert row.needs_choice and len(row.options) == 3

        choose_group_time(doctor, item, session(spine, time(16, 30)))

        days = per_date(item)
        assert days and all(len(value) == 2 and time(16, 30) in value for value in days.values())
        assert no_overlaps(program)
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)
        assert not group_time_choices(program)[0].needs_choice

    def test_twice_a_day_two_sessions_no_block(self, make_program, doctor):
        spine = make_group("ДС: острая спина", time(8, 15), time(16, 30))
        program = make_program()
        add_group(doctor, program, spine, per_day=2)

        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)
        assert group_time_choices(program) == []

    def test_cancelled_prescription_not_in_block(self, make_program, doctor):
        spine = make_group("ДС: острая спина", time(8, 15), time(16, 30))
        program = make_program()
        add_group(doctor, program, spine, cancel_date=TUE)

        assert group_time_choices(program) == []
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)

    def test_cancelled_later_keeps_choice_before_cancel(self, make_program, doctor):
        spine = make_group("ДС: острая спина", time(8, 15), time(16, 30))
        program = make_program()
        item = add_group(doctor, program, spine, cancel_date=date(2026, 10, 9))

        choose_group_time(doctor, item, session(spine, time(16, 30)))

        assert sorted(per_date(item)) == [TUE, WED, date(2026, 10, 8)]
        assert starts(item) == {time(16, 30)}

    def test_program_without_boards(self, make_program, doctor):
        """Шахматок нет (нет инструкторов и сетки) — выбор работает, вытесненных нет."""
        spine = make_group("ДС: острая спина", time(8, 15), time(16, 30))
        program = make_program()
        item = add_group(doctor, program, spine)

        result = choose_group_time(doctor, item, session(spine, time(16, 30)))

        assert result.displaced == []
        assert starts(item) == {time(16, 30)}

    def test_not_a_group_prescription(self, make_program, doctor, procedures):
        program = make_program()
        item = add_group(doctor, program, procedures["card_only"])

        with pytest.raises(ScheduleError, match="только для групп"):
            choose_group_time(doctor, item, None)


# --- 10. Строка назначения у автора выбора -------------------------------------------------


class TestAuthorSeesRow:
    def test_htmx_response_updates_prescription_row(self, client, make_program, doctor):
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15))
        program = make_program()
        item = add_group(doctor, program, spine)
        client.force_login(doctor)

        response = client.post(
            reverse("scheduling:program_group_time", args=[program.pk, item.pk]),
            {"session": session(spine, time(11, 15)).pk},
            HTTP_HX_REQUEST="true",
            HTTP_X_LIVE_CLIENT="tab-1",
        )

        html = response.content.decode()
        assert 'id="prescriptions"' in html and "hx-swap-oob" in html
        assert "в 11:15" in html.split('id="prescriptions"', 1)[1]

    def test_pool_group_change_updates_prescription_row(
        self, client, make_program, doctor, rehab, pools
    ):
        program = make_program()
        item = add_group(doctor, program, pools["generic"])
        choose_pool_group(rehab, item, pools["back"])
        choose_group_time(rehab, item, session(pools["back"], time(14, 0)))
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:program_pool_group", args=[program.pk, item.pk]),
            {"group": pools["leg"].pk},
            HTTP_HX_REQUEST="true",
        )

        assert 'id="prescriptions"' in response.content.decode()

    def test_page_reload_shows_row(self, client, make_program, doctor):
        """После перезагрузки страницы строка показывает выбранное время."""
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15))
        program = make_program()
        item = add_group(doctor, program, spine)
        choose_group_time(doctor, item, session(spine, time(11, 15)))
        client.force_login(doctor)

        page = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        table = page.split('id="prescriptions"', 1)[1]

        assert "в 11:15" in table


# --- 11. Страница программы: все варианты блока ---------------------------------------------


class TestPageVariants:
    def test_all_rows_render(self, client, make_program, doctor, rehab, pools, procedures):
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15))
        single = make_group("ДС: плечо", time(10, 0))
        program = make_program()
        auto = add_group(doctor, program, spine)
        back = make_group("ДС: спина", time(9, 0), time(15, 0))
        chosen = add_group(doctor, program, back)
        add_group(doctor, program, single)
        pool_missing = add_group(doctor, program, pools["generic"])
        pool_set = add_group(doctor, program, pools["generic"])
        choose_pool_group(rehab, pool_set, pools["leg"])
        choose_group_time(doctor, chosen, session(back, time(15, 0)))

        rows = {row.prescription.pk: row for row in program_schedule(program).group_times}
        assert set(rows) == {auto.pk, chosen.pk, pool_missing.pk, pool_set.pk}
        assert rows[auto.pk].needs_choice and rows[auto.pk].auto == "8:15"
        assert rows[chosen.pk].chosen_id and not rows[chosen.pk].needs_choice
        assert rows[pool_missing.pk].group_missing
        assert rows[pool_set.pk].needs_choice  # 10:00 и 15:00 у группы бассейна

        for user in (doctor, rehab):
            client.force_login(user)
            response = client.get(reverse("programs:detail", args=[program.pk]))
            assert response.status_code == 200
            html = response.content.decode()
            assert 'id="group-times"' in html
            assert html.count("время выбрано автоматически") == 2

        response = client.post(
            reverse("scheduling:program_group_time", args=[program.pk, auto.pk]),
            {"session": ""},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        assert 'id="group-times"' in response.content.decode()

    def test_withdrawn_program_page(self, client, make_program, doctor, rehab):
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15))
        program = make_program()
        add_group(doctor, program, spine)
        withdraw(rehab, program, WED, WithdrawalReason.EARLY_DISCHARGE)

        for user in (doctor, rehab):
            client.force_login(user)
            assert client.get(reverse("programs:detail", args=[program.pk])).status_code == 200

    def test_plain_post_redirects_with_notice(self, client, seat, doctor):
        """Без JavaScript: перенаправление на страницу программы, вытесненные — сообщением."""
        program = seat(TUE)
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 10))
        item = add_group(doctor, program, spine)
        client.force_login(doctor)

        response = client.post(
            reverse("scheduling:program_group_time", args=[program.pk, item.pk]),
            {"session": session(spine, time(11, 10)).pk},
            follow=True,
        )

        assert response.status_code == 200
        assert "06.10: индивидуальное в 11:10 (Соколов)" in response.content.decode()


# --- Экран «Групповые занятия» и админка: итог правки занятия -----------------------------


def htmx_delete(client, item: GroupSession) -> str:
    """«Удалить» использованное время на экране — архив (решение 82)."""
    response = client.post(
        reverse("scheduling:session_delete", args=[item.pk]),
        {"confirm": "1"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    return response.content.decode()


class TestGroupScheduleScreen:
    def edit(self, client, item: GroupSession, **changes) -> str:
        data = {
            "edit-start_time": f"{item.start_time:%H:%M}",
            "edit-duration_min": str(item.duration_min),
            "edit-place": item.place,
        }
        data |= changes
        data = {key: value for key, value in data.items() if value is not None}
        response = client.post(
            reverse("scheduling:session_edit", args=[item.pk]), data, HTTP_HX_REQUEST="true"
        )
        assert response.status_code == 200
        return response.content.decode()

    def test_disable_shows_reset(self, client, make_program, doctor, rehab):
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15))
        program = make_program(full_name="Сбросов Тест Тестович")
        item = add_group(doctor, program, spine)
        late = session(spine, time(11, 15))
        choose_group_time(doctor, item, late)
        client.force_login(rehab)

        html = htmx_delete(client, late)

        assert "Выбор времени сброшен" in html and "Сбросов" in html
        item.refresh_from_db()
        assert item.group_session is None and starts(item) == {time(8, 15)}
        assert item.history.first().history_user == rehab

    def test_move_shows_unpinned_and_displaced(self, client, seat, doctor, rehab):
        program = seat(TUE)
        spine = make_group("ДС: острая спина", time(8, 15), time(10, 30))
        item = add_group(doctor, program, spine)
        late = session(spine, time(10, 30))
        choose_group_time(doctor, item, late)
        Booking.objects.filter(prescription=item, date=WED).update(pinned=True)
        client.force_login(rehab)

        html = self.edit(client, late, **{"edit-start_time": "11:10"})

        assert "Сняты закреплённые в старое время" in html
        assert "Индивидуальные в «Не распределены»" in html
        assert starts(item) == {time(11, 10)}
        assert holds(program, TUE) == 1 and holds(program, WED) == 1
        assert no_overlaps(program)

    def test_doctor_cannot_edit_sessions(self, client, doctor):
        spine = make_group("ДС: острая спина", time(8, 15))
        client.force_login(doctor)

        response = client.post(
            reverse("scheduling:session_edit", args=[session(spine, time(8, 15)).pk]),
            {"edit-start_time": "9:00", "edit-duration_min": "30"},
        )

        assert response.status_code == 403
        assert session(spine, time(8, 15))


class TestAdminMoveToOtherGroup:
    def test_session_moved_to_other_group_resets_choice(
        self, client, make_program, doctor, admin_user
    ):
        spine = make_group("ДС: острая спина", time(8, 15), time(11, 15))
        other = make_group("ДС: плечо", time(9, 0))
        program = make_program()
        item = add_group(doctor, program, spine)
        late = session(spine, time(11, 15))
        choose_group_time(doctor, item, late)
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:catalog_groupsession_change", args=[late.pk]),
            {
                "procedure": other.pk,
                "start_time": "11:15",
                "duration_min": "30",
                "place": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        item.refresh_from_db()
        assert item.group_session is None
        assert starts(item) == {time(8, 15)}
        assert item.history.first().history_user == admin_user
