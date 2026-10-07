"""Подбор групп ДС (TZ.md, FR-SCH-2): как группы ЛФК и бассейн — по расписанию групп."""

from datetime import date

from apps.scheduling.domain.engine import propose
from apps.scheduling.domain.model import Kind, Need, Session, Snapshot

DAYS = (date(2026, 10, 6), date(2026, 10, 7))


def need(pk: int, kind: Kind, *sessions: Session, per_day: int = 1) -> Need:
    return Need(pk, pk, kind, f"группа {pk}", DAYS, sessions=sessions, per_day=per_day)


def times(proposal, prescription_id: int) -> set[int]:
    return {p.start for p in proposal.placements if p.prescription_id == prescription_id}


def test_ds_group_avoids_lfk_group_time():
    """ДС «спина» 9:00 пересекается с ЛФК 9:10 — подбор берёт второе занятие, 15:45."""
    lfk = need(1, Kind.LFK_GROUP, Session(10, 550, 580))
    ds = need(2, Kind.DS_GROUP, Session(20, 540, 570), Session(21, 945, 975))

    proposal = propose(Snapshot((ds, lfk)))

    assert times(proposal, 1) == {550}
    assert times(proposal, 2) == {945}
    assert {p.kind for p in proposal.placements if p.prescription_id == 2} == {Kind.DS_GROUP}
    assert not proposal.issues


def test_ds_group_twice_a_day():
    ds = need(2, Kind.DS_GROUP, Session(20, 540, 570), Session(21, 945, 975), per_day=2)

    proposal = propose(Snapshot((ds,)))

    assert len(proposal.placements) == 4
    assert times(proposal, 2) == {540, 945}
