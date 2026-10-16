import subprocess
from io import BytesIO

import pytest

from apps.exchange import word_doc
from apps.exchange.prescription_sheet import SheetError, parse_sheet
from apps.exchange.word_doc import OLE_SIGNATURE, WordConvertError, doc_to_docx, is_word97
from tests.sheets import make_doc_sheet, make_sheet


def test_signature():
    assert is_word97(OLE_SIGNATURE + b"rest")
    assert not is_word97(make_sheet().getvalue()[:8])
    assert not is_word97(b"")


def test_doc_sheet_parsed_like_docx():
    rows = [
        ("02.10.26", "Имитрон  15 мин 1 р/д е/д", ""),
        ("02.10.26", "Бассейн\n- верхняя конечность\n30 мин 1 р/д", "05.10.26"),
    ]

    from_doc = parse_sheet(make_doc_sheet(rows=rows, room="12б"))
    from_docx = parse_sheet(make_sheet(rows=rows, room="12б"))

    assert from_doc == from_docx
    assert (from_doc.full_name, from_doc.room, len(from_doc.rows)) == (
        "Тестова Анна Сергеевна",
        "12б",
        2,
    )


def test_damaged_doc():
    """Сигнатура OLE есть, а документа нет — LibreOffice ничего не создаёт."""
    if not word_doc.shutil.which("soffice"):
        pytest.skip("Нет LibreOffice (soffice)")

    with pytest.raises(WordConvertError, match="повреждён"):
        doc_to_docx(OLE_SIGNATURE + b"\0" * 600)


def test_no_libreoffice(monkeypatch):
    monkeypatch.setattr(word_doc.shutil, "which", lambda name: None)

    with pytest.raises(SheetError, match="LibreOffice"):
        parse_sheet(BytesIO(OLE_SIGNATURE + b"\0" * 64))


@pytest.mark.parametrize(
    ("raised", "message"),
    [
        (subprocess.TimeoutExpired("soffice", 40), "слишком много времени"),
        (FileNotFoundError("soffice"), "не запускается"),
    ],
)
def test_convert_failures(monkeypatch, raised, message):
    def run(*args, **kwargs):
        raise raised

    monkeypatch.setattr(word_doc.subprocess, "run", run)

    with pytest.raises(WordConvertError, match=message):
        doc_to_docx(OLE_SIGNATURE, soffice="/usr/bin/soffice")


def test_profile_is_private(monkeypatch):
    """Каждая конвертация — со своим профилем LibreOffice во временном каталоге."""
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(word_doc.subprocess, "run", run)

    for _ in range(2):
        with pytest.raises(WordConvertError):
            doc_to_docx(OLE_SIGNATURE, soffice="soffice")

    profiles = [command[1] for command, _ in calls]
    assert all(p.startswith("-env:UserInstallation=file://") for p in profiles)
    assert profiles[0] != profiles[1]
    assert calls[0][1]["timeout"] == word_doc.CONVERT_TIMEOUT_S
