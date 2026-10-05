"""Общие части форм: поля в стиле Bootstrap и поле даты для <input type="date">."""

from django import forms


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None):
        # Браузерное поле даты принимает только ISO-формат.
        super().__init__(attrs=attrs, format="%Y-%m-%d")


class BootstrapFormMixin:
    """Добавляет полям классы Bootstrap, чтобы шаблоны не повторяли их для каждого поля."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                css = "form-check-input"
            elif isinstance(widget, forms.Select):
                css = "form-select"
            elif isinstance(widget, forms.HiddenInput):
                continue
            else:
                css = "form-control"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()
