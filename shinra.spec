# -*- mode: python -*-
# Сборка Shinra Tensei для Windows:  python -m PyInstaller --noconfirm --clean shinra.spec
# Результат — папка dist\Shinra Tensei\ (exe + _internal). Папка, а не один exe: onefile при
# каждом запуске распаковывает Python и Qt во %TEMP%, а Defender проверяет каждую DLL заново.
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH)
APP = "Shinra Tensei"
ICON = str(ROOT / "assets" / "shinra.ico")

# версия — из верхнего раздела CHANGELOG.md («## [0.16.0] — …»)
m = re.search(r"^## \[(\d+)\.(\d+)\.(\d+)\]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
ver = tuple(int(x) for x in m.groups()) + (0,) if m else (0, 0, 0, 0)
ver_s = ".".join(map(str, ver[:3]))
version_info = None
if sys.platform == "win32":     # вкладка «Подробно» в свойствах exe
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                     StringTable, VarFileInfo, VarStruct,
                                                     VSVersionInfo)
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=ver, prodvers=ver),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "KDshnik1"),
                StringStruct("FileDescription", f"{APP} — IMAP to PST"),
                StringStruct("FileVersion", ver_s),
                StringStruct("InternalName", APP),
                StringStruct("LegalCopyright", "MIT License"),
                StringStruct("OriginalFilename", f"{APP}.exe"),
                StringStruct("ProductName", APP),
                StringStruct("ProductVersion", ver_s),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

a = Analysis(
    ["app.py"],
    pathex=[str(ROOT)],
    datas=[(ICON, "assets")],
    hiddenimports=["win32timezone", "shinra_core"],
    # программе нужны только QtCore/QtGui/QtWidgets
    excludes=["PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtPdf", "tkinter"],
    noarchive=False,
    optimize=2,          # байткод без assert и docstring
)

# Лишнее из Qt: программный OpenGL (~20 МБ), экранная клавиатура (тянет Qt Quick и QML),
# просмотр PDF как картинки, сеть Qt (почту качает своё ядро), редкие форматы картинок
# и переводы Qt на другие языки.
_DROP = re.compile(
    r"(^|[/\\])("
    r"opengl32sw\.dll"
    r"|(lib)?Qt6(Quick\w*|Qml\w*|VirtualKeyboard\w*|Pdf\w*|Network|OpenGL)\b[^/\\]*"
    r"|(lib)?qtvirtualkeyboardplugin[^/\\]*"
    r"|(lib)?q(pdf|icns|tga|wbmp)\.(dll|so)"
    r")$", re.I)
_TR = re.compile(r"[/\\]translations[/\\](qt|qtbase|qt_help|qtmultimedia|qtconnectivity|qtdeclarative"
                 r"|qtlocation|qtserialport|qtwebsockets|qtwebengine|qtquickcontrols2?)_([a-zA-Z_]+)\.qm$")
_KEEP_LANGS = {"ru", "en"}


def _keep(dest):
    if _DROP.search(dest):
        return False
    t = _TR.search(dest)
    if t:
        return t.group(1) in ("qt", "qtbase") and t.group(2) in _KEEP_LANGS
    return True


a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    icon=ICON,
    version=version_info,
    console=False,
    upx=False,           # UPX часто даёт ложные срабатывания антивирусов
    debug=False,
    strip=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name=APP,
    upx=False,
    strip=False,
)
