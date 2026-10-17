"""Модель почтового ящика и хранение настроек программы."""
import base64
import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path
from . import i18n
from .i18n import DATE_FMT, tr

APP_NAME = "Shinra Tensei"
OLD_APP_NAME = "MailArchiver"

SECURITY_OPTIONS = [
    ("SSL/TLS (IMAPS)", "ssl", 993),
    ("STARTTLS", "starttls", 143),
    (tr('Без шифрования'), "none", 143),
]


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    p = Path(base) / APP_NAME
    old = Path(base) / OLD_APP_NAME
    if not p.exists() and old.exists():
        # перенос настроек и журналов со старого названия программы
        import shutil
        try:
            shutil.copytree(old, p)
        except OSError:
            pass
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_archive_dir() -> Path:
    return Path.home() / "Documents" / APP_NAME / "Archive"


def default_pst_dir() -> Path:
    return Path.home() / "Documents" / APP_NAME / "PST"


# ---------- шифрование пароля (DPAPI — привязан к учётной записи Windows) ----------

def protect(text: str) -> str:
    if not text:
        return ""
    try:
        import win32crypt
        blob = win32crypt.CryptProtectData(text.encode("utf-8"), APP_NAME, None, None, None, 0)
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    except Exception:
        return "b64:" + base64.b64encode(text.encode("utf-8")).decode("ascii")


def unprotect(value: str) -> str:
    if not value:
        return ""
    try:
        if value.startswith("dpapi:"):
            import win32crypt
            _, data = win32crypt.CryptUnprotectData(base64.b64decode(value[6:]), None, None, None, 0)
            return data.decode("utf-8")
        if value.startswith("b64:"):
            return base64.b64decode(value[4:]).decode("utf-8")
    except Exception:
        pass
    return ""


@dataclass
class Account:
    email: str = ""
    host: str = ""
    port: int = 993
    security: str = "ssl"              # ssl | starttls | none
    accept_all_certs: bool = False
    login: str = ""
    password: str = field(default="", repr=False)
    save_password: bool = True
    date_mode: str = "all"             # all | range
    date_from: str = ""                # ISO yyyy-mm-dd
    date_to: str = ""
    folder_mode: str = "all"           # all | one
    folder_raw: str = ""               # имя папки как на сервере (mUTF-7)
    folder_display: str = ""
    duplicates: str = "auto"           # повторы писем из других папок: auto | always | never
    connections: int = 1               # соединений с сервером при загрузке этого ящика
    archive_dir: str = ""              # своя папка для .eml; пусто — общая папка архива
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def period_text(self) -> str:
        if self.date_mode != "range":
            return tr('За всё время')
        f = date.fromisoformat(self.date_from).strftime(DATE_FMT) if self.date_from else "…"
        t = date.fromisoformat(self.date_to).strftime(DATE_FMT) if self.date_to else "…"
        return f"{f} – {t}"

    def folder_text(self) -> str:
        if self.folder_mode == "one":
            return self.folder_display or self.folder_raw
        return tr('Все папки')

    def security_text(self) -> str:
        for text, val, _ in SECURITY_OPTIONS:
            if val == self.security:
                return text
        return self.security


class Config:
    # ---------- где лежат письма ящика ----------
    def archive_root_for(self, acc) -> str:
        return (acc.archive_dir or "").strip() or self.archive_dir

    def mailbox_dir(self, acc) -> Path:
        from .storage import MailboxArchive
        return MailboxArchive(self.archive_root_for(acc), acc.email).dir

    def account_by_email(self, email: str):
        key = email.strip().lower()
        for a in self.accounts:
            if a.email.strip().lower() == key:
                return a
        return None

    def mailbox_dir_by_email(self, email: str) -> Path:
        from .storage import MailboxArchive
        acc = self.account_by_email(email)
        return self.mailbox_dir(acc) if acc else MailboxArchive(self.archive_dir, email).dir

    def __init__(self):
        self.path = app_data_dir() / "config.json"
        self.accounts: list[Account] = []
        self.archive_dir = str(default_archive_dir())
        self.pst_dir = str(default_pst_dir())
        self.parallel = 3
        self.theme = "system"          # system | light | dark
        self.language = i18n.LANG      # ru | en (применяется после перезапуска)
        self.split_gb = 10.0           # размер одной части PST, ГБ
        self.export_skip_duplicates = True   # не экспортировать повторы писем

    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.archive_dir = data.get("archive_dir", self.archive_dir)
        self.pst_dir = data.get("pst_dir", self.pst_dir)
        self.parallel = int(data.get("parallel", self.parallel))
        self.theme = data.get("theme", self.theme)
        if data.get("language") in dict(i18n.LANGUAGES):
            self.language = data["language"]
        try:
            self.split_gb = float(data.get("split_gb", self.split_gb))
        except (TypeError, ValueError):
            pass
        self.export_skip_duplicates = bool(data.get("export_skip_duplicates",
                                                    self.export_skip_duplicates))
        known = {f.name for f in fields(Account)}
        for raw in data.get("accounts", []):
            enc = raw.pop("password_enc", "")
            if "duplicates" not in raw and raw.get("skip_duplicates") is False:
                raw["duplicates"] = "never"        # настройка версий 0.13–0.14
            acc = Account(**{k: v for k, v in raw.items() if k in known})
            acc.password = unprotect(enc)
            self.accounts.append(acc)

    def save(self):
        accs = []
        for a in self.accounts:
            d = asdict(a)
            pw = d.pop("password")
            d["password_enc"] = protect(pw) if a.save_password else ""
            accs.append(d)
        data = {
            "archive_dir": self.archive_dir,
            "pst_dir": self.pst_dir,
            "parallel": self.parallel,
            "theme": self.theme,
            "language": self.language,
            "split_gb": self.split_gb,
            "export_skip_duplicates": self.export_skip_duplicates,
            "accounts": accs,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
