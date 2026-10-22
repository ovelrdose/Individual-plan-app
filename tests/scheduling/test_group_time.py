"""Время группы на весь курс (TZ.md, решение 81): выбор на странице программы, «авто» с
предупреждением, шахматка и правка расписания групп.

Сегодня — вторник 06.10.2026, следующий будний — среда 07.10. Курс пациента 05.10–19.10.
Инструкторы вымышленные: пара 2/2 Волков / Лебедева, Соколов — 5/2. Сетка слотов: 9:10, 9:50,
10:30, 11:10 и вечер 18:00.
"""

from datetime import date, time

import pytest
from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import Preset
from apps.catalog.admin import GroupSessionAdmin
from apps.catalog.models import GroupSession, Procedure, ProcedureKind
from apps.programs.models import Prescription
from apps.programs.services import add_prescription
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

TUE, WED = date(2026, 10, 6), date(2026, 10, 7)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


@pytest.fixture
def spine(db) -> Procedure:
    """Группа ДС с двумя занятиями в день: 8:15 (до сетки инструкторов) и 11:10."""
    group = Procedure.objects.create(
        name="ДС: острая спина",
        card_label="ДС: острая спина",
        kind=ProcedureKind.DS_GROUP,
        default_duration_min=30,
    )
    for start in (time(8, 15), time(11, 10)):
        GroupSession.objects.create(procedure=group, start_time=start, duration_min=30)
    return group


def session(group: Procedure, start: time) -> GroupSession:
    return GroupSession.objects.get(procedure=group, start_time=start)


def add_group(doctor, program, group: Procedure, **fields) -> Prescription:
    return add_prescription(doctor, Prescription(program=program, procedure=group, **fields))


def starts(item: Prescription) -> set[time]:
    return set(item.bookings.values_list("start", flat=True))


def issue_codes(program) -> list[str]:
    program.refresh_from_db()
    return [issue["code"] for issue in program.schedule_issues]


def holds(program, day: date) -> int:
    entry = BoardPatient.objects.filter(program=program, date=day).first()
    return entry.unplaced if entry else 0


def no_overlaps(program) -> bool:
    """Пациент не в двух местах — проверяем и сами, а не только ограничением базы."""
    items = list(program.bookings.order_by("date", "start"))
    return all(
        not (a.date == b.date and a.start < b.end and b.start < a.end)
        for i, a in enumerate(items)
        for b in items[i + 1 :]
    )


@pytest.fixture
def seated(patient, rehab, staff, slots, doctor):
    """Пациент с индивидуальным в 11:10 у Соколова сегодня и завтра."""
    board_days.ensure_boards()
    program = patient()
    board.place_patient(rehab, program, staff["sokolov"], slots["11:10"], TUE)
    assert (
        Booking.objects.filter(
            program=program, kind=BookingKind.INDIVIDUAL, start=time(11, 10)
        ).count()
        == 2
    )
    return program


class TestAuto:
    def test_two_sessions_warn_and_take_free_time(self, seated, doctor, spine):
        item = add_group(doctor, seated, spine)

        # 11:10 занято индивидуальным — группа его обходит.
        assert starts(item) == {time(8, 15)}
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(seated)
        schedule = program_schedule(seated)
        assert not schedule.warnings  # предупреждение — в блоке «Время групп»
        row = schedule.group_times[0]
        assert row.needs_choice and row.auto == "8:15" and row.chosen_id is None

    def test_one_session_no_choice(self, make_program, doctor, procedures):
        GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 10))
        program = make_program()
        add_group(doctor, program, procedures["group"])

        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)
        assert group_time_choices(program) == []


