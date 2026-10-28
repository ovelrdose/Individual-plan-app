"""Журнал расписания без базы (TZ.md FR-SCH-19): сравнение состояний и пояснения расчётов."""

from datetime import date, timedelta

from apps.scheduling.domain.board_day import Column, GridSlot, Member, Patient, Seat, plan_day
from apps.scheduling.domain.engine import propose
from apps.scheduling.domain.journal import INDIVIDUAL, Shown, changes, shown
from apps.scheduling.domain.model import Busy, EquipmentLoad, Kind, Need, Snapshot

D1, D2, D3, D4 = (date(2026, 10, 5) + timedelta(days=n) for n in range(4))
EQ = "eq:1"


class TestChanges:
    def test_same_change_on_consecutive_days_is_one_range(self):
        before = {(EQ, d): "13:00" for d in (D1, D2, D3)}
        after = {(EQ, d): "14:00" for d in (D1, D2, D3)}
        [change] = changes(before, after)
        assert (change.date_from, change.date_to, change.before, change.after) == (
            D1,
            D3,
            "13:00",
            "14:00",
        )

    def test_unchanged_day_and_other_change_split_ranges(self):
        before = {(EQ, D1): "13:00", (EQ, D2): "13:00", (EQ, D3): "13:00", (EQ, D4): "13:00"}
        after = {(EQ, D1): "14:00", (EQ, D2): "13:00", (EQ, D3): "14:00", (EQ, D4): "Отменён"}
        found = [(c.date_from, c.date_to, c.after) for c in changes(before, after)]
        assert found == [(D1, D1, "14:00"), (D3, D3, "14:00"), (D4, D4, "Отменён")]

    def test_nothing_changed_nothing_written(self):
        state = {(INDIVIDUAL, D1): "Волков 9:10", (EQ, D1): "13:00"}
        assert changes(state, dict(state)) == []

    def test_new_board_day_compares_with_its_source(self):
        """Перенос «как вчера» — не изменение; передача напарнику — изменение."""
        before = {(INDIVIDUAL, D1): "Волков 9:10", (EQ, D2): "13:00"}
        same = {(INDIVIDUAL, D1): "Волков 9:10", (INDIVIDUAL, D2): "Волков 9:10", (EQ, D2): "13:00"}
        assert changes(before, same, base={D2: D1}) == []
        moved = {**same, (INDIVIDUAL, D2): "Лебедева 9:10"}
        [change] = changes(before, moved, base={D2: D1})
        assert (change.before, change.after) == ("Волков 9:10", "Лебедева 9:10")

    def test_base_does_not_apply_to_equipment(self):
        before = {(EQ, D1): "13:00"}
        after = {(EQ, D1): "13:00", (EQ, D2): "13:00"}
        [change] = changes(before, after, base={D2: D1})
        assert (change.subject, change.before, change.after) == (EQ, "", "13:00")

    def test_reasons_deduplicated_in_order(self):
        before = {(EQ, D1): "", (EQ, D2): ""}
        after = {(EQ, D1): "13:00", (EQ, D2): "13:00"}
        reasons = {(EQ, D1): ["основное время 13:00"], (EQ, D2): ["основное время 13:00", "ещё"]}
        [change] = changes(before, after, reasons)
        assert change.reason == "основное время 13:00; ещё"


GRID = [GridSlot(1, 9 * 60 + 10, 9 * 60 + 40), GridSlot(2, 10 * 60, 10 * 60 + 30)]


def member(pk: int, name: str, working: bool = True, free: tuple[int, ...] = (1, 2)) -> Member:
    return Member(pk, working, frozenset(free) if working else frozenset(), name=name)


