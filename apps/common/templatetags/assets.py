"""Адрес своих скриптов и стилей с версией: после правки браузер не берёт старый файл из кэша."""

from pathlib import Path

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def asset(path: str) -> str:
    """``{% asset 'js/board.js' %}`` → ``/static/js/board.js?v=<время изменения>``."""
    url = static(path)
    found = finders.find(path)
    if not found:
        return url
    return f"{url}?v={int(Path(found).stat().st_mtime)}"
