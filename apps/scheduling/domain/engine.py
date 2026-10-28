"""Жадный подбор (TZ.md §7.1, FR-SCH-2): группы ЛФК и ДС, бассейн и тренажёры на весь курс.

Идея одна для всех: выбрать одно время на весь курс — то, которое подходит в наибольшее
число дней; в дни, где оно не подходит, взять замену на день (предупреждение), а если
замены нет — оставить день пустым (конфликт). Пересекать занятия пациента нельзя никогда:
это запрещено и базой данных. Индивидуальные занятия ставит шахматка на день вперёд
(``board_day.plan_day``), не этот подбор.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from .model import Issue, IssueCode, Kind, Need, Placement, Proposal, Snapshot

STEP_ORDER = (Kind.LFK_GROUP, Kind.DS_GROUP, Kind.POOL, Kind.EQUIPMENT)


@dataclass(frozen=True)
class _Option:
    start: int
    end: int
    session_id: int | None = None


def propose(snapshot: Snapshot) -> Proposal:
    state = _State(snapshot)
    for kind in STEP_ORDER:
        needs = [n for n in snapshot.needs if n.kind == kind]
        if kind == Kind.EQUIPMENT:
            for need in needs:
                state.place_equipment(need)
        else:
            # Сначала группы с меньшим выбором времени: у «I can нога» одно занятие в день,
            # у «Нейро-тренинга» два — иначе результат зависел бы от порядка строк в листе.
            # Выбранное время — такой же жёсткий выбор, как единственное занятие.
            for need in sorted(needs, key=lambda n: (_choices(n), n.prescription_id)):
                state.place_group(need)
    return state.proposal


def _choices(need: Need) -> int:
    chosen = any(s.id == need.chosen_session_id for s in need.sessions)
    return 1 if chosen else len(need.sessions)


def hhmm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


class _State:
    def __init__(self, snapshot: Snapshot):
        self.proposal = Proposal()
        self.busy: dict[date, list[tuple[int, int]]] = defaultdict(list)
        # Названия занятий ``busy`` в том же порядке — для пояснений журнала.
        self.labels: dict[date, list[str]] = defaultdict(list)
        for item in snapshot.patient_busy:
            self.busy[item.date].append((item.start, item.end))
            self.labels[item.date].append(item.label)
        self.load: dict[tuple[int, date], list[tuple[int, int]]] = defaultdict(list)
        for item in snapshot.equipment_load:
            self.load[(item.equipment_id, item.date)].append((item.start, item.end))
        self.soft: dict[date, list[tuple[int, int]]] = defaultdict(list)
        for item in snapshot.soft_busy:
            self.soft[item.date].append((item.start, item.end))

    # --- Шаги 1–2: группы ЛФК и ДС, бассейн ---------------------------------------------------

    def place_group(self, need: Need) -> None:
        if need.group_required:
            self._issue(
                IssueCode.POOL_TYPE_REQUIRED, need, "не выбрана группа бассейна — выберите её"
            )
            return
        if not need.sessions:
            self._issue(IssueCode.NO_SESSIONS, need, "у группы нет занятий в «Групповых занятиях»")
            return
        options = [
            _Option(s.start, s.end, s.id)
            for s in sorted(need.sessions, key=lambda s: (s.start, s.id))
        ]
        # «2 р/д» — каждая группа дважды (решение владельца 04.10). Одно занятие группы в день
        # дважды не посетить, поэтому больше, чем занятий в расписании, не поставить.
        units = min(need.per_day, len(options))
        if need.per_day > units:
            self._issue(
                IssueCode.GROUP_FREQUENCY,
                need,
                f"назначено {need.per_day} р/д, а в «Групповых занятиях» {_times(len(options))} "
                f"в день — поставлено {units}",
            )
        chosen = next((o for o in options if o.session_id == need.chosen_session_id), None)
        previous = {pk: rank for rank, pk in enumerate(need.previous_session_ids)}
        automatic: list[_Option] = []
        for unit in range(units):
            # Закреплённые вручную занятия занимают первые «единицы» дня.
            dates = tuple(day for day in need.dates if need.pinned_on(day) <= unit)
            # Выбранное время — основное для первого занятия дня, без сравнения (решение 81).
            # Без выбора: больше дней, меньше пересечений с индивидуальными, прежнее время,
            # затем раннее (§7.3, шаг 1). Второе занятие той же группы не совпадёт с первым:
            # пациент в это время уже занят.
            best = self._place_course(
                need, options, dates=dates, fits=self._patient_free,
                load=lambda option: 0,
                avoid=lambda option, dates=dates: (
                    self._soft_overlaps(option, dates),
                    previous.get(option.session_id, len(previous)),
                ),
                preferred=chosen if unit == 0 else None,
                unplaced=IssueCode.GROUP_OVERLAP,
            )  # fmt: skip
            if best is not None and best is not chosen:
                automatic.append(best)
        if need.choice_needed and automatic:
            starts = ", ".join(hhmm(o.start) for o in options)
            picked = ", ".join(hhmm(o.start) for o in sorted(automatic, key=lambda o: o.start))
            self._issue(
                IssueCode.GROUP_TIME_AUTO,
                need,
                f"{_times(len(options))} в день ({starts}) — время выбрано автоматически "
                f"({picked}), выберите",
            )

    def _soft_overlaps(self, option: _Option, dates: tuple[date, ...]) -> int:
        """В скольких днях занятие легло бы на индивидуальное пациента."""
        return sum(
            1
            for day in dates
            if any(start < option.end and option.start < end for start, end in self.soft[day])
        )

    # --- Шаг 3: тренажёры ------------------------------------------------------------------

    def place_equipment(self, need: Need) -> None:
        if need.unavailable:
            self._issue(IssueCode.EQUIPMENT_UNPLACED, need, need.unavailable)
            return
        options = [_Option(start, start + need.duration) for start in sorted(need.starts)]
        if not options:
            self._issue(
                IssueCode.EQUIPMENT_UNPLACED, need, "занятие не помещается в окно тренажёра"
            )
            return

        def fits(day: date, option: _Option) -> bool:
            return self._patient_free(day, option) and self._used(need, day, option) < need.capacity

        def load(option: _Option) -> int:
            return sum(self._used(need, day, option) for day in need.dates)

        for unit in range(need.per_day):
            # Закреплённые вручную занятия занимают первые «единицы» дня.
            dates = tuple(day for day in need.dates if need.pinned_on(day) <= unit)
            self._place_course(need, options, dates=dates, fits=fits, load=load,
                               unplaced=IssueCode.EQUIPMENT_UNPLACED)  # fmt: skip

    def _used(self, need: Need, day: date, option: _Option) -> int:
        return sum(
            1
            for start, end in self.load[(need.equipment_id, day)]
            if start < option.end and option.start < end
        )

    # --- Общее -----------------------------------------------------------------------------

    def _place_course(
        self,
        need: Need,
        options: list[_Option],
        *,
        dates: tuple[date, ...] | None = None,
        fits: Callable[[date, _Option], bool],
        load: Callable[[_Option], int],
        unplaced: IssueCode,
        avoid: Callable[[_Option], tuple[int, ...]] = lambda option: (),
        preferred: _Option | None = None,
    ) -> _Option | None:
        """Одно время на весь курс и замены по дням; возвращает основное время.

        ``preferred`` — время выбрано человеком: оно основное, даже если подходит не во все дни.
        ``avoid`` — что учитывать после числа дней: у групп — индивидуальные и прежнее время.
        """
        dates = need.dates if dates is None else dates
        if not options or not dates:
            return None
        if preferred is not None:
            best = preferred
        else:
            coverage = {option: sum(fits(day, option) for day in dates) for option in options}
            best = min(
                options,
                key=lambda o: (-coverage[o], *avoid(o), load(o), o.start, o.session_id or 0),
            )

        explain = need.kind == Kind.EQUIPMENT
        if explain:
            if preferred is not None:
                main = f"основное время {hhmm(best.start)} выбрано вручную"
            else:
                main = (
                    f"основное время {hhmm(best.start)} — подходит в {coverage[best]} "
                    f"из {len(dates)} дн., из таких самое свободное и раннее"
                )
        deviations: dict[int, list[date]] = defaultdict(list)
        missing: list[date] = []
        for day in dates:
            if fits(day, best):
                self._put(need, day, best)
                if explain:
                    self._reason(need, day, main)
                continue
            # Замена на день: свободное время с наименьшей загрузкой, поближе к основному.
            candidates = [o for o in options if fits(day, o)]
            if not candidates:
                missing.append(day)
                if explain:
                    self._reason(need, day, f"не поставлен: {self._why_none(need, day, options)}")
                continue
            alternative = min(
                candidates, key=lambda o: (load(o), abs(o.start - best.start), o.start)
            )
            self._put(need, day, alternative)
            deviations[alternative.start].append(day)
            if explain:
                self._reason(
                    need,
                    day,
                    f"в основное время {hhmm(best.start)} {self._why_not(need, day, best)} — "
                    f"замена {hhmm(alternative.start)} (свободное, ближе к основному)",
                )

        for start, days in sorted(deviations.items()):
            self._issue(
                IssueCode.DAY_DEVIATION,
                need,
                f"{_days(days)} — в {hhmm(start)} вместо {hhmm(best.start)}",
            )
        if missing:
            self._issue(unplaced, need, f"{_days(missing)} — нет свободного времени, не поставлено")
        return best

    def _reason(self, need: Need, day: date, text: str) -> None:
        key = (need.prescription_id, day)
        current = self.proposal.reasons.get(key)
        # «2 р/д» — пояснения обоих занятий дня через «;».
        self.proposal.reasons[key] = f"{current}; {text}" if current else text

    def _why_not(self, need: Need, day: date, option: _Option) -> str:
        """Почему время не подходит в этот день: занятие пациента или нет мест."""
        for (start, end), label in zip(self.busy[day], self.labels[day], strict=False):
            if start < option.end and option.start < end:
                return f"пациент занят: {label or 'другое занятие'}"
        if need.equipment_id is not None:
            return f"мест нет ({self._used(need, day, option)} из {need.capacity})"
        return "не подходит"

    def _why_none(self, need: Need, day: date, options: list[_Option]) -> str:
        """Почему не подошло ни одно время окна — по причинам, с временами."""
        groups: dict[str, list[str]] = defaultdict(list)
        for option in options:
            groups[self._why_not(need, day, option)].append(hhmm(option.start))
        parts = []
        for why, times in groups.items():
            shown = ", ".join(times) if len(times) <= 3 else f"{times[0]}–{times[-1]}"
            parts.append(f"{shown} — {why}")
        return "; ".join(parts)

    def _patient_free(self, day: date, option: _Option) -> bool:
        return all(end <= option.start or option.end <= start for start, end in self.busy[day])

    def _put(self, need: Need, day: date, option: _Option) -> None:
        self.busy[day].append((option.start, option.end))
        self.labels[day].append(f"{need.label} {hhmm(option.start)}")
        if need.equipment_id is not None:
            self.load[(need.equipment_id, day)].append((option.start, option.end))
        self.proposal.placements.append(
            Placement(
                prescription_id=need.prescription_id,
                procedure_id=need.procedure_id,
                kind=need.kind,
                date=day,
                start=option.start,
                end=option.end,
                session_id=option.session_id,
                equipment_id=need.equipment_id,
            )
        )

    def _issue(self, code: IssueCode, need: Need, text: str) -> None:
        issue = Issue(code, need.prescription_id, f"{need.label}: {text}.")
        # «3 р/д», а мест в окне 2 — один и тот же конфликт не повторяем.
        if issue not in self.proposal.issues:
            self.proposal.issues.append(issue)


def _times(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} занятие"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} занятия"
    return f"{count} занятий"


def _days(days: list[date]) -> str:
    return ", ".join(f"{day:%d.%m}" for day in days)