class TestBoardDayWhy:
    def test_partner_takes_over(self):
        column = Column("pair", (member(1, "Волков", working=False), member(2, "Лебедева")))
        [day] = plan_day([Patient(10, need=1, seats=(Seat(1, 1),))], [column], GRID)
        assert day.seats == (Seat(2, 1),)
        assert day.why == "Волков в 9:10 не работает — к напарнику Лебедева"

    def test_not_working_and_partner_busy(self):
        column = Column(
            "pair", (member(1, "Волков", working=False), member(2, "Лебедева", free=(2,)))
        )
        [day] = plan_day([Patient(10, need=1, seats=(Seat(1, 1),))], [column], GRID, auto=False)
        assert day.unplaced == 1
        assert "Волков 9:10: не работает, напарник Лебедева занят (распорядок)" in day.why

    def test_patient_busy_with_named_group(self):
        column = Column("one", (member(1, "Соколов"),))
        patient = Patient(
            10, need=1, seats=(Seat(1, 1),), busy=((9 * 60, 9 * 60 + 45),), busy_labels=("Эрго",)
        )
        [day] = plan_day([patient], [column], GRID, auto=False)
        assert "пациент в это время занят: Эрго" in day.why

    def test_auto_explains_choice_and_blockers(self):
        column = Column("one", (member(1, "Соколов"),))
        patient = Patient(10, need=1, busy=((9 * 60, 9 * 60 + 45),), busy_labels=("Эрго 9:00",))
        [day] = plan_day([patient], [column], GRID)
        assert day.seats == (Seat(1, 2),)
        assert day.why.startswith("автоматически к Соколов в 10:00")
        assert "раньше мешало: пациент занят: Эрго 9:00 (9:10)" in day.why

    def test_today_is_left_to_specialist(self):
        [day] = plan_day(
            [Patient(10, need=1)], [Column("one", (member(1, "Соколов"),))], GRID, auto=False
        )
        assert "ставит специалист" in day.why

    def test_no_cell_explains_why(self):
        column = Column("one", (member(1, "Соколов", free=()),))
        [day] = plan_day([Patient(10, need=1)], [column], GRID)
        assert day.unplaced == 1 and "нет подходящей ячейки" in day.why
        assert "у инструкторов занятость или не в смене: 2" in day.why

    def test_why_does_not_affect_equality(self):
        column = Column("one", (member(1, "Соколов"),))
        [day] = plan_day([Patient(10, need=1)], [column], GRID)
        assert day == type(day)(10, (Seat(1, 1),))


def equipment_need(dates: tuple[date, ...], capacity: int = 1) -> Need:
    return Need(
        prescription_id=1,
        procedure_id=1,
        kind=Kind.EQUIPMENT,
        label="Pablo",
        dates=dates,
        equipment_id=7,
        starts=(13 * 60, 13 * 60 + 15),
        duration=15,
        capacity=capacity,
    )


class TestEngineReasons:
    def test_main_time(self):
        proposal = propose(Snapshot(needs=(equipment_need((D1, D2)),)))
        assert proposal.reasons[(1, D1)].startswith("основное время 13:00 — подходит в 2 из 2 дн.")

    def test_deviation_names_what_blocked(self):
        snapshot = Snapshot(
            needs=(equipment_need((D1, D2)),),
            # Ни одно время не подходит в оба дня: основное — раннее 13:00, в D2 — замена.
            patient_busy=(
                Busy(D1, 13 * 60 + 15, 13 * 60 + 30, "Бассейн 13:15"),
                Busy(D2, 13 * 60, 13 * 60 + 15, "Эрго 13:00"),
            ),
        )
        reason = propose(snapshot).reasons[(1, D2)]
        assert reason == (
            "в основное время 13:00 пациент занят: Эрго 13:00 — замена 13:15 "
            "(свободное, ближе к основному)"
        )

    def test_unplaced_lists_each_time(self):
        snapshot = Snapshot(
            needs=(equipment_need((D1,)),),
            patient_busy=(Busy(D1, 13 * 60 + 15, 13 * 60 + 30, "Бассейн 13:15"),),
            equipment_load=(EquipmentLoad(7, D1, 13 * 60, 13 * 60 + 15),),
        )
        reason = propose(snapshot).reasons[(1, D1)]
        assert reason == (
            "не поставлен: 13:00 — мест нет (1 из 1); 13:15 — пациент занят: Бассейн 13:15"
        )


class TestShown:
    def test_only_moved_session_shown(self):
        """Два инд., перенесли одно — видно, какое (жалоба владельца: было → стало целиком)."""
        item = shown("Паршуков 10:30, Шиндер 15:40", "Паршуков 10:30, Паршуков 15:40")
        assert (item.before, item.after, item.kept) == (
            "Шиндер 15:40",
            "Паршуков 15:40",
            "Паршуков 10:30",
        )

    def test_whole_change_and_new_patient_as_is(self):
        assert shown("Волков 9:10", "Лебедева 9:10") == Shown("Волков 9:10", "Лебедева 9:10", "")
        assert shown("", "Волков 9:10") == Shown("", "Волков 9:10", "")

    def test_comma_inside_note_and_removed_session(self):
        item = shown(
            "Волков 9:10 (art, шар), Соколов 10:00", "Волков 9:10 (art, шар), Не распределены"
        )
        assert (item.before, item.after, item.kept) == (
            "Соколов 10:00",
            "Не распределены",
            "Волков 9:10 (art, шар)",
        )
