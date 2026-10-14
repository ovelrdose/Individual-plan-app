"""Эталонные примеры TZ.md §6.2 на настоящих описаниях справочника (initial_data), без БД."""

import pytest

from apps.catalog import initial_data as data
from apps.catalog.models import ProcedureKind
from apps.exchange.matching import ProcedureRef, match_procedure, match_with_typos


def catalog() -> list[ProcedureRef]:
    specs = [
        *data.LFK_GROUPS,
        *data.POOL_GROUPS,
        *data.DS_GROUPS,
        data.POOL_UNSPECIFIED,
        data.INDIVIDUAL,
        *data.CARD_ONLY,
    ]
    refs = [ProcedureRef(i, s.name, s.kind, s.synonyms) for i, s in enumerate(specs, start=1)]
    refs += [
        ProcedureRef(100 + i, e.name, ProcedureKind.EQUIPMENT, e.synonyms)
        for i, e in enumerate(data.EQUIPMENT)
    ]
    return refs


REFS = catalog()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Эрго общая", "Эрго общая"),
        ("I can нога", "I can нога"),
        ("Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", "Индивидуальное занятие"),
        ("Thera Trainer Tigo 15 мин 2 р/д е/д", "T.tigo"),
        ("Имитрон  15 мин 1 р/д е/д", "Имитрон"),
        ("ST – 150 15 мин 1 р/д е/д", "st-150"),
        (
            "Алмаг на заднюю поверхность бедра и голени правой ноги, и голень левой ноги, "
            "Алмаг 20 мин, 1 р/д е/д",
            "Алмаг",
        ),
        ("Степпер 15 минут 1 р/д, ежедневно", "Степпер"),
        ("ЛФК в воде (нижняя конечность) 30 мин", "ЛФК в воде: нижняя конечность"),
        ("Бассейн, верхняя конечность", "ЛФК в воде: верхняя конечность"),
        ("бассейн спина 30 мин", "ЛФК в воде: спина"),
        ("Бассейн 30 мин 1 р/д", "Бассейн"),
        ("ЛФК в воде 30 мин", "Бассейн"),
        ("Нейро - тренинг", "Нейро-тренинг"),
        ("Эрготренинг", "Эрготерапия"),
        ("Неизвестная процедура 10 мин", None),
    ],
)
def test_reference_examples(text, expected):
    found = match_procedure(text, REFS)

    assert (found.name if found else None) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("медицинского психолога", "Психолог"),
        ("медицинского логопеда", "Логопед"),
        ("эрготерапевта", "Эрготерапевт"),
        ("терапевта", None),
        ("физического терапевта", None),
    ],
)
def test_consultations_match_card_only(text, expected):
    found = match_procedure(text, REFS, kinds=(ProcedureKind.CARD_ONLY,))

    assert (found.name if found else None) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("спина", "ДС: спина"),
        ("Острая спина", "ДС: острая спина"),
        ("ЛФК ШОП 30 мин 1 р/д", "ДС: ШОП"),
        ("шейный отдел позвоночника", "ДС: ШОП"),
        ("Колено", "ДС: коленный сустав"),
        ("Группа: коленные суставы", "ДС: коленный сустав"),
        ("ТБС", "ДС: ТБС"),
        ("тазобедренного сустава", "ДС: ТБС"),
        ("ГСС", "ДС: ГСС"),
        ("Плечевой сустав", "ДС: плечо"),
        ("плечо 30 мин", "ДС: плечо"),
    ],
)
def test_day_hospital_groups(text, expected):
    """Группы ДС — по названию и сокращению в любом списке (FR-IMP-14)."""
    found = match_procedure(text, REFS)

    assert (found.name if found else None) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Массаж плечевого сустава", "Массаж"),
        ("Массаж спины", "Массаж"),
        ("Артромот коленного сустава", "Артромот"),
        ("Мото-Л на коленный сустав", "Мото-Л"),
        ("Механотерапия голеностопного сустава", "Механотерапия"),
        ("Алмаг на тазобедренный сустав 20 мин", "Алмаг"),
        ("Индивидуальное занятие ЛФК (плечо) 30 мин", "Индивидуальное занятие"),
        ("Бассейн спина", "ЛФК в воде: спина"),
    ],
)
def test_body_part_only_qualifies_other_procedure(text, expected):
    """Часть тела в строке другой процедуры — не группа ДС: её ищем последней."""
    found = match_procedure(text, REFS)

    assert (found.name if found else None) == expected