class TestChoose:
    def test_choice_displaces_individual_today_and_tomorrow(self, seated, doctor, spine):
        item = add_group(doctor, seated, spine)

        result = choose_group_time(doctor, item, session(spine, time(11, 10)))

        item.refresh_from_db()
        assert item.group_session.start_time == time(11, 10)
        assert starts(item) == {time(11, 10)}
        assert [str(d) for d in result.displaced] == [
            "06.10: индивидуальное в 11:10 (Соколов) пересеклось с группой — пациент "
            "в «Не распределены»",
            "07.10: индивидуальное в 11:10 (Соколов) пересеклось с группой — пациент "
            "в «Не распределены»",
        ]
        assert holds(seated, TUE) == 1 and holds(seated, WED) == 1
        assert no_overlaps(seated)
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(seated)
        assert item.history.first().history_user == doctor

    def test_back_to_auto(self, make_program, doctor, spine):
        program = make_program()
        item = add_group(doctor, program, spine)
        choose_group_time(doctor, item, session(spine, time(11, 10)))

        choose_group_time(doctor, item, None)

        item.refresh_from_db()
        assert item.group_session is None
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(program)

    def test_same_choice_changes_nothing(self, make_program, doctor, spine):
        program = make_program()
        item = add_group(doctor, program, spine)
        late = session(spine, time(11, 10))
        choose_group_time(doctor, item, late)
        records = item.history.count()

        assert choose_group_time(doctor, item, late).displaced == []
        assert item.history.count() == records

    def test_rights(
        self, make_user, make_program, doctor, rehab, department, other_department, spine
    ):
        program = make_program()
        item = add_group(doctor, program, spine)
        late = session(spine, time(11, 10))
        outsider = make_user("outsider", (other_department, Preset.REHAB))
        nobody = make_user("nobody", (department, Preset.DOCTOR), rights=[])

        for user in (outsider, nobody):
            with pytest.raises(PermissionDenied):
                choose_group_time(user, item, late)
        choose_group_time(rehab, item, late)  # специалист ФР — тоже
        item.refresh_from_db()
        assert item.group_session == late

    def test_wrong_session(self, make_program, doctor, spine, procedures):
        program = make_program()
        item = add_group(doctor, program, spine)
        foreign = GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 0))
        off = GroupSession.objects.create(procedure=spine, start_time=time(15, 0), is_active=False)
        individual = add_group(doctor, program, procedures["individual"])

        for wrong in (foreign, off):
            with pytest.raises(ScheduleError, match="действующее занятие этой группы"):
                choose_group_time(doctor, item, wrong)
        with pytest.raises(ScheduleError, match="только для групп"):
            choose_group_time(doctor, individual, None)


@pytest.fixture
def pools(db) -> dict[str, Procedure]:
    generic = Procedure.objects.create(
        name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
    )
    back = Procedure.objects.create(name="Бассейн спина", card_label="Бассейн", kind="POOL")
    leg = Procedure.objects.create(name="Бассейн нога", card_label="Бассейн", kind="POOL")
    for group, start in ((back, time(9, 0)), (back, time(14, 0)), (leg, time(10, 0))):
        GroupSession.objects.create(procedure=group, start_time=start)
    return {"generic": generic, "back": back, "leg": leg}


class TestPool:
    def test_group_first_then_time_and_reset_on_new_group(self, make_program, doctor, rehab, pools):
        program = make_program()
        item = add_group(doctor, program, pools["generic"])
        with pytest.raises(ScheduleError, match="Сначала выберите группу бассейна"):
            choose_group_time(rehab, item, None)
        row = group_time_choices(program)[0]
        assert row.group_missing and not row.needs_choice

        choose_pool_group(rehab, item, pools["back"])
        choose_group_time(rehab, item, session(pools["back"], time(14, 0)))
        assert starts(item) == {time(14, 0)}

        choose_pool_group(rehab, item, pools["leg"])
        item.refresh_from_db()
        assert item.group_session is None and starts(item) == {time(10, 0)}


