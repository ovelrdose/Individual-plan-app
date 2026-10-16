"""Старый формат Word (.doc) → .docx через LibreOffice. Без Django.

python-docx читает только .docx, а листы назначений из МИС и старых шаблонов бывают .doc.
Надёжно разобрать таблицы .doc на чистом Python нечем, поэтому файл пересохраняется
LibreOffice (`soffice --headless`), а дальше идёт обычный разбор (prescription_sheet).
"""

import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

# Сигнатура составного документа OLE2 — в нём хранятся .doc (а также .xls, .ppt).
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Меньше таймаута gunicorn (60 с): врач должен увидеть ошибку, а не обрыв соединения.
CONVERT_TIMEOUT_S = 40


class WordConvertError(ValueError):
    """Файл .doc не удалось пересохранить в .docx; текст — для врача."""


def is_word97(head: bytes) -> bool:
    """Файл в старом формате Word (OLE2) — по первым байтам, а не по расширению: переименованный
    .doc тоже должен читаться."""
    return head.startswith(OLE_SIGNATURE)


def doc_to_docx(data: bytes, soffice: str | None = None) -> BytesIO:
    """Пересохраняет .doc в .docx и возвращает содержимое нового файла."""
    soffice = soffice or shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise WordConvertError(
            "Старый формат .doc на сервере не читается (не установлен LibreOffice). "
            "Откройте файл в Word и сохраните как .docx."
        )
    with tempfile.TemporaryDirectory(prefix="omr4-doc-") as tmp:
        work = Path(tmp)
        source = work / "sheet.doc"
        source.write_bytes(data)
        # Свой профиль на каждый вызов: общий профиль LibreOffice не даёт запускать
        # несколько конвертаций одновременно, а домашний каталог сервиса закрыт на запись.
        command = [
            soffice,
            f"-env:UserInstallation={(work / 'profile').as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            "docx:MS Word 2007 XML",
            "--outdir",
            str(work),
            str(source),
        ]
        try:
            subprocess.run(
                command,
                cwd=work,
                env=os.environ | {"HOME": str(work)},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=CONVERT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise WordConvertError(
                "Файл .doc не удалось прочитать: преобразование заняло слишком много времени. "
                "Сохраните его в Word как .docx."
            ) from error
        except OSError as error:
            raise WordConvertError(
                "Файл .doc не удалось прочитать: не запускается LibreOffice. "
                "Сохраните его в Word как .docx."
            ) from error
        result = work / "sheet.docx"
        # LibreOffice и на повреждённом файле нередко завершается с кодом 0 — смотрим на результат.
        if not result.is_file() or result.stat().st_size == 0:
            raise WordConvertError(
                "Файл .doc повреждён или это не документ Word. Сохраните его в Word как .docx."
            )
        return BytesIO(result.read_bytes())
