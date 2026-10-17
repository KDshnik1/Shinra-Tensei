"""Описание выпуска из CHANGELOG: раздел этой версии по-русски, затем по-английски.

    python .github/release_notes.py v0.13.0 notes.md
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def section(path: Path, version: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def main():
    version = sys.argv[1].removeprefix("refs/tags/").lstrip("v")
    ru = section(ROOT / "CHANGELOG.ru.md", version)
    en = section(ROOT / "CHANGELOG.md", version)
    if not (ru or en):
        print(f"::warning::в CHANGELOG нет раздела [{version}]", file=sys.stderr)
        repo = os.environ.get("GITHUB_REPOSITORY", "KDshnik1/Shinra-Tensei")
        ru = f"Список изменений — в [CHANGELOG.ru.md](https://github.com/{repo}/blob/main/CHANGELOG.ru.md)."
    parts = [ru] if ru else []
    if en:
        parts.append("<details><summary>English</summary>\n\n" + en + "\n\n</details>")
    # файл пишем сами: перенаправление в PowerShell портит кириллицу
    Path(sys.argv[2]).write_text("\n\n".join(parts) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