def test_longest_synonym_wins():
    refs = [
        ProcedureRef(1, "Занятие", "CARD_ONLY", ("занятие",)),
        ProcedureRef(2, "Индивидуальное занятие", "INDIVIDUAL", ("индивидуальное занятие",)),
    ]

    assert match_procedure("Индивидуальное занятие 30 мин", refs).id == 2


def test_pool_without_pool_procedures_falls_back_to_synonyms():
    refs = [ProcedureRef(1, "Бассейн", "CARD_ONLY", ("бассейн",))]

    assert match_procedure("Бассейн 30 мин", refs).id == 1


# --- Формулировки из листов разных врачей (TZ.md, решение 71) ---------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Индивид.ЛФК 2р/день, ежедневно", "Индивидуальное занятие"),
        ("Инд. ЛФК 30 мин", "Индивидуальное занятие"),
        ("Индивид.психолог 30 мин 1 р/д по будням", "Психолог"),
        ("Индивид.логопед 1р/день, ежедневно", "Логопед"),
        ("Меркурий ( э/с) на переднюю поверхность предплечий, 10 минут", "Меркурий"),
        ("Диамаг на оголовье 20 мин 1 р/д е/д.", "Диамаг"),
        ("ЧЭНС поясничного отдела, 1р/день, 10 мин.", "ЧЭНС"),
        ("Пневмокомпрессия верхних конечностей 30 минут", "Пневмокомпрессия"),
        ("Магнитотерапия вдоль позвоночника, Алмаг 01, 20 мин", "Алмаг"),
        ("Бос – Pablo 15 мин 1 р/д е/д", "Pablo"),
    ],
)
def test_real_sheet_wording(text, expected):
    found = match_procedure(text, REFS)

    assert (found.name if found else None) == expected


class TestTypos:
    @pytest.mark.parametrize(
        ("text", "kinds", "expected"),
        [
            ("медицинского логпеда", (ProcedureKind.CARD_ONLY,), "Логопед"),
            ("медицинского психолга", (ProcedureKind.CARD_ONLY,), "Психолог"),
            ("Имитрн 15 мин", None, "Имитрон"),
            ("Пневмокомпресия н/к", None, "Пневмокомпрессия"),
        ],
    )
    def test_one_letter_typo_is_forgiven(self, text, kinds, expected):
        found = match_with_typos(text, REFS, kinds=kinds)

        assert found.procedure.name == expected
        assert found.typo

    def test_exact_match_is_not_a_typo(self):
        found = match_with_typos("медицинского логопеда", REFS)

        assert (found.procedure.name, found.typo) == ("Логопед", False)

    @pytest.mark.parametrize(
        ("text", "kinds"),
        [
            # «физического терапевта» в двух буквах от «эрготерапевта» — не опечатка.
            ("физического терапевта", (ProcedureKind.CARD_ONLY,)),
            ("терапевта", (ProcedureKind.CARD_ONLY,)),
            ("Плабо 15 мин", None),  # Pablo короче 6 букв — только точно
            ("Калено в 15:00", None),  # группы ДС с опечаткой не угадываем
            ("Лечебная ходьба", None),
        ],
    )
    def test_no_false_matches(self, text, kinds):
        assert match_with_typos(text, REFS, kinds=kinds).procedure is None
