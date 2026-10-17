"""Небольшой IMAP-сервер для тестов загрузки (только то, что использует программа).

folders: {имя: {"flags": [...], "messages": {uid: bytes}}}. Режимы (opt):
  latency     — задержка ответа на каждую команду, с
  flags_after — FLAGS и INTERNALDATE после тела письма
  missing     — UID, которых «нет» при выдаче писем (удалены после поиска)
  unsolicited — перед каждым письмом незапрошенный «* N FETCH (FLAGS (...))»
  drop_after  — один раз оборвать соединение, отдав столько писем
  no_multi    — отвечать BAD на FETCH нескольких писем
  no_size     — отвечать BAD на запрос размеров
  no_msgid    — отвечать BAD на запрос заголовка Message-ID (размеры — как обычно)
  max_conns   — не больше стольких соединений одновременно, лишним — BYE
  bandwidth   — скорость выдачи писем одним соединением, байт/с (как у серверов с лимитом)
state.max_active — сколько соединений было открыто одновременно (наибольшее);
state.cmds — порядок запросов писем: "size", "meta" (с Message-ID) или "body".
"""
import socket
import socketserver
import threading
import time

UIDVALIDITY = "777"
IDATE = '"05-Jan-2015 10:00:00 +0300"'


def _parse_set(s, have):
    out = []
    for part in s.split(","):
        if ":" in part:
            a, b = part.split(":")
            hi = max(have, default=0) if b == "*" else int(b)
            out += [u for u in sorted(have) if int(a) <= u <= hi]
        elif int(part) in have:
            out.append(int(part))
    return out


def _header_field(msg: bytes, name: bytes) -> bytes:
    head = msg.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    lines, take = [], False
    for line in head.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t"):
            if take:
                lines.append(line)
            continue
        take = line.lower().startswith(name.lower() + b":")
        if take:
            lines.append(line)
    return b"".join(lines) + b"\r\n"


class _Handler(socketserver.StreamRequestHandler):
    def w(self, b):
        self.wfile.write(b)

    def ok(self, tag, text="done"):
        self.w(f"{tag} OK {text}\r\n".encode())

    def handle(self):
        st = self.server.state
        with st.lock:
            st.active += 1
            st.max_active = max(st.max_active, st.active)
            refuse = st.opt.get("max_conns") is not None and st.active > st.opt["max_conns"]
        try:
            if refuse:
                self.w(b"* BYE too many connections\r\n")
                self.wfile.flush()
                return
            self._session(st)
        finally:
            with st.lock:
                st.active -= 1

    def _session(self, st):
        opt = st.opt
        self.w(b"* OK fake IMAP ready\r\n")
        self.wfile.flush()
        cur = None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            tag, _, rest = line.decode().rstrip("\r\n").partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            if opt.get("latency"):
                time.sleep(opt["latency"])
            if cmd == "CAPABILITY":
                self.w(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n")
                self.ok(tag)
            elif cmd == "LOGIN":
                self.ok(tag, "logged in")
            elif cmd == "LIST":
                for name, f in st.folders.items():
                    flags = " ".join(["\\HasNoChildren"] + f.get("flags", []))
                    self.w(f'* LIST ({flags}) "/" "{name}"\r\n'.encode())
                self.ok(tag)
            elif cmd in ("SELECT", "EXAMINE"):
                cur = args.strip().strip('"')
                n = len(st.folders[cur]["messages"])
                self.w(f"* {n} EXISTS\r\n* OK [UIDVALIDITY {UIDVALIDITY}] ok\r\n".encode())
                self.ok(tag, "[READ-ONLY] done")
            elif cmd == "LOGOUT":
                self.w(b"* BYE\r\n")
                self.ok(tag, "bye")
                self.wfile.flush()
                return
            elif cmd == "UID":
                if not self._uid(tag, args, st.folders[cur]["messages"], opt, st):
                    return
            else:
                self.ok(tag)
            self.wfile.flush()

    def _uid(self, tag, args, msgs, opt, st) -> bool:
        sub, _, a2 = args.partition(" ")
        order = sorted(msgs)
        if sub.upper() == "SEARCH":
            self.w(("* SEARCH " + " ".join(str(u) for u in order) + "\r\n").encode())
            self.ok(tag)
            return True
        sset, _, items = a2.partition(" ")
        have = set(msgs) - set(opt.get("missing", ()))
        uids = _parse_set(sset, have)
        up = items.upper()
        if "BODY.PEEK[]" not in up:                     # размеры / Message-ID
            with st.lock:
                st.cmds.append("meta" if "MESSAGE-ID" in up else "size")
            if opt.get("no_size") or (opt.get("no_msgid") and "MESSAGE-ID" in up):
                self.w(f"{tag} BAD not supported\r\n".encode())
                return True
            for u in uids:
                seq = order.index(u) + 1
                if "MESSAGE-ID" in up:
                    h = _header_field(msgs[u], b"Message-ID")
                    self.w(f"* {seq} FETCH (UID {u} RFC822.SIZE {len(msgs[u])} "
                           f"BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(h)}}}\r\n".encode() + h + b")\r\n")
                else:
                    self.w(f"* {seq} FETCH (UID {u} RFC822.SIZE {len(msgs[u])})\r\n".encode())
            self.ok(tag)
            return True
        if opt.get("no_multi") and ("," in sset or ":" in sset):
            self.w(f"{tag} BAD too many\r\n".encode())
            return True
        with st.lock:
            st.fetch_cmds += 1
            st.cmds.append("body")
        for u in uids:
            with st.lock:
                drop = (opt.get("drop_after") is not None and not st.dropped
                        and st.bodies_sent >= opt["drop_after"])
                if drop:
                    st.dropped = True
            if drop:
                self.wfile.flush()
                self.request.shutdown(socket.SHUT_RDWR)
                return False
            seq = order.index(u) + 1
            body = msgs[u]
            if opt.get("unsolicited"):
                self.w(f"* {seq + 1} FETCH (FLAGS (\\Deleted))\r\n".encode())
            if opt.get("flags_after"):
                self.w(f"* {seq} FETCH (UID {u} BODY[] {{{len(body)}}}\r\n".encode() + body
                       + f" FLAGS (\\Seen) INTERNALDATE {IDATE})\r\n".encode())
            else:
                self.w(f"* {seq} FETCH (UID {u} FLAGS (\\Seen) INTERNALDATE {IDATE} "
                       f"BODY[] {{{len(body)}}}\r\n".encode() + body + b")\r\n")
            if opt.get("bandwidth"):
                time.sleep(len(body) / opt["bandwidth"])
            with st.lock:
                st.bodies_sent += 1
        self.ok(tag)
        return True


class _State:
    def __init__(self, folders, opt):
        self.folders = folders
        self.opt = opt
        self.dropped = False
        self.fetch_cmds = 0
        self.bodies_sent = 0
        self.active = self.max_active = 0
        self.cmds = []
        self.lock = threading.Lock()


class FakeImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, folders, **opt):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = _State(folders, opt)
        self._t = threading.Thread(target=self.serve_forever, daemon=True)
        self._t.start()

    @property
    def port(self):
        return self.server_address[1]

    def close(self):
        self.shutdown()
        self.server_close()