class TestGroupSchedule:
    def test_new_session_warns_and_time_does_not_jump(self, make_program, doctor, rehab):
        group = Procedure.objects.create(
            name="ДС: плечо", card_label="ДС: плечо", kind=ProcedureKind.DS_GROUP
        )
        GroupSession.objects.create(procedure=group, start_time=time(11, 15))
        program = make_program()
        item = add_group(doctor, program, group)
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)

        result = save_group_session(rehab, GroupSession(procedure=group, start_time=time(8, 15)))

        assert program in result.programs
        assert starts(item) == {time(11, 15)}  # более раннее занятие пациентов не перетянуло
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(program)

    def test_disabled_session_resets_choice(self, make_program, doctor, rehab, spine):
        program = make_program()
        item = add_group(doctor, program, spine)
        late = session(spine, time(11, 10))
        choose_group_time(doctor, item, late)

        late.is_active = False
        result = save_group_session(rehab, late)

        item.refresh_from_db()
        assert item.group_session is None and starts(item) == {time(8, 15)}
        assert [p.pk for p in result.reset] == [item.pk]
        assert item.history.first().history_user == rehab
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)  # выбирать больше не из чего

    def test_moved_session_keeps_choice(self, make_program, doctor, rehab, spine):
        program = make_program()
        item = add_group(doctor, program, spine)
        late = session(spine, time(11, 10))
        choose_group_time(doctor, item, late)

        late.start_time = time(11, 30)
        result = save_group_session(rehab, late)

        item.refresh_from_db()
        assert item.group_session == late and starts(item) == {time(11, 30)}
        assert result.reset == []

    def test_moved_session_displaces_individual(self, seated, doctor, rehab, spine):
        early = session(spine, time(8, 15))
        item = add_group(doctor, seated, spine)
        choose_group_time(doctor, item, early)
        session(spine, time(11, 10)).delete()  # на 11:10 переносим выбранное занятие

        early.start_time = time(11, 10)
        result = save_group_session(rehab, early)

        assert starts(item) == {time(11, 10)}
        assert [(d.program, d.date, d.start) for d in result.displaced] == [
            (seated, TUE, time(11, 10)),
            (seated, WED, time(11, 10)),
        ]
        assert no_overlaps(seated)

    @pytest.mark.parametrize("change", ["move", "disable"])
    def test_pinned_in_old_time_is_removed(self, make_program, doctor, rehab, spine, change):
        program = make_program()
        item = add_group(doctor, program, spine)
        late = session(spine, time(11, 10))
        choose_group_time(doctor, item, late)
        pinned = item.bookings.get(date=TUE)
        pinned.pinned = True
        pinned.save()

        if change == "move":
            late.start_time = time(11, 30)
        else:
            late.is_active = False
        result = save_group_session(rehab, late)

        assert [b.date for b in result.unpinned] == [TUE]
        assert not Booking.objects.filter(pk=pinned.pk).exists()
        assert Booking.history.filter(id=pinned.pk, history_type="-").exists()
        expected = time(11, 30) if change == "move" else time(8, 15)
        assert item.bookings.get(date=TUE).start == expected
        assert not item.bookings.filter(pinned=True).exists()


class TestAdmin:
    def request(self, user):
        request = RequestFactory().post("/")
        request.user = user
        return request

    def test_delete_resets_choice_and_replans(self, make_program, doctor, admin_user, spine):
        program = make_program()
        late = session(spine, time(11, 10))
        # Выбор есть, а занятий в расписании ещё нет — иначе админка удалить не даст (PROTECT).
        item = Prescription.objects.create(program=program, procedure=spine, group_session=late)

        GroupSessionAdmin(GroupSession, site).delete_model(self.request(admin_user), late)

        item.refresh_from_db()
        assert item.group_session is None
        assert item.history.first().history_user == admin_user
        assert starts(item) == {time(8, 15)}

    def test_delete_queryset(self, make_program, doctor, admin_user, spine):
        program = make_program()
        late = session(spine, time(11, 10))
        item = Prescription.objects.create(program=program, procedure=spine, group_session=late)

        GroupSessionAdmin(GroupSession, site).delete_queryset(
            self.request(admin_user), GroupSession.objects.filter(pk=late.pk)
        )

        item.refresh_from_db()
        assert item.group_session is None and starts(item) == {time(8, 15)}

    def test_inline_disable_resets_choice(self, client, make_program, doctor, admin_user, spine):
        program = make_program()
        item = add_group(doctor, program, spine)
        early, late = session(spine, time(8, 15)), session(spine, time(11, 10))
        choose_group_time(doctor, item, late)
        client.force_login(admin_user)
        data = {
            "name": spine.name,
            "card_label": spine.card_label,
            "kind": spine.kind,
            "synonyms": "",
            "default_duration_min": "30",
            "place": "",
            "is_active": "on",
            "sessions-TOTAL_FORMS": "2",
            "sessions-INITIAL_FORMS": "2",
            "sessions-MIN_NUM_FORMS": "0",
            "sessions-MAX_NUM_FORMS": "1000",
        }
        for index, item_session in enumerate((early, late)):
            prefix = f"sessions-{index}-"
            data |= {
                prefix + "id": item_session.pk,
                prefix + "procedure": spine.pk,
                prefix + "start_time": f"{item_session.start_time:%H:%M}",
                prefix + "duration_min": "30",
                prefix + "place": "",
            }
        data["sessions-0-is_active"] = "on"  # 11:10 выключаем

        response = client.post(reverse("admin:catalog_procedure_change", args=[spine.pk]), data)

        assert response.status_code == 302
        item.refresh_from_db()
        assert item.group_session is None and starts(item) == {time(8, 15)}
        assert item.history.first().history_user == admin_user


