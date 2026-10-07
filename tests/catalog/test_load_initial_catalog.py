from datetime import time
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from openpyxl import Workbook

from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure, ProcedureKind
from apps.catalog.schedule_file import ScheduleFileError, read_schedule
from apps.catalog.services import UnknownGroupError, load_initial_catalog
from apps.org.models import Department

PRIMARY_DOCS = Path(settings.BASE_DIR) / "primary_docs"
LFK = PRIMARY_DOCS / "lfk.xlsx"
POOL = PRIMARY_DOCS / "basseyn.xlsx"
DS = PRIMARY_DOCS / "Gruppy_DS.xlsx"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not LFK.exists(), reason="нет файлов заказчика"),
]


def make_xlsx(path: Path, rows: list[tuple]) -> Path:
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    workbook.save(path)
    return path


def starts(name: str) -> list[time]:
    return list(
        GroupSession.objects.filter(procedure__name=name)
        .order_by("start_time")
        .values_list("start_time", flat=True)
    )


@pytest.fixture
def loaded():
    return load_initial_catalog(LFK, POOL)


def test_department(loaded):
    assert Department.objects.get(code="omr4").name == "ОМР № 4"


def test_lfk_groups(loaded):
    assert Procedure.objects.filter(kind=ProcedureKind.LFK_GROUP).count() == 9
    assert starts("Эрго общая") == [time(9, 10)]
    assert starts("Нейро-тренинг") == [time(10, 30), time(14, 20)]

    ergo = Procedure.objects.get(name="Эрготерапия")
    assert ergo.card_label == "Эрготренинг"
    assert ergo.place == "эргозона"


def test_pool_groups(loaded):
    assert starts("ЛФК в воде: нижняя конечность") == [
        time(9, 0),
        time(9, 45),
        time(14, 15),
        time(15, 40),
    ]
    assert starts("ЛФК в воде: спина") == [time(10, 30), time(13, 30), time(15, 0)]
    unspecified = Procedure.objects.get(name="Бассейн")
    assert unspecified.kind == ProcedureKind.POOL
    assert not unspecified.sessions.exists()


def test_equipment_and_other_procedures(loaded):
    assert set(Equipment.objects.values_list("name", flat=True)) == {"st-150", "Имитрон", "Pablo"}
    st = Procedure.objects.get(name="st-150")
    assert st.kind == ProcedureKind.EQUIPMENT
    assert st.equipment.name == "st-150"
    assert "ST – 150" in st.synonyms

    individual = Procedure.objects.get(kind=ProcedureKind.INDIVIDUAL)
    assert (individual.card_label, individual.default_duration_min) == ("Инд.занятие", 30)
    assert Procedure.objects.filter(kind=ProcedureKind.CARD_ONLY).count() == 11
    evening = Procedure.objects.filter(evening_individual=True)
    assert set(evening.values_list("name", flat=True)) == {"Мото-Л", "Артромот"}

    for item in Procedure.objects.all():
        item.full_clean()


def test_slots(loaded):
    assert InstructorSlot.objects.count() == 13
    assert InstructorSlot.objects.filter(is_evening=True).count() == 3
    assert str(InstructorSlot.objects.first()) == "09:10–09:40"


def test_idempotent_and_keeps_manual_edits(loaded):
    Procedure.objects.filter(name="Лого-тренинг").update(place="кабинет логопеда")

    report = load_initial_catalog(LFK, POOL)

    assert sum(report.created.values()) == 0
    assert GroupSession.objects.count() == 18
    assert Procedure.objects.get(name="Лого-тренинг").place == "кабинет логопеда"


def test_unknown_group(tmp_path):
    lfk = make_xlsx(tmp_path / "lfk.xlsx", [(time(9, 10), "Эрго - общая"), (time(12, 0), "Йога")])

    with pytest.raises(UnknownGroupError, match="A2 «Йога»"):
        load_initial_catalog(lfk, POOL)
    assert not Procedure.objects.exists(), "при ошибке ничего не сохраняется"


def test_command(capsys):
    call_command("load_initial_catalog")

    # 10 ЛФК + 8 бассейна + 11 дневного стационара.
    assert "занятия групп: создано 29" in capsys.readouterr().out


def test_command_reports_errors(tmp_path):
    with pytest.raises(CommandError):
        call_command("load_initial_catalog", lfk=tmp_path / "missing.xlsx")


class TestDayHospital:
    """Группы дневного стационара из Gruppy_DS.xlsx (FR-CAT-6)."""

    def test_groups_sessions_and_places(self, tmp_path):
        ds = make_xlsx(
            tmp_path / "ds.xlsx",
            [
                (time(8, 15), "острая спина", "181 кабинет"),
                (time(9, 0), " спина", "181 кабинет"),
                (time(15, 0), "коленный сустав", "179кабинет"),
                (time(15, 45), "спина", "181 кабинет"),
                (time(17, 15), "ГСС", None),
            ],
        )

        load_initial_catalog(LFK, POOL, ds)

        groups = Procedure.objects.filter(kind=ProcedureKind.DS_GROUP)
        assert set(groups.values_list("name", flat=True)) == {
            "ДС: острая спина", "ДС: спина", "ДС: плечо", "ДС: коленный сустав",
            "ДС: ШОП", "ДС: ТБС", "ДС: ГСС",
        }  # fmt: skip
        assert all(group.department is None for group in groups), "общие для центра"
        assert starts("ДС: спина") == [time(9, 0), time(15, 45)]
        knee = GroupSession.objects.get(procedure__name="ДС: коленный сустав")
        assert (knee.duration_min, knee.effective_place) == (30, "179 кабинет")
        assert GroupSession.objects.get(procedure__name="ДС: ГСС").effective_place == ""

    def test_without_file_no_groups(self, loaded):
        assert not Procedure.objects.filter(kind=ProcedureKind.DS_GROUP).exists()

    @pytest.mark.skipif(not DS.exists(), reason="нет файла заказчика")
    def test_customer_file(self):
        report = load_initial_catalog(LFK, POOL, DS)

        assert report.created["занятия групп"] == 29
        assert starts("ДС: острая спина") == [time(8, 15), time(16, 30)]

    def test_unknown_group(self, tmp_path):
        ds = make_xlsx(tmp_path / "ds.xlsx", [(time(9, 0), "локоть", "179 кабинет")])

        with pytest.raises(UnknownGroupError, match="«локоть»"):
            load_initial_catalog(LFK, POOL, ds)


class TestReadSchedule:
    def test_fraction_times_and_blank_rows(self, tmp_path):
        path = make_xlsx(
            tmp_path / "s.xlsx", [(0.3819444, " Эрго - общая "), (None, None), (0.5, "Йога")]
        )

        rows = read_schedule(path)

        assert [(r.start, r.name, r.cell) for r in rows] == [
            (time(9, 10), "Эрго - общая", "A1"),
            (time(12, 0), "Йога", "A3"),
        ]

    def test_place_column(self, tmp_path):
        path = make_xlsx(tmp_path / "s.xlsx", [(time(9, 0), "спина", " 179кабинет ")])

        assert read_schedule(path)[0].place == "179 кабинет"

    def test_missing_name(self, tmp_path):
        path = make_xlsx(tmp_path / "s.xlsx", [(time(9, 10), None)])

        with pytest.raises(ScheduleFileError, match="строка 1"):
            read_schedule(path)

    def test_bad_time(self, tmp_path):
        path = make_xlsx(tmp_path / "s.xlsx", [("утро", "Йога")])

        with pytest.raises(ScheduleFileError, match="A1"):
            read_schedule(path)
