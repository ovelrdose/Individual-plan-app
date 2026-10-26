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
            # Группа галочек или переключателей — тоже form-check-input у каждого пункта.
            if isinstance(
                widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)
            ):
                css = "form-check-input"
            elif isinstance(widget, forms.Select):
                css = "form-select"
            elif isinstance(widget, forms.HiddenInput):
                continue
            else:
                css = "form-control"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()


class ListInput(forms.Widget):
    """Несколько полей с одним именем (синонимы, времена): поля рисует шаблон, новые
    добавляет Alpine.js по кнопке «добавить»."""

    def value_from_datadict(self, data, files, name: str) -> list[str]:
        if hasattr(data, "getlist"):
            return data.getlist(name)
        return list(data.get(name) or [])


class StringListField(forms.Field):
    """Список строк без пустых и повторов (синонимы процедуры)."""

    widget = ListInput

    def __init__(self, *, max_length: int = 100, **kwargs) -> None:
        self.max_length = max_length
        super().__init__(**kwargs)

    def to_python(self, value) -> list[str]:
        result: list[str] = []
        for item in value or []:
            text = str(item).strip()
            if not text or text in result:
                continue
            if len(text) > self.max_length:
                raise forms.ValidationError(f"«{text[:30]}…» длиннее {self.max_length} символов.")
            result.append(text)
        return result

    def validate(self, value: list[str]) -> None:
        if self.required and not value:
            raise forms.ValidationError(self.error_messages["required"], code="required")
