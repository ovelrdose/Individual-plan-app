"""Обновление программы из новой версии листа назначений (TZ.md FR-IMP-15, решение 69).

Сегодня в тестах — вторник 06.10.2026; курс ШРМ 4 с 05.10 по 19.10. Листы — из make_sheet
(вымышленные данные).
"""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.catalog.models import Procedure
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import (
    apply_sheet_update,
    import_prescription_sheet,
    preview_sheet_update,
)
from apps.exchange.views import PENDING_KEY
from apps.programs import services as programs
from apps.programs.models import Program, WithdrawalReason
from apps.programs.services import ProgramsError
from apps.scheduling import board_days
from apps.scheduling.models import Booking
from tests.sheets import make_sheet

pytestmark = pytest.mark.usefixtures("full_catalog")

MON, TUE, WED, THU = (date(2026, 10, d) for d in (5, 6, 7, 8))
FIRST = [
    ("05.10.26", "Групповые занятия ЛФК\n- Эрго общая\n30 мин 1 р/д", ""),
    ("05.10.26", "Имитрон 15 мин 1 р/д", ""),
    ("05.10.26", "Степпер 15 мин 1 р/д", ""),
    ("05.10.26", "Алмаг", ""),
]


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


@pytest.fixture
def program(doctor, department) -> Program:
    return import_prescription_sheet(
        doctor, department, parse_sheet(make_sheet(rows=FIRST)), attending_doctor=doctor
    )


def sheet(**kwargs):
    return parse_sheet(make_sheet(**kwargs))


def by_label(program: Program) -> dict:
    return {p.label: p for p in program.prescriptions.select_related("procedure")}


def states(update) -> dict[str, str]:
    return {line.label: line.state for line in update.lines}


class TestService:
    def test_preview_saves_nothing(self, doctor, program):
        before = list(program.prescriptions.values_list("pk", "per_day", "cancel_date"))

        update = preview_sheet_update(doctor, program, sheet(rows=FIRST[:2], room="7"))

        assert update.has_changes
        assert list(program.prescriptions.values_list("pk", "per_day", "cancel_date")) == before
        assert Program.objects.get(pk=program.pk).room == "5а"

    def test_same_changed_added_cancelled(self, doctor, program):
        old = by_label(program)
        rows = [
            FIRST[0],
            FIRST[1],
            ("05.10.26", "Степпер 15 мин 2 р/д", ""),
            ("06.10.26", "ST – 150 15 мин 1 р/д", ""),
        ]

        update = apply_sheet_update(doctor, program, sheet(rows=rows))

        assert states(update) == {
            "Эрго общая": "same",
            "Имитрон": "same",
            "Степпер": "changed",
            "st-150": "added",
            "Алмаг": "cancelled",
        }
        new = by_label(program)
        # Строки обновлены, а не пересозданы — занятия и выбор специалиста остаются.
        assert {k: new[k].pk for k in old} == {k: old[k].pk for k in old}
        assert new["Степпер"].per_day == 2
        assert new["st-150"].start_date == TUE
        assert new["Алмаг"].cancel_date == WED, "отменено с завтра, сегодня ещё идёт"
        order = [p.label for p in program.prescriptions.order_by("card_order")]
        assert order == ["Эрго общая", "Имитрон", "Степпер", "st-150", "Алмаг"]

    def test_not_started_is_deleted_already_cancelled_kept(self, doctor, department):
        rows = [
            ("05.10.26", "Имитрон", ""),
            ("08.10.26", "Степпер", ""),  # начнётся в четверг
            ("05.10.26", "Алмаг", "06.10.26"),  # отменён сегодня
        ]
        program = import_prescription_sheet(
            doctor, department, sheet(rows=rows), attending_doctor=doctor
        )

        update = apply_sheet_update(doctor, program, sheet(rows=rows[:1]))

        assert states(update) == {"Имитрон": "same", "Степпер": "deleted", "Алмаг": "kept"}
        assert set(by_label(program)) == {"Имитрон", "Алмаг"}
        assert by_label(program)["Алмаг"].cancel_date == TUE

    def test_procedure_chosen_for_unrecognized_row_is_kept(self, doctor, department):
        rows = [("05.10.26", "Непонятная процедура 15 мин", "")]
        program = import_prescription_sheet(
            doctor, department, sheet(rows=rows), attending_doctor=doctor
        )
        item = program.prescriptions.get()
        item.procedure = Procedure.objects.get(name="Имитрон", department=None)
        programs.update_prescription(doctor, item)

        update = apply_sheet_update(doctor, program, sheet(rows=rows))

        assert states(update) == {"Имитрон": "same"}
        assert program.prescriptions.get().procedure.name == "Имитрон"

    def test_sheet_overrides_web_dose(self, doctor, program):
        item = by_label(program)["Имитрон"]
        item.per_day = 2
        programs.update_prescription(doctor, item)

        update = apply_sheet_update(doctor, program, sheet(rows=FIRST))

        assert states(update)["Имитрон"] == "changed"
        assert by_label(program)["Имитрон"].per_day == 1

    def test_pinned_and_past_bookings_survive(self, doctor, program, monkeypatch):
        """Строку не пересоздают — её занятия не уходят каскадом: закреплённое остаётся,
        прошедший день не пустеет (незакреплённые группы replan подбирает заново всегда)."""
        group = by_label(program)["Эрго общая"]
        pinned = Booking.objects.filter(prescription=group, date=WED).get()
        pinned.pinned = True
        pinned.save()
        monkeypatch.setattr(board_days, "today", lambda: WED)

        apply_sheet_update(doctor, program, sheet(rows=[FIRST[0], FIRST[1]]))

        assert Booking.objects.filter(pk=pinned.pk, pinned=True).exists()
        assert Booking.objects.filter(prescription=group, date=TUE).exists()

    def test_header_and_shrm_change_end_but_not_start(self, doctor, program):
        rows = [("06.10.26", *FIRST[1][1:])]

        update = apply_sheet_update(doctor, program, sheet(rows=rows, room="7", shrm=5))

        program.refresh_from_db()
        assert {c.label: (c.old, c.new) for c in update.header} == {
            "Палата": ("5а", "7"),
            "ШРМ": ("4", "5"),
            "Окончание курса": ("19.10.2026", "24.10.2026"),
        }
        assert (program.room, program.shrm, program.start_date) == ("7", 5, MON)
        assert program.end_date == date(2026, 10, 24)
        assert any("Начало идущего курса" in w for w in program.import_warnings)

    def test_manual_end_date_is_kept(self, doctor, program):
        program.end_date = date(2026, 10, 20)
        programs.save_program(doctor, program, end_date_changed=True)

        update = apply_sheet_update(doctor, program, sheet(rows=FIRST, shrm=5))

        program.refresh_from_db()
        assert program.end_date == date(2026, 10, 20)
        assert [c.label for c in update.header] == ["ШРМ"]

    def test_other_patient_is_a_warning(self, doctor, program):
        update = preview_sheet_update(
            doctor, program, sheet(rows=FIRST, ib="999", fio="Другова Анна Сергеевна")
        )

        assert any("ИБ № 999" in w for w in update.warnings)
        assert any("Другова" in w for w in update.warnings)

    def test_rights_withdrawn_and_finished(
        self, doctor, make_user, other_department, program, monkeypatch
    ):
        stranger = make_user("stranger", (other_department, Role.REHAB))
        with pytest.raises(PermissionDenied):
            apply_sheet_update(stranger, program, sheet(rows=FIRST))

        programs.withdraw(doctor, program, TUE, WithdrawalReason.REFUSAL)
        with pytest.raises(ProgramsError, match="выбыл"):
            apply_sheet_update(doctor, program, sheet(rows=FIRST))
        programs.restore(doctor, program)

        monkeypatch.setattr(board_days, "today", lambda: date(2026, 10, 20))
        with pytest.raises(ProgramsError, match="закончился"):
            preview_sheet_update(doctor, program, sheet(rows=FIRST))


