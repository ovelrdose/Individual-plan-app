from datetime import date, timedelta

from apps.scheduling.domain import ScheduledItem, TypicalRow, day_grid, typical_day

DAYS = [date(2026, 10, 5) + timedelta(days=i) for i in range(5)]


def item(key, label, day, start, place="зал"):
    return ScheduledItem(key, label, place, day, start, start + 30)


def test_most_common_time_and_deviations():
    items = [item(1, "Эрго общая", d, 550) for d in DAYS[:4]] + [
        item(1, "Эрго общая", DAYS[4], 940)
    ]
    items += [item(2, "st-150", d, 780, "БОС") for d in DAYS]

    rows = typical_day(items)

    assert [(r.label, r.start, r.place, r.days) for r in rows] == [
        ("Эрго общая", 550, "зал", 5),
        ("st-150", 780, "БОС", 5),
    ]
    assert rows[0].deviations == (DAYS[4],)
    assert rows[1].deviations == ()


def test_two_units_a_day_give_two_rows():
    items = [item(1, "st-150", d, start) for d in DAYS for start in (795, 780)]

    rows = typical_day(items)

    assert [r.start for r in rows] == [780, 795]


def test_tie_takes_earliest_time():
    items = [item(1, "Эрго", DAYS[0], 600), item(1, "Эрго", DAYS[1], 550)]

    assert typical_day(items)[0].start == 550


def test_empty():
    assert typical_day([]) == []


# Дневная сетка слотов как в справочнике: 9:10 … 16:20.
SLOT_STARTS = [
    "9:10",
    "9:50",
    "10:30",
    "11:10",
    "13:00",
    "13:40",
    "14:20",
    "15:00",
    "15:40",
    "16:20",
]
SLOTS = [
    (int(h) * 60 + int(m), int(h) * 60 + int(m) + 30)
    for h, m in (s.split(":") for s in SLOT_STARTS)
]


def row(label, start, end=None, place="зал", deviations=()):
    end = start + 30 if end is None else end
    return TypicalRow(1, label, place, start, end, 5, tuple(deviations))


def test_empty_day_is_all_windows():
    grid = day_grid([], SLOTS)

    assert [(r.start, r.is_window, r.label, r.place) for r in grid] == [
        (start, True, "", "") for start, _ in SLOTS
    ]


def test_activities_take_their_slots_and_leave_windows():
    grid = day_grid([row("Эрго общая", 550), row("Нейротренинг", 860)], SLOTS)

    assert len(grid) == 10
    assert [(r.start, r.label) for r in grid if not r.is_window] == [
        (550, "Эрго общая"),
        (860, "Нейротренинг"),
    ]
    assert grid[1].is_window and grid[1].start == 590


def test_off_grid_start_keeps_real_time():
    # Группа в 9:00 — строка слота 9:10, но время в карте 9:00; бассейн 13:30–14:00 — слот 13:40.
    grid = day_grid([row("Эрго общая", 540), row("ЛФК в воде", 810, place="бассейн")], SLOTS)

    assert (grid[0].slot_start, grid[0].start, grid[0].label) == (550, 540, "Эрго общая")
    assert grid[4].is_window
    assert (grid[5].slot_start, grid[5].start, grid[5].place) == (820, 810, "бассейн")


def test_two_activities_in_one_slot_share_a_row():
    grid = day_grid(
        [
            row("st-150", 795, 810, place="БОС"),
            row("Pablo", 780, 795, place="БОС", deviations=[DAYS[0]]),
        ],
        SLOTS,
    )

    assert (grid[4].start, grid[4].label, grid[4].place) == (780, "Pablo/st-150 13:15", "БОС")
    assert grid[4].deviations == 1
    assert sum(not r.is_window for r in grid) == 1


def test_activity_outside_grid_gets_own_row_and_keeps_windows():
    # 12:00 и 18:00 не пересекаются с дневными слотами — свои строки, окна 11:10 и 16:20 целы.
    grid = day_grid([row("Массаж", 720, 735), row("Инд.занятие", 1080)], SLOTS)

    assert len(grid) == 12
    assert grid[3].is_window and grid[3].start == 670
    assert (grid[4].start, grid[4].label) == (720, "Массаж")
    assert grid[10].is_window and grid[10].start == 980
    assert (grid[11].start, grid[11].label) == (1080, "Инд.занятие")


def test_later_activity_in_shared_slot_shows_its_time():
    # Тренажёр 2 р/д и Имитрон: 13:00 и 13:15 в одном слоте — время второго не теряется.
    grid = day_grid(
        [row("st-150", 780, 795), row("st-150", 795, 810), row("Имитрон", 780, 795)],
        SLOTS,
    )

    assert (grid[4].start, grid[4].label) == (780, "st-150/Имитрон/st-150 13:15")


def test_without_slots_every_activity_is_a_row():
    grid = day_grid([row("st-150", 780), row("Эрго общая", 550)], [])

    assert [(r.start, r.label) for r in grid] == [(550, "Эрго общая"), (780, "st-150")]


def test_individual_instructor_most_common_others_are_deviations():
    def ind(day, who):
        return ScheduledItem(3, "Инд.занятие", "", day, 590, 620, who)

    items = [ind(d, "Волков/ Лебедева") for d in DAYS[:4]] + [ind(DAYS[4], "Соколов")]

    rows = typical_day(items)
    grid = day_grid(rows, [(550, 580), (590, 620)])

    assert rows[0].who == "Волков/ Лебедева"
    assert rows[0].deviations == (DAYS[4],)
    assert grid[1].who == "Волков/ Лебедева"
    assert grid[0].who == "" and grid[0].is_window


def test_individual_without_instructor_is_not_deviation():
    """Выходной: индивидуальное в то же время без инструктора (решение 56) — не отклонение."""

    def ind(day, who, start=590):
        return ScheduledItem(3, "Инд.занятие", "", day, start, start + 30, who)

    items = [ind(d, "Соколов") for d in DAYS[:3]] + [ind(DAYS[3], ""), ind(DAYS[4], "", 630)]

    rows = typical_day(items)

    assert rows[0].who == "Соколов"
    assert rows[0].deviations == (DAYS[4],), "другое время — отклонение, пустой инструктор — нет"
