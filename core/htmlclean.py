"""Очистка HTML письма перед показом в просмотрщике (то же делает native/src/html.rs).

Просмотрщик Qt понимает только простой HTML: он не умеет display:none, поэтому
показывает скрытый текст рассылок, а на месте картинок из интернета (их мы не
загружаем) оставляет пустые места. Здесь убирается то, что показать нельзя или не
нужно, а остальное передаётся как есть. Сами письма в архиве не меняются."""
from html import escape
from html.parser import HTMLParser

# удаляются вместе с содержимым: исполняемое, встроенные окна, элементы форм
DROP = {"script", "title", "iframe", "frame", "frameset", "object", "embed", "applet",
        "noembed", "template", "svg", "math", "select", "textarea", "button", "canvas",
        "audio", "video"}
# удаляется только сам тег, содержимое остаётся
STRIP = {"meta", "link", "base", "form", "input"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
URL_ATTRS = {"href", "src", "background", "action", "formaction", "poster", "cite", "longdesc",
             "usemap", "xlink:href", "dynsrc", "lowsrc"}

TEXT, RAW, START, END = range(4)


class _Tokens(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.toks = []

    def handle_starttag(self, tag, attrs):
        self.toks.append((START, tag, attrs, False))

    def handle_startendtag(self, tag, attrs):
        self.toks.append((START, tag, attrs, True))

    def handle_endtag(self, tag):
        self.toks.append((END, tag))

    def handle_data(self, data):
        # внутри <style>/<script> текст приходит как есть
        self.toks.append((RAW if self.cdata_elem else TEXT, data))


def _attr(attrs, name):
    for n, v in attrs:
        if n == name:
            return v or ""
    return None


def _hidden(attrs) -> bool:
    """Элемент скрыт стилем или атрибутом hidden (так прячут «прехедеры» рассылок)."""
    if _attr(attrs, "hidden") is not None:
        return True
    style = _attr(attrs, "style")
    if style is None:
        return False
    st = "".join(style.split()).lower()
    if "display:none" in st or "visibility:hidden" in st:
        return True
    p = st.find("max-height:0")
    if "overflow:hidden" in st and p >= 0:
        rest = st[p + len("max-height:0"):]
        return not (rest[:1].isdigit() or rest[:1] == ".")
    return False


def _bad_url(attr: str, value: str) -> bool:
    """Ссылка, которую нельзя оставлять: скрипт или встроенный документ."""
    v = "".join(c for c in value if not (c.isascii() and (c.isspace() or ord(c) < 32 or ord(c) == 127))
                and not (0x80 <= ord(c) <= 0x9f)).lower()
    if v.startswith(("javascript:", "vbscript:")):
        return True
    return v.startswith("data:") and not (attr == "src" and v.startswith("data:image/"))


def _tiny(attrs) -> bool:
    for n in ("width", "height"):
        v = _attr(attrs, n)
        if v is None:
            continue
        v = v.strip()
        v = v[:-2] if v.endswith("px") else v
        try:
            if float(v.strip()) <= 2:
                return True
        except ValueError:
            pass
    return False


def _start(name, attrs, closed) -> str:
    out = ["<", name]
    for n, v in attrs:
        if n.startswith("on"):
            continue
        if v is not None and n in URL_ATTRS and _bad_url(n, v):
            continue
        out.append(" " + n if v is None else f' {n}="{escape(v)}"')
    out.append(" />" if closed else ">")
    return "".join(out)


def clean_html(h: str) -> str:
    """HTML письма, пригодный для показа: без скриптов, скрытого текста и картинок из сети."""
    p = _Tokens()
    p.feed(h)
    p.close()
    toks = p.toks
    # пара для каждого открывающего тега: ближайший закрывающий с тем же именем
    close_of, open_ = {}, {}
    for k, t in enumerate(toks):
        if t[0] == START and not t[3] and t[1] not in VOID:
            open_.setdefault(t[1], []).append(k)
        elif t[0] == END and open_.get(t[1]):
            close_of[open_[t[1]].pop()] = k
    out = []
    k = 0
    while k < len(toks):
        t = toks[k]
        if t[0] == TEXT:
            out.append(escape(t[1], quote=False))
        elif t[0] == RAW:
            out.append(t[1])
        elif t[0] == END:
            if t[1] not in DROP and t[1] not in STRIP:
                out.append(f"</{t[1]}>")
        else:
            _, name, attrs, closed = t
            void = closed or name in VOID
            if name in DROP:
                # без закрывающего тега — до конца текста
                k = k + 1 if void else close_of.get(k, len(toks) - 1) + 1
                continue
            if _hidden(attrs):
                if void:
                    k += 1
                    continue
                if k in close_of:
                    k = close_of[k] + 1
                    continue
                # не закрыт: прятать нечего, иначе пропало бы всё письмо до конца
            if name in STRIP:
                k += 1
                continue
            if name == "img":
                src = (_attr(attrs, "src") or "").strip().lower()
                if not src.startswith(("cid:", "data:image/")):
                    # картинки из интернета не загружаются: вместо пустого места — подпись
                    if not _tiny(attrs):
                        out.append(escape((_attr(attrs, "alt") or "").strip(), quote=False))
                    k += 1
                    continue
            out.append(_start(name, attrs, closed))
        k += 1
    return "".join(out)
