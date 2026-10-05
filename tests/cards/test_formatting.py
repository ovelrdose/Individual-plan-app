import pytest

from apps.cards.xlsx.formatting import sex_age_text, years_word


@pytest.mark.parametrize(
    ("age", "word"),
    [
        (1, "год"),
        (2, "года"),
        (4, "года"),
        (5, "лет"),
        (11, "лет"),
        (12, "лет"),
        (14, "лет"),
        (17, "лет"),
        (21, "год"),
        (22, "года"),
        (40, "лет"),
        (52, "года"),
        (101, "год"),
        (111, "лет"),
    ],
)
def test_years_word(age, word):
    assert years_word(age) == word


def test_sex_age_text():
    assert sex_age_text("Ж", 17) == "(Ж) 17 лет"
    assert sex_age_text("М", 52) == "(М) 52 года"
