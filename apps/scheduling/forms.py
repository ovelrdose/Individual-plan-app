from datetime import time

from django import forms
from django.core.validators import MinValueValidator

from apps.accounts.models import User
from apps.catalog.domain import ends_same_day
from apps.catalog.models import BEHAVIOR_HELP, Equipment, GroupSession, Procedure, Section
from apps.common.forms import BootstrapFormMixin, ListInput, StringListField

from .services import group_departments, group_schedule_procedures, procedure_name_taken


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%H:%M")


class TimeListField(forms.Field):
    """Времена занятий: пустые поля отбрасываются, повтор — ошибка (решение 82)."""

    widget = ListInput
    default_error_messages = {"required": "Укажите хотя бы одно время занятия."}

    def to_python(self, value) -> list[time]:
        parse = forms.TimeField()
        result: list[time] = []
        for item in value or []:
            if isinstance(item, time):
                result.append(item)
                continue
            text = str(item).strip()
            if not text:
                continue
            try:
                result.append(parse.to_python(text))
            except forms.ValidationError:
                raise forms.ValidationError(f"Неверное время «{text}».") from None
        repeated = sorted({start for start in result if result.count(start) > 1})
        if repeated:
            raise forms.ValidationError(
                "Время указано дважды: " + ", ".join(f"{t:%H:%M}" for t in repeated) + "."
            )
        return sorted(result)

    def validate(self, value: list[time]) -> None:
        if self.required and not value:
            raise forms.ValidationError(self.error_messages["required"], code="required")


class GroupSessionForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = GroupSession
        # «Действует» нет: выключение — «Удалить» (архив), включение — «Вернуть» (решение 82).
        fields = ["procedure", "start_time", "duration_min", "place"]
        widgets = {"start_time": TimeInput()}
        labels = {"duration_min": "Мин", "place": "Место"}

    def __init__(self, *args, user: User, section: Section | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        groups = group_schedule_procedures(user)
        if section is not None:
            # Во вкладке раздела время добавляют только его группам (решение 82).
            groups = groups.filter(section=section)
        self.fields["procedure"].queryset = groups
        self.fields["place"].widget.attrs["placeholder"] = "как у группы"
        for name in ("start_time", "duration_min", "place"):
            self.fields[name].widget.attrs["class"] += " form-control-sm"
        if self.instance.pk:
            # Группу у занятия не меняют (services.save_group_session).
            self.fields["procedure"].disabled = True


class NewEquipmentForm(BootstrapFormMixin, forms.ModelForm):
    """Новый тренажёр вместе с его процедурой (services.create_equipment)."""

    card_label = forms.CharField(
        label="Как печатается в карте",
        max_length=60,
        required=False,
        help_text="Пусто — как название.",
    )
    synonyms = StringListField(
        label="Синонимы", required=False, help_text="Как тренажёр пишут в листе назначений."
    )

    class Meta:
        model = Equipment
        fields = ["name", "window_start", "window_end", "step_min", "duration_min", "capacity"]
        widgets = {"window_start": TimeInput(), "window_end": TimeInput()}


class EquipmentForm(NewEquipmentForm):
    """Правка тренажёра: название, подпись и синонимы его процедуры, окно и вместимость.
    «Действует» нет — «Удалить» / «Вернуть из архива» (решение 83)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        procedure = self.instance.procedures.order_by("pk").first() if self.instance.pk else None
        if procedure is not None and not self.is_bound:
            self.initial["card_label"] = procedure.card_label
            self.initial["synonyms"] = list(procedure.synonyms)


class SectionForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Section
        fields = ["name", "behavior", "order"]
        widgets = {"behavior": forms.RadioSelect}
        help_texts = {"order": "Вкладки идут по возрастанию."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.procedures.exists():
            # От поведения зависит вид групп раздела (services.save_section).
            self.fields["behavior"].disabled = True
            self.fields["behavior"].help_text = "В разделе есть группы — поведение не меняется."

    def behaviors(self) -> list[tuple[str, str, str, bool]]:
        """Варианты поведения с пояснением: значение, подпись, пояснение, выбран ли."""
        current = str(self["behavior"].value() or "")
        return [
            (value, label, BEHAVIOR_HELP[value], value == current)
            for value, label in self.fields["behavior"].choices
            if value
        ]


class GroupForm(BootstrapFormMixin, forms.ModelForm):
    """Новая группа (с временами занятий) или правка группы (решение 82)."""

    synonyms = StringListField(
        label="Синонимы", required=False, help_text="Как группу пишут в листе назначений."
    )
    times = TimeListField(label="Время занятий")

    class Meta:
        model = Procedure
        fields = [
            "section",
            "name",
            "card_label",
            "synonyms",
            "default_duration_min",
            "place",
            "department",
        ]
        labels = {"default_duration_min": "Длительность, мин"}

    def __init__(self, *args, user: User, **kwargs):
        super().__init__(*args, **kwargs)
        section = self.fields["section"]
        section.queryset = Section.objects.order_by("order", "name")
        section.required = True
        section.empty_label = None
        section.label_from_instance = lambda item: (
            f"{item.name} ({item.get_behavior_display().lower()})"
        )
        self.fields["card_label"].required = False
        self.fields["card_label"].help_text = "Пусто — как название."
        duration = self.fields["default_duration_min"]
        duration.required = True
        # min_value после создания поля валидатор не добавляет — добавляем сами.
        duration.validators.append(MinValueValidator(1))
        duration.widget.attrs["min"] = 1
        department = self.fields["department"]
        department.queryset = group_departments(user)
        department.empty_label = "Общая для центра"
        if self.instance.pk:
            del self.fields["times"]

    def clean(self):
        cleaned = super().clean()
        section = cleaned.get("section")
        if section is not None and not self.instance.pk:
            # Вид новой группы — из раздела; без него проверка модели не узнает группу.
            self.instance.kind = section.behavior
        name = (cleaned.get("name") or "").strip()
        label_limit = Procedure._meta.get_field("card_label").max_length
        if len(name) > label_limit and not (cleaned.get("card_label") or "").strip():
            # Пустая подпись в карте — это название; ошибку показываем там, где её причина.
            self.add_error(
                "name",
                f"В карту печатается не больше {label_limit} символов, а в названии "
                f"{len(name)}: заполните «как печатается в карте» или сократите название.",
            )
        duration = cleaned.get("default_duration_min")
        late = [
            start
            for start in cleaned.get("times") or []
            if duration and not ends_same_day(start, duration)
        ]
        if late:
            self.add_error(
                "times",
                "Занятие должно закончиться до полуночи: "
                + ", ".join(f"{start:%H:%M}" for start in late)
                + f" + {duration} мин — позже.",
            )
        if name:
            probe = Procedure(pk=self.instance.pk, name=name, department=cleaned.get("department"))
            if error := procedure_name_taken(probe):
                self.add_error("name", error)
        return cleaned