def upload(**kwargs):
    return SimpleUploadedFile("лист.docx", make_sheet(**kwargs).getvalue())


class TestScreens:
    def test_button_on_program_page(self, client, doctor, rehab, program):
        url = reverse("exchange:sheet_update", args=[program.pk])
        client.force_login(doctor)
        assert url in client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        # Специалист ФР обновляет программу из листа, как и врач (решение 75).
        client.force_login(rehab)
        assert url in client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert client.get(url).status_code == 200

    def test_upload_preview_apply(self, client, doctor, program):
        client.force_login(doctor)
        url = reverse("exchange:sheet_update", args=[program.pk])

        preview = client.post(url, {"file": upload(rows=FIRST[:3], room="7")})

        page = preview.content.decode()
        assert "Что изменится в программе" in page
        assert "отменено" in page and "Применить" in page
        assert Program.objects.get(pk=program.pk).room == "5а", "до «Применить» ничего не меняется"

        applied = client.post(
            reverse("exchange:sheet_update_apply"), {"token": preview.context["token"]}, follow=True
        )

        assert applied.redirect_chain[-1][0] == reverse("programs:detail", args=[program.pk])
        assert "Программа обновлена" in applied.content.decode()
        assert Program.objects.get(pk=program.pk).room == "7"
        assert by_label(program)["Алмаг"].cancel_date == WED
        assert client.session[PENDING_KEY] == {}

    def test_bad_file_stays_on_form(self, client, doctor, program):
        client.force_login(doctor)

        response = client.post(
            reverse("exchange:sheet_update", args=[program.pk]),
            {"file": SimpleUploadedFile("лист.doc", b"x")},
        )

        assert response.status_code == 200
        assert "в формате .docx" in response.content.decode()

    def test_from_duplicate_screen(self, client, doctor, program):
        client.force_login(doctor)

        duplicate = client.post(reverse("exchange:sheet_import"), {"file": upload(rows=FIRST[:2])})
        assert "Обновить эту программу" in duplicate.content.decode()

        preview = client.post(
            reverse("exchange:sheet_update_preview"),
            {"token": duplicate.context["token"], "program": program.pk},
        )
        assert "Что изменится в программе" in preview.content.decode()

        client.post(reverse("exchange:sheet_update_apply"), {"token": duplicate.context["token"]})
        assert Program.objects.count() == 1
        assert {"Имитрон", "Степпер", "Алмаг"} <= set(by_label(program))
        assert by_label(program)["Степпер"].cancel_date == WED

    def test_stale_token(self, client, doctor):
        client.force_login(doctor)

        for url, data in [
            (reverse("exchange:sheet_update_apply"), {"token": "нет"}),
            (reverse("exchange:sheet_update_preview"), {"token": "нет", "program": "1"}),
        ]:
            response = client.post(url, data, follow=True)
            assert "Загрузка устарела" in response.content.decode()

    def test_apply_error_is_a_message(self, client, doctor, program, monkeypatch):
        client.force_login(doctor)
        preview = client.post(
            reverse("exchange:sheet_update", args=[program.pk]), {"file": upload(rows=FIRST)}
        )
        monkeypatch.setattr(board_days, "today", lambda: date(2026, 10, 20))

        response = client.post(
            reverse("exchange:sheet_update_apply"), {"token": preview.context["token"]}, follow=True
        )

        assert "Программа не обновлена" in response.content.decode()
