"""Жадный подбор (TZ.md §7.3): группы ЛФК, бассейн, индивидуальные занятия, тренажёры.

Идея одна для всех: выбрать одно время на весь курс — то, которое подходит в наибольшее
число дней; в дни, где оно не подходит, взять замену на день (предупреждение), а если
замены нет — оставить день пустым (конфликт). Пересекать занятия пациента нельзя никогда:
это запрещено и базой данных.

У индивидуальных занятий выбирается ещё и инструктор: «команда» (пара 2/2 или одиночка)
и слот на весь курс, в дату — работающий член команды (шаг 3). Только по будням: в субботу и
воскресенье занятие стоит в то же время без инструктора — его ведёт дежурный 2/2 (решение 56).
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .model import (
    Issue,
    IssueCode,
    Kind,
    Need,
    Placement,
    Proposal,
    Slot,
    Snapshot,
    is_weekend,
)

STEP_ORDER = (Kind.LFK_GROUP, Kind.POOL, Kind.INDIVIDUAL, Kind.EQUIPMENT)
# Два индивидуальных занятия с перерывом не больше 10 минут — «подряд» (TZ.md §7.2, ответ 9).
ADJACENT_GAP = 10


@dataclass(frozen=True)
class _Option:
    start: int
    end: int
    session_id: int | None = None


@dataclass(frozen=True)
class _Seat:
    """Индивидуальное занятие в дату: кто и в каком слоте. В выходные — без инструктора."""

    instructor_id: int | None
    slot: Slot


@dataclass(frozen=True)
class _Choice:
    """Выбор на весь курс: команда (или инструктор по желанию пациента) и слот."""

    members: tuple[int, ...]
    slot: Slot
    label: str


def propose(snapshot: Snapshot) -> Proposal:
    state = _State(snapshot)
    for kind in STEP_ORDER:
        needs = [n for n in snapshot.needs if n.kind == kind]
        if kind == Kind.EQUIPMENT:
            for need in needs:
                state.place_equipment(need)
        elif kind == Kind.INDIVIDUAL:
            state.place_individuals(sorted(needs, key=lambda n: n.prescription_id))
        else:
            # Сначала группы с меньшим выбором времени: у «I can нога» одно занятие в день,
            # у «Нейро-тренинга» два — иначе результат зависел бы от порядка строк в листе.
            for need in sorted(needs, key=lambda n: (len(n.sessions), n.prescription_id)):
                state.place_group(need)
    return state.proposal


def hhmm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


class _State:
    def __init__(self, snapshot: Snapshot):
        self.proposal = Proposal()
        self.busy: dict[date, list[tuple[int, int]]] = defaultdict(list)
        for item in snapshot.patient_busy:
            self.busy[item.date].append((item.start, item.end))
        self.load: dict[tuple[int, date], list[tuple[int, int]]] = defaultdict(list)
        for item in snapshot.equipment_load:
            self.load[(item.equipment_id, item.date)].append((item.start, item.end))
        # Индивидуальные занятия пациента по дням: для лимита и запрета «подряд».
        self.individual: dict[date, list[tuple[int, int]]] = defaultdict(list)
        for item in snapshot.patient_busy:
            if item.individual:
                self.individual[item.date].append((item.start, item.end))
        self.staff = snapshot.staff
        # Вечерние слоты — только ручная запись (TZ.md FR-CAT-5, решение 10).
        self.slots = sorted(
            (slot for slot in self.staff.slots if not slot.evening), key=lambda s: (s.start, s.id)
        )
        self.teams = self.staff.teams
        members = [member for team in self.teams for member in team.members]
        self.names = {m.id: m.short_name for m in members}
        self.order = {m.id: (m.display_order, m.short_name, m.id) for m in members}
        self.partner = {
            pk: other for team in self.teams for pk in team.member_ids for other in team.member_ids
            if other != pk
        }  # fmt: skip
        self.team_of = {pk: team for team in self.teams for pk in team.member_ids}
        self.instructor_load: Counter[int] = Counter(dict(self.staff.load))
        working = self.staff.working or {(day, pk) for day, _slot, pk in self.staff.free}
        self.workdays: dict[int, set[date]] = defaultdict(set)
        for day, pk in working:
            if not is_weekend(day):
                self.workdays[pk].add(day)
        self.taken: set[tuple[date, int, int]] = set()

    # --- Шаги 1–2: группы ЛФК и бассейн ---------------------------------------------------

    def place_group(self, need: Need) -> None:
        if need.group_required:
            self._issue(
                IssueCode.POOL_TYPE_REQUIRED, need, "не выбрана группа бассейна — выберите её"
            )
            return
        if not need.sessions:
            self._issue(IssueCode.NO_SESSIONS, need, "у группы нет занятий в расписании групп")
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
                f"назначено {need.per_day} р/д, а в расписании групп {_times(len(options))} "
                f"в день — поставлено {units}",
            )
        for unit in range(units):
            # Закреплённые вручную занятия занимают первые «единицы» дня.
            dates = tuple(day for day in need.dates if need.pinned_on(day) <= unit)
            # «Первая по времени сессия, которая не пересекается с уже выбранными» (§7.3, шаг 1):
            # нагрузки у групп нет, поэтому при равном покрытии решает время. Второе занятие
            # той же группы не совпадёт с первым: пациент в это время уже занят.
            self._place_course(need, options, dates=dates, fits=self._patient_free,
                               load=lambda option: 0,
                               unplaced=IssueCode.GROUP_OVERLAP)  # fmt: skip

    # --- Шаг 4: тренажёры ------------------------------------------------------------------

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

    # --- Шаг 3: индивидуальные занятия --------------------------------------------------------

    def place_individuals(self, needs: list[Need]) -> None:
        """Сначала сохраняются прошлые постановки всех индивидуальных назначений, и только потом
        какое-либо из них добирает недостающее: иначе назначение, которое идёт раньше, заняло бы
        слот другого (решение 49)."""
        kept = {need.prescription_id: self._keep_previous(need) for need in needs}
        for need in needs:
            self.place_individual(need, kept[need.prescription_id])

    def _units(self, need: Need) -> int:
        return min(need.per_day, self.staff.max_per_day)

    def place_individual(self, need: Need, kept: dict[date, list[_Seat]] | None = None) -> None:
        kept = kept or {}
        limit = self.staff.max_per_day
        units = self._units(need)
        if need.per_day > limit:
            # Сервис такого не пропустит (FR-PRG-3), но движок защищается сам (§7.3, сценарий 9).
            self._issue(
                IssueCode.INDIVIDUAL_LIMIT,
                need,
                f"назначено {need.per_day} р/д, а лимит отделения — {limit}: поставлено {limit}",
            )
        anchors = self._anchors(kept, units)
        filled = self._split_kept(kept, anchors, units)
        todo: dict[int, list[date]] = defaultdict(list)
        for day in need.dates:
            count = units - need.pinned_on(day) - len(kept.get(day, ()))
            empty = [unit for unit in range(units) if day not in filled[unit]]
            for unit in empty[: max(count, 0)]:
                todo[unit].append(day)
        limited: list[date] = []
        for unit in range(units):
            days = []
            for day in todo[unit]:
                if len(self.individual[day]) >= limit:
                    limited.append(day)
                else:
                    days.append(day)
            anchor = anchors[unit] if unit < len(anchors) else None
            self._place_individual_unit(need, days, anchor)
        if limited:
            self._issue(
                IssueCode.INDIVIDUAL_LIMIT,
                need,
                f"{_days(sorted(set(limited)))} — индивидуальных в день уже {limit} "
                "(лимит отделения), не поставлено",
            )

    def _keep_previous(self, need: Need) -> dict[date, list[_Seat]]:
        """Прошлые автоматические постановки, которые ещё допустимы, — первыми (решение 49).

        При инструкторе по желанию пациента прошлые занятия у других инструкторов не
        сохраняются: в его рабочие дни ставится он (решение 44, 52).
        """
        slots = {slot.id: slot for slot in self.slots}
        active = set(need.dates)
        preferred = self.staff.preferred
        units = self._units(need)
        previous = sorted(
            (
                item
                for item in self.staff.previous
                if item.prescription_id == need.prescription_id
                and item.slot_id in slots
                and item.date in active
                # Выходные каждый раз берут время единицы заново: инструктора там нет.
                and not is_weekend(item.date)
                and (preferred is None or item.instructor_id == preferred)
            ),
            key=lambda item: (item.date, slots[item.slot_id].start, item.instructor_id),
        )
        kept: dict[date, list[_Seat]] = defaultdict(list)
        for item in previous:
            day, seat = item.date, _Seat(item.instructor_id, slots[item.slot_id])
            if need.pinned_on(day) + len(kept[day]) >= units:
                continue
            if len(self.individual[day]) >= self.staff.max_per_day:
                continue
            if not self._seat_free(day, seat):
                continue
            self._put_individual(need, day, seat)
            kept[day].append(seat)
        return {day: seats for day, seats in kept.items() if seats}

    def _anchors(self, kept: dict[date, list[_Seat]], units: int) -> list[_Choice]:
        """Основной вариант «единиц» дня по сохранённым занятиям: самые частые «команда × слот»
        в разных слотах (решение 52). Недостающие даты единицы добираются у этой команды."""
        counts: Counter[_Choice] = Counter(
            self._choice_of(seat) for seats in kept.values() for seat in seats
        )
        anchors: list[_Choice] = []
        for choice, _count in sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0].slot.start, kv[0].members)
        ):
            if len(anchors) < units and all(a.slot != choice.slot for a in anchors):
                anchors.append(choice)
        return anchors

    def _choice_of(self, seat: _Seat) -> _Choice:
        if self.staff.preferred == seat.instructor_id or seat.instructor_id not in self.team_of:
            return _Choice((seat.instructor_id,), seat.slot, self.names.get(seat.instructor_id, ""))
        team = self.team_of[seat.instructor_id]
        return _Choice(team.member_ids, seat.slot, team.label)

    def _split_kept(
        self, kept: dict[date, list[_Seat]], anchors: list[_Choice], units: int
    ) -> dict[int, dict[date, _Seat]]:
        """Сохранённое занятие дня относится к «единице», чей основной вариант с ним совпадает,
        иначе — к ближайшей по времени: ранняя постановка, ставшая невозможной, не сдвигает
        позднюю в чужую единицу."""
        filled: dict[int, dict[date, _Seat]] = defaultdict(dict)
        for day, seats in kept.items():
            free = list(range(units))
            rest = []
            for seat in seats:
                choice = self._choice_of(seat)
                unit = next((u for u in free if u < len(anchors) and anchors[u] == choice), None)
                if unit is None:
                    rest.append(seat)
                    continue
                free.remove(unit)
                filled[unit][day] = seat
            for seat in rest:
                unit = min(
                    free,
                    key=lambda u: (
                        abs(anchors[u].slot.start - seat.slot.start)
                        if u < len(anchors)
                        else 24 * 60,
                        u,
                    ),
                )
                free.remove(unit)
                filled[unit][day] = seat
        return filled

    def _place_individual_unit(self, need: Need, days: list[date], anchor: _Choice | None) -> None:
        """Одна «единица» дня: выбор команды и слота на курс, замены на день (§7.3, шаг 3).

        Предупреждения — только на даты, поставленные заново: сохранённые занятия не менялись.
        Команда выбирается по будням; в выходные — то же время без инструктора (решение 56).
        """
        if not days:
            return
        main = anchor or self._main_choice([day for day in days if not is_weekend(day)])
        preferred = self.staff.preferred
        notes: dict[tuple[IssueCode, str], list[date]] = defaultdict(list)
        missing: list[date] = []
        for day in days:
            if main is None:
                seat = None
            elif is_weekend(day):
                seat = self._weekend_seat(day, main.slot)
            else:
                seat = self._seat_for_day(day, main, preferred, keep_team=anchor is not None)
            if seat is None:
                missing.append(day)
                continue
            self._put_individual(need, day, seat)
            self._note(notes, main, seat, day)
        for (code, text), dates in sorted(notes.items(), key=lambda kv: (min(kv[1]), kv[0])):
            self._issue(code, need, f"{_days(sorted(dates))} — {text}")
        if missing:
            self._issue(
                IssueCode.INDIVIDUAL_UNPLACED,
                need,
                f"{_days(missing)} — нет свободного инструктора, не поставлено",
            )

    def _main_choice(self, days: list[date]) -> _Choice | None:
        """Команда и слот на весь курс.

        Ключ — (−покрытие, загрузка, время слота, порядок в шахматке) (§7.3). Покрытие —
        сколько дат подходит. По желанию пациента сначала выбирается слот, где свободен он сам
        (решение 44), при равенстве — где свободна его команда; если он недоступен весь курс —
        общее правило.
        """
        teams = [
            _Choice(team.member_ids, slot, team.label) for team in self.teams for slot in self.slots
        ]
        if not teams:
            return None

        def coverage(choice: _Choice) -> int:
            return sum(
                self._free_member(day, choice.members, choice.slot) is not None for day in days
            )

        covered = {choice: coverage(choice) for choice in teams}
        load = {choice.members: self._average_load(choice.members) for choice in teams}

        def team_key(choice: _Choice) -> tuple:
            order = min(self.order[pk] for pk in choice.members)
            return (load[choice.members], -covered[choice], choice.slot.start, order)

        preferred = self.staff.preferred
        if preferred is not None and preferred in self.names:
            own = [_Choice((preferred,), slot, self.names[preferred]) for slot in self.slots]
            team = self.team_of[preferred].member_ids

            def own_key(choice: _Choice) -> tuple:
                return (-coverage(choice), -coverage(_Choice(team, choice.slot, "")),
                        choice.slot.start)  # fmt: skip

            best = min(own, key=own_key)
            if coverage(best):
                return best
        # Равная нагрузка важнее одной команды на весь курс (решение 56): из команд, которые
        # могут вести пациента хотя бы в половине дат, — наименее загруженная в среднем за
        # рабочий день. Остальные даты — штатная подмена.
        allowed = [choice for choice in teams if 2 * covered[choice] >= len(days)]
        if allowed:
            return min(allowed, key=team_key)
        return min(teams, key=lambda c: (-covered[c], *team_key(c)))

    def _average_load(self, members: tuple[int, ...]) -> Fraction:
        """Индивидуальных занятий команды на её рабочий день за даты курса — дробью, без
        округлений: сравнение детерминированное (решение 56)."""
        days = set().union(*(self.workdays[pk] for pk in members))
        if not days:
            return Fraction(10**9)
        return Fraction(sum(self.instructor_load[pk] for pk in members), len(days))

    def _seat_for_day(
        self, day: date, main: _Choice, preferred: int | None, *, keep_team: bool = False
    ) -> _Seat | None:
        """Замена на день, по порядку: работающий член команды в том же слоте; любой
        свободный инструктор в том же слоте с наименьшей загрузкой; ближайший свободный слот
        (сначала команда, потом любой). По желанию пациента — сначала он сам (в любом слоте,
        ближайшем к основному), потом напарник, потом общее правило (решение 44).

        ``keep_team`` — команда держит прошлые постановки (решение 52): в том же слоте — она
        или любой свободный, иначе сначала она же в ближайшем слоте, потом другие инструкторы.
        """
        near = sorted(self.slots, key=lambda s: (abs(s.start - main.slot.start), s.start))
        tiers: list[tuple[tuple[int, ...] | None, list[Slot]]] = []
        if preferred is not None:
            tiers.append(((preferred,), near))
            if preferred in self.partner:
                tiers.append(((self.partner[preferred],), near))
        if keep_team:
            # Сначала то же время (команда, затем любой) — пациенту не нужно менять время, если
            # занят только инструктор (FR-SCH-13); потом ближайшее время у той же команды.
            tiers += [(main.members, [main.slot]), (None, [main.slot]), (main.members, near)]
            tiers += [(None, near)]
        else:
            tiers += [(main.members, [main.slot]), (None, [main.slot])]
            for slot in near:
                tiers += [(main.members, [slot]), (None, [slot])]
        for who, slots in tiers:
            for slot in slots:
                found = self._free_member(day, who, slot)
                if found is not None:
                    return _Seat(found, slot)
        return None

    def _weekend_seat(self, day: date, main: Slot) -> _Seat | None:
        """Выходной: время единицы, а если пациент в это время занят — ближайший слот."""
        near = sorted(self.slots, key=lambda s: (abs(s.start - main.start), s.start))
        slot = next((slot for slot in near if self._individual_fits(day, slot)), None)
        return _Seat(None, slot) if slot is not None else None

    def _free_member(self, day: date, who: Iterable[int] | None, slot: Slot) -> int | None:
        """Кто может взять пациента в слот: из ``who`` — первый по порядку команды, из всех
        (``None``) — с наименьшей загрузкой. None — никто или пациент в это время занят."""
        if not self._individual_fits(day, slot):
            return None
        pool = self.names if who is None else who
        ready = [pk for pk in pool if self._seat_free(day, _Seat(pk, slot), patient=False)]
        if not ready:
            return None
        if who is None:
            return min(ready, key=lambda pk: (self.instructor_load[pk], self.order[pk]))
        return ready[0]

    def _seat_free(self, day: date, seat: _Seat, *, patient: bool = True) -> bool:
        key = (day, seat.slot.id, seat.instructor_id)
        if key not in self.staff.free or key in self.taken:
            return False
        return not patient or self._individual_fits(day, seat.slot)

    def _individual_fits(self, day: date, slot: Slot) -> bool:
        """Пациент свободен весь интервал слота, и рядом нет его индивидуального занятия:
        два подряд запрещены без исключений (INDIVIDUAL_ADJACENT, решение 6)."""
        if not self._patient_free(day, _Option(slot.start, slot.end)):
            return False
        return all(
            max(start - slot.end, slot.start - end) > ADJACENT_GAP
            for start, end in self.individual[day]
        )

    def _put_individual(self, need: Need, day: date, seat: _Seat) -> None:
        slot = seat.slot
        self.busy[day].append((slot.start, slot.end))
        self.individual[day].append((slot.start, slot.end))
        if seat.instructor_id is not None:
            self.taken.add((day, slot.id, seat.instructor_id))
            self.instructor_load[seat.instructor_id] += 1
        self.proposal.placements.append(
            Placement(
                prescription_id=need.prescription_id,
                procedure_id=need.procedure_id,
                kind=need.kind,
                date=day,
                start=slot.start,
                end=slot.end,
                instructor_id=seat.instructor_id,
                slot_id=slot.id,
            )
        )

    def _note(
        self,
        notes: dict[tuple[IssueCode, str], list[date]],
        main: _Choice | None,
        seat: _Seat,
        day: date,
    ) -> None:
        """Отличие от выбранного на курс — предупреждение на дату."""
        preferred = self.staff.preferred
        name = self.names.get(seat.instructor_id, "?")
        time_text = (
            f"в {hhmm(seat.slot.start)} вместо {hhmm(main.slot.start)}"
            if main is not None and seat.slot != main.slot
            else ""
        )
        if seat.instructor_id is None:
            # Выходной: инструктора нет, отличаться может только время.
            if time_text:
                notes[(IssueCode.DAY_DEVIATION, time_text)].append(day)
            return
        if preferred is not None and seat.instructor_id != preferred:
            wanted = self.names.get(preferred) or self.staff.preferred_name
            text = (
                f"{name} вместо {wanted} (по желанию пациента)"
                if wanted
                else f"{name} вместо инструктора по желанию пациента"
            )
            notes[(IssueCode.PREFERRED_REPLACED, _join(text, time_text))].append(day)
            return
        if main is None:
            return
        # В нерабочий по графику день команды подмена штатная — не отклонение (решение 56).
        off_duty = not any(day in self.workdays[pk] for pk in main.members)
        who_text = (
            "" if seat.instructor_id in main.members or off_duty else f"{name} вместо {main.label}"
        )
        if who_text or time_text:
            notes[(IssueCode.DAY_DEVIATION, _join(who_text, time_text))].append(day)

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
    ) -> None:
        dates = need.dates if dates is None else dates
        if not options or not dates:
            return
        coverage = {option: sum(fits(day, option) for day in dates) for option in options}
        best = min(options, key=lambda o: (-coverage[o], load(o), o.start, o.session_id or 0))

        deviations: dict[int, list[date]] = defaultdict(list)
        missing: list[date] = []
        for day in dates:
            if fits(day, best):
                self._put(need, day, best)
                continue
            # Замена на день: свободное время с наименьшей загрузкой, поближе к основному.
            candidates = [o for o in options if fits(day, o)]
            if not candidates:
                missing.append(day)
                continue
            alternative = min(
                candidates, key=lambda o: (load(o), abs(o.start - best.start), o.start)
            )
            self._put(need, day, alternative)
            deviations[alternative.start].append(day)

        for start, days in sorted(deviations.items()):
            self._issue(
                IssueCode.DAY_DEVIATION,
                need,
                f"{_days(days)} — в {hhmm(start)} вместо {hhmm(best.start)}",
            )
        if missing:
            self._issue(unplaced, need, f"{_days(missing)} — нет свободного времени, не поставлено")

    def _patient_free(self, day: date, option: _Option) -> bool:
        return all(end <= option.start or option.end <= start for start, end in self.busy[day])

    def _put(self, need: Need, day: date, option: _Option) -> None:
        self.busy[day].append((option.start, option.end))
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


def _join(*parts: str) -> str:
    return ", ".join(part for part in parts if part)


def _days(days: list[date]) -> str:
    return ", ".join(f"{day:%d.%m}" for day in days)