class TestBoard:
    def test_new_patient_avoids_possible_group_times(
        self, make_program, doctor, staff, spine, procedures
    ):
        """Время группы не выбрано: новое индивидуальное обходит оба занятия группы."""
        board_days.ensure_boards()
        program = make_program()
        add_group(doctor, program, spine)
        add_group(doctor, program, procedures["individual"])

        wed = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)
        assert wed.start != time(11, 10)

    def test_busy_json_follows_choice(self, seated, doctor, spine):
        """Подсветка занятости в шахматке берёт занятия из базы: новое время есть, старого нет."""
        item = add_group(doctor, seated, spine)
        choose_group_time(doctor, item, session(spine, time(11, 10)))

        busy = board.busy_json(board.occupied({seated.pk}, WED)[seated.pk])

        assert "11:10 ДС: острая спина" in busy and "8:15" not in busy


class TestProgramPage:
    def url(self, program, item) -> str:
        return reverse("scheduling:program_group_time", args=[program.pk, item.pk])

    def test_block_and_htmx_choice(self, client, seated, doctor, spine):
        item = add_group(doctor, seated, spine)
        client.force_login(doctor)

        page = client.get(reverse("programs:detail", args=[seated.pk])).content.decode()
        response = client.post(
            self.url(seated, item),
            {"session": session(spine, time(11, 10)).pk},
            HTTP_HX_REQUEST="true",
        )

        assert "Время групп" in page and "время выбрано автоматически (8:15)" in page
        assert "— авто (8:15) —" in page
        html = response.content.decode()
        assert html.lstrip().startswith('<div id="schedule"')
        assert "07.10: индивидуальное в 11:10 (Соколов)" in html
        assert "время выбрано автоматически" not in html

    def test_errors_in_fragment(self, client, make_program, doctor, spine, procedures):
        program = make_program()
        item = add_group(doctor, program, spine)
        foreign = GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 0))
        client.force_login(doctor)

        wrong = client.post(
            self.url(program, item), {"session": foreign.pk}, HTTP_HX_REQUEST="true"
        )
        garbage = client.post(self.url(program, item), {"session": "x"}, HTTP_HX_REQUEST="true")
        plain = client.post(self.url(program, item), {"session": ""})

        assert (
            wrong.status_code == 200 and "действующее занятие этой группы" in wrong.content.decode()
        )
        assert "Выберите время из списка" in garbage.content.decode()
        assert plain.status_code == 302

    def test_foreign_program_is_404(
        self, client, make_user, make_program, doctor, other_department, spine
    ):
        program = make_program()
        item = add_group(doctor, program, spine)
        client.force_login(make_user("outsider", (other_department, Preset.REHAB)))

        assert client.post(self.url(program, item), {"session": ""}).status_code == 404

    def test_no_right_is_403(self, client, make_user, make_program, doctor, department, spine):
        program = make_program()
        item = add_group(doctor, program, spine)
        client.force_login(make_user("viewer", (department, Preset.DOCTOR), rights=[]))

        response = client.post(self.url(program, item), {"session": "x"})

        assert response.status_code == 403
        assert Prescription.objects.filter(pk=item.pk, group_session=None).exists()


# --- «Кого переставить» после других действий на странице программы (решение 81) ----------

SHOULDER_TEXT = "06.10: индивидуальное в 11:10 (Соколов) пересеклось с группой"


@pytest.fixture
def shoulder(db) -> Procedure:
    """Группа ДС с единственным занятием в 11:10 — ровно на индивидуальном пациента."""
    group = Procedure.objects.create(
        name="ДС: плечо", card_label="ДС: плечо", kind=ProcedureKind.DS_GROUP
    )
    GroupSession.objects.create(procedure=group, start_time=time(11, 10))
    return group


