from django import template

register = template.Library()


@register.filter
def hhmm(minutes: int) -> str:
    """Минуты от полуночи → «9:10», как время в карте."""
    return f"{minutes // 60}:{minutes % 60:02d}"


@register.filter
def index(items: list, position: int):
    """Элемент списка по номеру: колонка календаря для ячейки строки."""
    return items[position]
