"""Язык интерфейса (русский / английский).

Исходные строки в коде — русские: tr("Текст") возвращает перевод для текущего языка.
Язык читается из настроек при запуске; смена языка применяется после перезапуска.
Переводы — в core/lang_en.py.
"""
import json
import os
from pathlib import Path

LANGUAGES = [("ru", "Русский"), ("en", "English")]
_CODES = {code for code, _name in LANGUAGES}


def _config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "Shinra Tensei" / "config.json"


def system_language() -> str:
    """Язык Windows: русский интерфейс -> "ru", любой другой -> "en"."""
    try:
        import ctypes
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "ru" if (lang_id & 0x3FF) == 0x19 else "en"
    except Exception:
        pass
    try:
        import locale
        loc = (locale.getlocale()[0] or "").lower()
        return "ru" if loc.startswith("ru") else "en"
    except Exception:
        return "en"


def _detect() -> str:
    try:
        lang = json.loads(_config_path().read_text(encoding="utf-8")).get("language")
        if lang in _CODES:
            return lang
    except Exception:
        pass
    return system_language()


LANG = _detect()
_table = None


def tr(text: str) -> str:
    global _table
    if LANG == "ru":
        return text
    if _table is None:
        from .lang_en import EN
        _table = EN
    return _table.get(text, text)


# ---------- форматы дат и чисел ----------
DATE_FMT = "%d.%m.%Y" if LANG == "ru" else "%Y-%m-%d"           # для strftime
DATETIME_FMT = DATE_FMT + " %H:%M"
QT_DATE_FMT = "dd.MM.yyyy" if LANG == "ru" else "yyyy-MM-dd"     # для QDateEdit


def num(n: int) -> str:
    """Разделитель разрядов: «1 234 567» (ru) / «1,234,567» (en)."""
    s = f"{int(n):,}"
    return s.replace(",", " ") if LANG == "ru" else s


def dec(s: str) -> str:
    """Десятичный разделитель: «2,5» (ru) / «2.5» (en)."""
    return s.replace(".", ",") if LANG == "ru" else s
