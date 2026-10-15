"""Modified UTF-7 (RFC 3501, 5.1.3) — кодировка имён папок IMAP.

Нужна, чтобы корректно показывать и открывать папки с кириллицей,
например «&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-» -> «Отправленные».
"""
import base64


def decode(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c != "&":
            out.append(c)
            i += 1
            continue
        j = s.find("-", i)
        if j == -1:                      # битое имя — отдаём как есть
            out.append(s[i:])
            break
        if j == i + 1:                   # "&-" означает символ "&"
            out.append("&")
        else:
            chunk = s[i + 1:j].replace(",", "/")
            chunk += "=" * (-len(chunk) % 4)
            try:
                out.append(base64.b64decode(chunk).decode("utf-16-be"))
            except Exception:
                out.append(s[i:j + 1])
        i = j + 1
    return "".join(out)


def encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for c in s:
        if 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            buf.append(c)
    flush()
    return "".join(out)
