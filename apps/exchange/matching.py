"""Сопоставление строки листа назначений со справочником процедур (TZ.md §6.2). Без Django."""

from dataclasses import dataclass

from apps.catalog.domain import normalize_name

POOL_MARKERS = ("бассейн", "вводе")  # «ЛФК в воде» после нормализации — «лфквводе»
POOL_GROUPS = ("верхн", "нижн", "спин")
# Группы дневного стационара названы по части тела — их ищем, только если ничего другого нет.
FALLBACK_KINDS = ("DS_GROUP",)


@dataclass(frozen=True)
class ProcedureRef:
    id: int
    name: str
    kind: str
    synonyms: tuple[str, ...] = ()

    def match_keys(self) -> list[str]:
        return [key for key in (normalize_name(t) for t in (self.name, *self.synonyms)) if key]


def match_procedure(
    text: str, procedures: list[ProcedureRef], *, kinds: tuple[str, ...] | None = None
) -> ProcedureRef | None:
    """Процедура для строки или None.

    Бассейн (FR-IMP-9) — по группе «верхн/нижн/спин», без группы — общая «Бассейн».
    Остальное (FR-IMP-8) — синоним входит в строку, при нескольких — самый длинный синоним.
    Группы ДС (FR-IMP-14) — последними: они названы по части тела, а часть тела в строке чаще
    уточняет другую процедуру («Массаж плечевого сустава», «Артромот коленного сустава»).
    Поэтому группа ДС — только если строка не подошла ни к одной другой процедуре.
    """
    key = normalize_name(text)
    candidates = [p for p in procedures if kinds is None or p.kind in kinds]
    if kinds is None and any(marker in key for marker in POOL_MARKERS):
        found = _match_pool(key, candidates)
        if found:
            return found

    main = [p for p in candidates if p.kind not in FALLBACK_KINDS]
    fallback = [p for p in candidates if p.kind in FALLBACK_KINDS]
    return _longest(key, main) or _longest(key, fallback)


def _longest(key: str, procedures: list[ProcedureRef]) -> ProcedureRef | None:
    """Процедура, синоним которой входит в строку; при нескольких — самый длинный синоним:
    «острая спина» побеждает «спину»."""
    best, best_length = None, 0
    for procedure in procedures:
        for synonym in procedure.match_keys():
            if synonym in key and len(synonym) > best_length:
                best, best_length = procedure, len(synonym)
    return best


def _match_pool(key: str, procedures: list[ProcedureRef]) -> ProcedureRef | None:
    pools = [p for p in procedures if p.kind == "POOL"]
    for group in POOL_GROUPS:
        if group in key:
            found = next((p for p in pools if group in normalize_name(p.name)), None)
            if found:
                return found
    return next(
        (p for p in pools if not any(group in normalize_name(p.name) for group in POOL_GROUPS)),
        None,
    )


# Опечатка — одна буква (решение 71). Короткие названия (ТБС, ШОП, Pablo) — только точно:
# в них одна буква уже меняет смысл.
TYPO_MIN_LENGTH = 6


@dataclass(frozen=True)
class Match:
    procedure: ProcedureRef | None
    typo: bool = False  # распознано с опечаткой — врачу показывается предупреждение


def match_with_typos(
    text: str, procedures: list[ProcedureRef], *, kinds: tuple[str, ...] | None = None
) -> Match:
    """Как ``match_procedure``, а если ничего не нашлось — синоним с опечаткой в одну букву
    («логпеда» → «логопед»). Группы ДС так не ищутся: их названия — части тела."""
    found = match_procedure(text, procedures, kinds=kinds)
    if found is not None:
        return Match(found)
    key = normalize_name(text)
    best, best_length = None, 0
    for procedure in procedures:
        if procedure.kind in FALLBACK_KINDS or (kinds is not None and procedure.kind not in kinds):
            continue
        for synonym in procedure.match_keys():
            long_enough = len(synonym) >= TYPO_MIN_LENGTH and len(synonym) > best_length
            if long_enough and _contains_near(key, synonym):
                best, best_length = procedure, len(synonym)
    return Match(best, typo=True) if best else Match(None)


def _contains_near(text: str, word: str) -> bool:
    """В ``text`` есть кусок, отличающийся от ``word`` не больше чем на одну букву."""
    for size in (len(word) - 1, len(word), len(word) + 1):
        for start in range(0, len(text) - size + 1):
            if _distance_at_most_one(text[start : start + size], word):
                return True
    return False


def _distance_at_most_one(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return edits + (len(b) - j) <= 1
