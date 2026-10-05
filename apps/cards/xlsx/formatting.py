"""Тексты, которые печатаются в шапке карты."""


def years_word(age: int) -> str:
    """Склонение: 1 год, 2 года, 5 лет, 11 лет, 21 год."""
    if 11 <= age % 100 <= 14:
        return "лет"
    last = age % 10
    if last == 1:
        return "год"
    if 2 <= last <= 4:
        return "года"
    return "лет"


def sex_age_text(sex: str, age: int | None) -> str:
    """«(Ж) 17 лет» — как в шаблоне. Без возраста — «(Ж)», без пола — «17 лет»."""
    parts = [f"({sex})"] if sex else []
    if age is not None:
        parts.append(f"{age} {years_word(age)}")
    return " ".join(parts)