def htmx(client, url: str, data: dict) -> str:
    response = client.post(url, data, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


class TestDisplacedEverywhere:
    def test_pool_group(self, client, seated, doctor, rehab):
        generic = Procedure.objects.create(
            name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
        )
        back = Procedure.objects.create(name="Бассейн спина", card_label="Бассейн", kind="POOL")
        GroupSession.objects.create(procedure=back, start_time=time(11, 10))
        item = add_group(doctor, seated, generic)
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("scheduling:program_pool_group", args=[seated.pk, item.pk]),
            {"group": back.pk},
        )

        assert "Переставьте в расписании" in html and SHOULDER_TEXT in html
        assert holds(seated, TUE) == 1 and no_overlaps(seated)

    def test_add_prescription(self, client, seated, doctor, shoulder):
        client.force_login(doctor)

        html = htmx(
            client,
            reverse("programs:prescription_add", args=[seated.pk]),
            {"procedure": shoulder.pk, "per_day": "1", "in_card": "on"},
        )

        assert SHOULDER_TEXT in html and "07.10: индивидуальное в 11:10" in html
        assert no_overlaps(seated)

    def test_edit_prescription(self, client, seated, doctor, shoulder, procedures):
        item = add_group(doctor, seated, procedures["card_only"])
        client.force_login(doctor)

        html = htmx(
            client,
            reverse("programs:prescription_edit", args=[seated.pk, item.pk]),
            {"edit-procedure": shoulder.pk, "edit-per_day": "1", "edit-in_card": "on"},
        )

        assert SHOULDER_TEXT in html

    def test_restore_cancelled_prescription(
        self, client, patient, rehab, staff, slots, doctor, shoulder
    ):
        board_days.ensure_boards()
        program = patient()
        # Отменена с первого дня занятий — групп нет, индивидуальное встаёт на 11:10.
        item = add_group(doctor, program, shoulder, cancel_date=TUE)
        board.place_patient(rehab, program, staff["sokolov"], slots["11:10"], TUE)
        client.force_login(doctor)

        html = htmx(
            client,
            reverse("programs:prescription_cancel", args=[program.pk, item.pk]),
            {"restore": "1"},
        )

        assert SHOULDER_TEXT in html and no_overlaps(program)

    def test_manual_restore_of_removed_group(
        self, client, patient, rehab, staff, slots, doctor, shoulder
    ):
        from apps.scheduling import manual

        board_days.ensure_boards()
        program = patient()
        item = add_group(doctor, program, shoulder)
        group = item.bookings.get(date=TUE)
        manual.remove_booking(rehab, group, group.version)
        board.place_patient(rehab, program, staff["sokolov"], slots["11:10"], TUE)
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("scheduling:booking_restore", args=[program.pk, item.pk]),
            {"date": "2026-10-06"},
        )

        assert SHOULDER_TEXT in html and holds(program, TUE) == 1
        assert no_overlaps(program)

    def test_nothing_displaced_no_block(self, client, make_program, doctor, shoulder):
        program = make_program()
        client.force_login(doctor)

        html = htmx(
            client,
            reverse("programs:prescription_add", args=[program.pk]),
            {"procedure": shoulder.pk, "per_day": "1", "in_card": "on"},
        )

        assert "Переставьте в расписании" not in html


class TestFinishedCourses:
    def test_reset_skips_finished_and_withdrawn(self, make_program, doctor, rehab, spine):
        from apps.programs.models import WithdrawalReason
        from apps.programs.services import withdraw

        late = session(spine, time(11, 10))
        current = make_program(room="1")
        finished = make_program(room="2", start_date=date(2026, 9, 1))
        left = make_program(room="3")
        items = {
            name: Prescription.objects.create(program=program, procedure=spine, group_session=late)
            for name, program in (("current", current), ("finished", finished), ("left", left))
        }
        withdraw(rehab, left, TUE, WithdrawalReason.REFUSAL)

        late.is_active = False
        result = save_group_session(rehab, late)

        assert [p.pk for p in result.reset] == [items["current"].pk]
        for name, item in items.items():
            item.refresh_from_db()
            assert (item.group_session is None) == (name == "current")


class TestNewSessionEndToEnd:
    def test_group_schedule_screen(self, client, make_program, doctor, rehab, shoulder):
        program = make_program()
        item = add_group(doctor, program, shoulder)
        client.force_login(rehab)
        url = reverse("scheduling:session_add")
        data = {"procedure": shoulder.pk, "duration_min": "30", "place": "", "is_active": "on"}

        htmx_html = htmx(client, url, data | {"start_time": "08:15"})
        plain = client.post(url, data | {"start_time": "16:00"})

        assert "Расписание пересобрано у программ (1)" in htmx_html
        assert plain.status_code == 200 and "Сохранено" in plain.content.decode()
        assert sorted(shoulder.sessions.values_list("start_time", flat=True)) == [
            time(8, 15), time(11, 10), time(16, 0),
        ]  # fmt: skip
        assert starts(item) == {time(11, 10)}  # время не прыгает
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(program)
        row = group_time_choices(program)[0]
        assert [o.text for o in row.options][:3] == ["8:15", "11:10", "16:00"]

    def test_admin_add(self, client, make_program, doctor, admin_user, shoulder):
        program = make_program()
        item = add_group(doctor, program, shoulder)
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:catalog_groupsession_add"),
            {
                "procedure": shoulder.pk,
                "start_time": "08:15",
                "duration_min": "30",
                "place": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        assert starts(item) == {time(11, 10)}
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(program)
        late = session(shoulder, time(11, 10))
        choose_group_time(doctor, item, session(shoulder, time(8, 15)))
        assert starts(item) == {time(8, 15)} and late.is_active
