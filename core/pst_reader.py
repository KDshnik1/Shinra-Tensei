"""Независимая проверка PST-файла по [MS-PST]: заголовок, контрольные суммы,
подписи, B-деревья, heap/BTH/PC/TC. Используется для самопроверки после экспорта."""
import struct

from .pst_writer import NATIVE, pst_crc, _sig, FIRST_AMAP, REGION, region_pages, mark_bits
from . import pst_writer
from .i18n import tr


class PstCheckError(Exception):
    pass


# Сообщение переводится и форматируется только при ошибке (проверок — миллионы).
def _chk(cond, msg, *args, **kw):
    if not cond:
        raise PstCheckError(tr(msg).format(*args, **kw) if args or kw else tr(msg))


BROKEN = 'повреждённая структура файла'   # цикл или слишком глубокая вложенность
MAX_BTREE_DEPTH = 16                      # уровней в B-дереве NBT/BBT
MAX_FOLDER_DEPTH = 250                    # вложенность папок


def _utf16(d: bytes) -> str:
    try:
        return d.decode("utf-16-le")
    except UnicodeDecodeError:
        raise PstCheckError(tr('строка не в UTF-16')) from None


class PstReader:
    def __init__(self, path):
        self.f = open(path, "rb")
        try:
            self._open()
        except BaseException:
            self.f.close()
            raise

    def _open(self):
        h = self.f.read(564)
        _chk(h[:4] == b"!BDN", 'нет сигнатуры !BDN')
        _chk(struct.unpack_from("<I", h, 4)[0] == pst_crc(h[8:479]), 'CRC заголовка (partial)')
        _chk(struct.unpack_from("<I", h, 524)[0] == pst_crc(h[8:524]), 'CRC заголовка (full)')
        _chk(struct.unpack_from("<H", h, 10)[0] >= 23, 'не Unicode PST')
        (_, self.eof, self.amap_last, _, _, nbt_bid, nbt_ib, bbt_bid, bbt_ib, _, _, _) = \
            struct.unpack_from("<IQQQQQQQQBBH", h, 180)
        self.f.seek(0, 2)
        _chk(self.f.tell() == self.eof, 'размер файла не совпадает с ibFileEof')
        _chk((self.eof - FIRST_AMAP) % REGION == 0, 'конец файла не на границе AMap')
        self.rgnid = struct.unpack_from("<32I", h, 44)
        self.bbt, self.nbt = {}, {}
        self.pages = []
        self._seen_pages = set()
        self._walk(bbt_ib, bbt_bid, 0x80, self.bbt)
        self._walk(nbt_ib, nbt_bid, 0x81, self.nbt)
        self._check_amaps()

    def _page(self, ib, bid, ptype):
        _chk(ib not in self._seen_pages, BROKEN)
        self._seen_pages.add(ib)
        self.pages.append(ib)
        _chk(ib + 512 <= self.eof, 'данные обрезаны')
        self.f.seek(ib)
        p = self.f.read(512)
        t, t2, sig, crc, pbid = struct.unpack_from("<BBHIQ", p, 496)
        _chk(t == ptype and t2 == ptype, 'тип страницы @{ib:#x}', ib=ib)
        _chk(crc == pst_crc(p[:496]), 'CRC страницы @{ib:#x}', ib=ib)
        _chk(pbid == bid, 'BID страницы @{ib:#x}', ib=ib)
        _chk(sig == _sig(ib, bid), 'подпись страницы @{ib:#x}', ib=ib)
        return p

    def _walk(self, ib, bid, ptype, out, depth=0):
        _chk(depth < MAX_BTREE_DEPTH, BROKEN)
        p = self._page(ib, bid, ptype)
        cent, cmax, cbent, level = p[488], p[489], p[490], p[491]
        prev = None
        for i in range(cent):
            e = p[i * cbent:(i + 1) * cbent]
            key = struct.unpack_from("<Q", e)[0]
            _chk(prev is None or key > prev, 'ключи B-дерева не по возрастанию')
            prev = key
            if level:
                _, cbid, cib = struct.unpack("<QQQ", e)
                self._walk(cib, cbid, ptype, out, depth + 1)
            elif ptype == 0x80:
                b, bib, cb, cref = struct.unpack_from("<QQHH", e)
                out[b] = (bib, cb)
            else:
                n, d, s, par = struct.unpack_from("<QQQI", e)
                out[n] = (d, s, par)

    def _check_amaps(self):
        """Служебные страницы на своих местах, и AMap покрывает их и все блоки."""
        nreg = (self.eof - FIRST_AMAP) // REGION
        actual = bytearray(nreg * 496)
        expect = bytearray(nreg * 496)
        names = {1: (0x83, "PMap"), 2: (0x82, "FMap"), 3: (0x85, "FPMap")}
        for k in range(nreg):
            start = FIRST_AMAP + k * REGION
            self.f.seek(start)
            p = self.f.read(512)
            _chk(p[496] == 0x84, 'нет AMap @{start:#x}', start=start)
            _chk(struct.unpack_from("<I", p, 500)[0] == pst_crc(p[:496]), f"CRC AMap @{start:#x}")
            actual[k * 496:(k + 1) * 496] = p[:496]
            for pg in region_pages(k):
                base = k * REGION // 64 + pg * 8
                mark_bits(expect, base, base + 8)
                if pg:
                    ptype, name = names[pg]
                    self.f.seek(start + pg * 512)
                    q = self.f.read(512)
                    _chk(q[496] == ptype and q[497] == ptype, 'нет {name} @{0:#x}', start + pg * 512, name=name)
                    _chk(struct.unpack_from("<I", q, 500)[0] == pst_crc(q[:496]),
                         f"CRC {name} @{start + pg * 512:#x}")
        for ib, cb in self.bbt.values():
            _chk(ib >= FIRST_AMAP, BROKEN)
            _chk(ib + (cb + 16 + 63) // 64 * 64 <= self.eof, 'данные обрезаны')
            a = (ib - FIRST_AMAP) // 64
            mark_bits(expect, a, a + (cb + 16 + 63) // 64)
        for ib in self.pages:
            _chk(ib >= FIRST_AMAP, BROKEN)
            a = (ib - FIRST_AMAP) // 64
            mark_bits(expect, a, a + 8)
        for k in range(nreg):
            e = int.from_bytes(expect[k * 496:(k + 1) * 496], "big")
            a = int.from_bytes(actual[k * 496:(k + 1) * 496], "big")
            _chk(e & ~a == 0, 'AMap @{0:#x} не отмечает занятые данные', FIRST_AMAP + k * REGION)

    # ---------- блоки и узлы ----------
    def block(self, bid):
        _chk(bid in self.bbt, 'блок {bid:#x} отсутствует в BBT', bid=bid)
        ib, cb = self.bbt[bid]
        total = (cb + 16 + 63) // 64 * 64
        _chk(ib + total <= self.eof, 'данные обрезаны')
        self.f.seek(ib)
        raw = self.f.read(total)
        tcb, sig, crc, tbid = struct.unpack_from("<HHIQ", raw, total - 16)
        _chk(tcb == cb and tbid == bid, 'трейлер блока {bid:#x}', bid=bid)
        _chk(sig == _sig(ib, bid), 'подпись блока {bid:#x}', bid=bid)
        _chk(crc == pst_crc(raw[:cb]), 'CRC блока {bid:#x}', bid=bid)
        return raw[:cb]

    def chunks(self, bid, depth=0):
        if bid == 0:
            return []
        data = self.block(bid)
        if not bid & 2:
            return [data]
        _chk(depth < 2, BROKEN)               # XBLOCK -> XXBLOCK -> данные, не глубже
        btype, level, cent, total = struct.unpack_from("<BBHI", data)
        _chk(btype == 1, 'ожидался XBLOCK')
        out = []
        for i in range(cent):
            sub = self.chunks(struct.unpack_from("<Q", data, 8 + 8 * i)[0], depth + 1)
            if i < cent - 1:
                _chk(sum(len(c) for c in sub) == (8176 if level == 1 else 8176 * 1021),
                     'неполный промежуточный блок в дереве {bid:#x} (Outlook это отвергает)', bid=bid)
            out += sub
        _chk(sum(len(c) for c in out) == total, 'lcbTotal не совпадает')
        return out

    def subnodes(self, bid, depth=0):
        res = {}
        if bid == 0:
            return res
        _chk(depth < 2, BROKEN)               # SIBLOCK -> SLBLOCK, не глубже
        data = self.block(bid)
        btype, level, cent = struct.unpack_from("<BBH", data)
        _chk(btype == 2, 'ожидался SLBLOCK/SIBLOCK')
        for i in range(cent):
            if level == 0:
                n, d, s = struct.unpack_from("<QQQ", data, 8 + 24 * i)
                res[n] = (d, s)
            else:
                n, b = struct.unpack_from("<QQ", data, 8 + 16 * i)
                res.update(self.subnodes(b, depth + 1))
        return res

    # ---------- LTP ----------
    class _HN:
        def __init__(self, reader, chunks, subs):
            self.r, self.pages, self.subs = reader, chunks, subs
            _chk(chunks and chunks[0][2] == 0xEC, 'нет сигнатуры heap 0xEC')
            self.client = chunks[0][3]
            self.root = struct.unpack_from("<I", chunks[0], 4)[0]
            self.maps = []
            for p in chunks:
                ibm = struct.unpack_from("<H", p)[0]
                calloc = struct.unpack_from("<H", p, ibm)[0]
                self.maps.append(struct.unpack_from(f"<{calloc + 1}H", p, ibm + 4))

        def get(self, hid):
            if hid == 0:
                return b""
            if hid & 0x1F:
                _chk(hid in self.subs, 'подузел {hid:#x} не найден', hid=hid)
                return b"".join(self.r.chunks(self.subs[hid][0]))
            idx, blk = (hid >> 5) & 0x7FF, hid >> 16
            _chk(blk < len(self.maps), 'HID {hid:#x} вне карты heap', hid=hid)
            m = self.maps[blk]
            _chk(1 <= idx < len(m), 'HID {hid:#x} вне карты heap', hid=hid)
            _chk(m[idx - 1] <= m[idx] <= len(self.pages[blk]), 'данные обрезаны')
            return self.pages[blk][m[idx - 1]:m[idx]]

        def bth(self, hid):
            h = self.get(hid)
            t, ck, ce, lv, root = struct.unpack("<BBBBI", h)
            _chk(t == 0xB5, 'нет сигнатуры BTH')
            out = []

            def walk(hid_, lvl):
                d = self.get(hid_)
                step = ck + (ce if lvl == 0 else 4)
                _chk(len(d) % step == 0, 'размер узла BTH')
                for i in range(0, len(d), step):
                    k = int.from_bytes(d[i:i + ck], "little")
                    if lvl:
                        walk(struct.unpack_from("<I", d, i + ck)[0], lvl - 1)
                    else:
                        out.append((k, d[i + ck:i + step]))
            if root:
                walk(root, lv)
            keys = [k for k, _ in out]
            _chk(keys == sorted(keys), 'BTH не отсортирован')
            return out

    def _hn(self, bid_data, bid_sub):
        return self._HN(self, self.chunks(bid_data), self.subnodes(bid_sub))

    def pc(self, bid_data, bid_sub):
        hn = self._hn(bid_data, bid_sub)
        _chk(hn.client == 0xBC, 'ожидался Property Context')
        props = {}
        for pid, rec in hn.bth(hn.root):
            ptype, dw = struct.unpack("<HI", rec)
            tag = (pid << 16) | ptype
            if ptype in (0x0003, 0x0002, 0x000B):
                props[tag] = dw
            else:
                data = hn.get(dw)
                props[tag] = _utf16(data) if ptype == 0x1F else data
        return props

    def tc(self, bid_data, bid_sub):
        hn = self._hn(bid_data, bid_sub)
        _chk(hn.client == 0x7C, 'ожидался Table Context')
        info = hn.get(hn.root)
        _, ncol, t4, t2, t1, tbm, hid_ri, hnid_rows, _ = struct.unpack_from("<BB4HIII", info)
        cols = [struct.unpack_from("<IHBB", info, 22 + 8 * i) for i in range(ncol)]
        for tag, ib, cb, bit in cols:
            if tag == 0x67F20003:
                _chk(ib == 0 and cb == 4 and bit == 0, 'COLDESC для RowID должен быть ib=0, iBit=0')
            if tag == 0x67F30003:
                _chk(ib == 4 and cb == 4 and bit == 1, 'COLDESC для RowVer должен быть ib=4, iBit=1')
        index = hn.bth(hid_ri)
        rows = []
        if hnid_rows:
            if hnid_rows & 0x1F:
                _chk(hnid_rows in hn.subs, 'подузел {hid:#x} не найден', hid=hnid_rows)
                blocks = self.chunks(hn.subs[hnid_rows][0])
            else:
                blocks = [hn.get(hnid_rows)]
            _chk(0 < tbm <= 8176, 'строка таблицы обрезана')
            per = 8176 // tbm
            for rid, ridx in index:
                n = struct.unpack("<I", ridx)[0]
                _chk(n // per < len(blocks), 'строка таблицы обрезана')
                blk = blocks[n // per]
                row = blk[(n % per) * tbm:(n % per + 1) * tbm]
                _chk(len(row) == tbm, 'строка таблицы обрезана')
                _chk(struct.unpack_from("<I", row)[0] == rid, 'RowID не совпадает с индексом')
                vals = {}
                for tag, ib, cb, bit in cols:
                    if not row[t1 + bit // 8] & (0x80 >> (bit % 8)):
                        continue
                    _chk(ib + cb <= len(row), 'строка таблицы обрезана')
                    cell = row[ib:ib + cb]
                    if (tag & 0xFFFF) in (0x1F, 0x102):
                        d = hn.get(struct.unpack("<I", cell)[0])
                        vals[tag] = _utf16(d) if tag & 0xFFFF == 0x1F else d
                    else:
                        vals[tag] = int.from_bytes(cell, "little")
                rows.append((rid, vals))
        return rows

    def node(self, nid):
        _chk(nid in self.nbt, 'узел {nid:#x} отсутствует в NBT', nid=nid)
        return self.nbt[nid]

    def node_pc(self, nid):
        d, s, _ = self.node(nid)
        return self.pc(d, s)

    def node_tc(self, nid):
        d, s, _ = self.node(nid)
        return self.tc(d, s)


def verify(path) -> dict:
    """Полная проверка файла. Возвращает сводку: {папка: число писем}.
    Повреждённый файл -> PstCheckError (а не IndexError, struct.error и т.п.)."""
    try:
        return _verify(path)
    except PstCheckError:
        raise
    except (struct.error, IndexError, ValueError, ZeroDivisionError, TypeError, KeyError):
        raise PstCheckError(tr('данные обрезаны')) from None
    except RecursionError:
        raise PstCheckError(tr(BROKEN)) from None


def _verify(path) -> dict:
    r = PstReader(path)
    try:
        return _check(r)
    finally:
        r.f.close()


def _check(r) -> dict:
    for nid in r.nbt:
        _chk((nid >> 5) < r.rgnid[nid & 0x1F], 'счётчик NID в заголовке меньше узла {nid:#x}', nid=nid)
    store = r.node_pc(0x21)
    _chk(0x67FF0003 in store, 'нет PR_PST_PASSWORD')
    _chk(0x3001001F in r.node_pc(0x122), 'у корневой папки нет имени')
    _chk(0x35E00102 in store, 'нет IPM Subtree в хранилище')
    summary = {"_store": store.get(0x3001001F, ""), "_password": bool(store.get(0x67FF0003))}

    seen = {0x122}

    def walk(nid, path_, depth=0):
        _chk(depth < MAX_FOLDER_DEPTH, BROKEN)
        for child, vals in r.node_tc((nid & ~0x1F) | 0x0D):
            _chk(child not in seen, BROKEN)       # папка встречается дважды — цикл
            seen.add(child)
            pc = r.node_pc(child)
            name = pc.get(0x3001001F, "?")
            rows = r.node_tc((child & ~0x1F) | 0x0E)
            _chk(len(rows) == pc.get(0x36020003, 0), 'счётчик писем папки «{name}»', name=name)
            for mid, _ in rows:
                d, s, par = r.node(mid)
                _chk(par == child, 'родитель письма')
                m = r.pc(d, s)
                subs = r.subnodes(s)
                if 0x692 in subs:
                    r.tc(*subs[0x692])
                if 0x671 in subs:
                    for anid, _ in r.tc(*subs[0x671]):
                        _chk(anid in subs, 'подузел {hid:#x} не найден', hid=anid)
                        a = r.pc(*subs[anid])
                        _chk(0x37010102 in a, 'у вложения нет данных')
                _chk(0x001A001F in m, 'нет класса письма')
            summary[path_ + name] = len(rows)
            walk(child, path_ + name + "/", depth + 1)
    walk(0x122, "")
    return summary


# Нативная проверка (Rust, модуль shinra_core) — те же проверки, в разы быстрее.
_py_verify = verify
if NATIVE:
    _native = pst_writer._native

    def verify(path) -> dict:
        try:
            return _native.verify(path)
        except _native.PstCheckError as e:
            raise PstCheckError(str(e)) from None
