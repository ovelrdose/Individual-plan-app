"""Сопоставление строки листа назначений со справочником процедур (TZ.md §6.2). Без Django."""

from dataclasses import dataclass

from apps.catalog.domain import normalize_name

POOL_MARKERS = ("бассейн", "вводе")  # «ЛФК в воде» после нормализации — «лфквводе»
POOL_GROUPS = ("верхн", "нижн", "спин")


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
    """
    key = normalize_name(text)
    candidates = [p for p in procedures if kinds is None or p.kind in kinds]
    if kinds is None and any(marker in key for marker in POOL_MARKERS):
        found = _match_pool(key, candidates)
        if found:
            return found

    best, best_length = None, 0
    for procedure in candidates:
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
