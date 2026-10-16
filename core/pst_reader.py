"""Независимая проверка PST-файла по [MS-PST]: заголовок, контрольные суммы,
подписи, B-деревья, heap/BTH/PC/TC. Используется для самопроверки после экспорта."""
import struct

from .pst_writer import pst_crc, _sig, FIRST_AMAP, REGION, region_pages, mark_bits
from .i18n import tr


class PstCheckError(Exception):
    pass


def _chk(cond, msg):
    if not cond:
        raise PstCheckError(msg)


class PstReader:
    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(564)
        _chk(h[:4] == b"!BDN", tr('нет сигнатуры !BDN'))
        _chk(struct.unpack_from("<I", h, 4)[0] == pst_crc(h[8:479]), tr('CRC заголовка (partial)'))
        _chk(struct.unpack_from("<I", h, 524)[0] == pst_crc(h[8:524]), tr('CRC заголовка (full)'))
        _chk(struct.unpack_from("<H", h, 10)[0] >= 23, tr('не Unicode PST'))
        (_, self.eof, self.amap_last, _, _, nbt_bid, nbt_ib, bbt_bid, bbt_ib, _, _, _) = \
            struct.unpack_from("<IQQQQQQQQBBH", h, 180)
        self.f.seek(0, 2)
        _chk(self.f.tell() == self.eof, tr('размер файла не совпадает с ibFileEof'))
        _chk((self.eof - FIRST_AMAP) % REGION == 0, tr('конец файла не на границе AMap'))
        self.rgnid = struct.unpack_from("<32I", h, 44)
        self.bbt, self.nbt = {}, {}
        self.pages = []
        self._walk(bbt_ib, bbt_bid, 0x80, self.bbt)
        self._walk(nbt_ib, nbt_bid, 0x81, self.nbt)
        self._check_amaps()

    def _page(self, ib, bid, ptype):
        self.pages.append(ib)
        self.f.seek(ib)
        p = self.f.read(512)
        t, t2, sig, crc, pbid = struct.unpack_from("<BBHIQ", p, 496)
        _chk(t == ptype and t2 == ptype, tr('тип страницы @{ib:#x}').format(ib=ib))
        _chk(crc == pst_crc(p[:496]), tr('CRC страницы @{ib:#x}').format(ib=ib))
        _chk(pbid == bid, tr('BID страницы @{ib:#x}').format(ib=ib))
        _chk(sig == _sig(ib, bid), tr('подпись страницы @{ib:#x}').format(ib=ib))
        return p

    def _walk(self, ib, bid, ptype, out, lo=None):
        p = self._page(ib, bid, ptype)
        cent, cmax, cbent, level = p[488], p[489], p[490], p[491]
        prev = None
        for i in range(cent):
            e = p[i * cbent:(i + 1) * cbent]
            key = struct.unpack_from("<Q", e)[0]
            _chk(prev is None or key > prev, tr('ключи B-дерева не по возрастанию'))
            prev = key
            if level:
                _, cbid, cib = struct.unpack("<QQQ", e)
                self._walk(cib, cbid, ptype, out)
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
            _chk(p[496] == 0x84, tr('нет AMap @{start:#x}').format(start=start))
            _chk(struct.unpack_from("<I", p, 500)[0] == pst_crc(p[:496]), f"CRC AMap @{start:#x}")
            actual[k * 496:(k + 1) * 496] = p[:496]
            for pg in region_pages(k):
                base = k * REGION // 64 + pg * 8
                mark_bits(expect, base, base + 8)
                if pg:
                    ptype, name = names[pg]
                    self.f.seek(start + pg * 512)
                    q = self.f.read(512)
                    _chk(q[496] == ptype and q[497] == ptype, tr('нет {name} @{0:#x}').format(start + pg * 512, name=name))
                    _chk(struct.unpack_from("<I", q, 500)[0] == pst_crc(q[:496]),
                         f"CRC {name} @{start + pg * 512:#x}")
        for ib, cb in self.bbt.values():
            a = (ib - FIRST_AMAP) // 64
            mark_bits(expect, a, a + (cb + 16 + 63) // 64)
        for ib in self.pages:
            a = (ib - FIRST_AMAP) // 64
            mark_bits(expect, a, a + 8)
        for k in range(nreg):
            e = int.from_bytes(expect[k * 496:(k + 1) * 496], "big")
            a = int.from_bytes(actual[k * 496:(k + 1) * 496], "big")
            _chk(e & ~a == 0, tr('AMap @{0:#x} не отмечает занятые данные').format(FIRST_AMAP + k * REGION))

    # ---------- блоки и узлы ----------
    def block(self, bid):
        _chk(bid in self.bbt, tr('блок {bid:#x} отсутствует в BBT').format(bid=bid))
        ib, cb = self.bbt[bid]
        total = (cb + 16 + 63) // 64 * 64
        self.f.seek(ib)
        raw = self.f.read(total)
        tcb, sig, crc, tbid = struct.unpack_from("<HHIQ", raw, total - 16)
        _chk(tcb == cb and tbid == bid, tr('трейлер блока {bid:#x}').format(bid=bid))
        _chk(sig == _sig(ib, bid), tr('подпись блока {bid:#x}').format(bid=bid))
        _chk(crc == pst_crc(raw[:cb]), tr('CRC блока {bid:#x}').format(bid=bid))
        return raw[:cb]

    def chunks(self, bid):
        if bid == 0:
            return []
        data = self.block(bid)
        if not bid & 2:
            return [data]
        btype, level, cent, total = struct.unpack_from("<BBHI", data)
        _chk(btype == 1, tr('ожидался XBLOCK'))
        out = []
        for i in range(cent):
            sub = self.chunks(struct.unpack_from("<Q", data, 8 + 8 * i)[0])
            if i < cent - 1:
                _chk(sum(len(c) for c in sub) == (8176 if level == 1 else 8176 * 1021),
                     tr('неполный промежуточный блок в дереве {bid:#x} (Outlook это отвергает)').format(bid=bid))
            out += sub
        _chk(sum(len(c) for c in out) == total, tr('lcbTotal не совпадает'))
        return out

    def subnodes(self, bid):
        res = {}
        if bid == 0:
            return res
        data = self.block(bid)
        btype, level, cent = struct.unpack_from("<BBH", data)
        _chk(btype == 2, tr('ожидался SLBLOCK/SIBLOCK'))
        for i in range(cent):
            if level == 0:
                n, d, s = struct.unpack_from("<QQQ", data, 8 + 24 * i)
                res[n] = (d, s)
            else:
                n, b = struct.unpack_from("<QQ", data, 8 + 16 * i)
                res.update(self.subnodes(b))
        return res

    # ---------- LTP ----------
    class _HN:
        def __init__(self, reader, chunks, subs):
            self.r, self.pages, self.subs = reader, chunks, subs
            _chk(chunks and chunks[0][2] == 0xEC, tr('нет сигнатуры heap 0xEC'))
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
                _chk(hid in self.subs, tr('подузел {hid:#x} не найден').format(hid=hid))
                return b"".join(self.r.chunks(self.subs[hid][0]))
            idx, blk = (hid >> 5) & 0x7FF, hid >> 16
            m = self.maps[blk]
            _chk(1 <= idx < len(m), tr('HID {hid:#x} вне карты heap').format(hid=hid))
            return self.pages[blk][m[idx - 1]:m[idx]]

        def bth(self, hid):
            h = self.get(hid)
            t, ck, ce, lv, root = struct.unpack("<BBBBI", h)
            _chk(t == 0xB5, tr('нет сигнатуры BTH'))
            out = []

            def walk(hid_, lvl):
                d = self.get(hid_)
                step = ck + (ce if lvl == 0 else 4)
                _chk(len(d) % step == 0, tr('размер узла BTH'))
                for i in range(0, len(d), step):
                    k = int.from_bytes(d[i:i + ck], "little")
                    if lvl:
                        walk(struct.unpack_from("<I", d, i + ck)[0], lvl - 1)
                    else:
                        out.append((k, d[i + ck:i + step]))
            if root:
                walk(root, lv)
            keys = [k for k, _ in out]
            _chk(keys == sorted(keys), tr('BTH не отсортирован'))
            return out

    def _hn(self, bid_data, bid_sub):
        return self._HN(self, self.chunks(bid_data), self.subnodes(bid_sub))

    def pc(self, bid_data, bid_sub):
        hn = self._hn(bid_data, bid_sub)
        _chk(hn.client == 0xBC, tr('ожидался Property Context'))
        props = {}
        for pid, rec in hn.bth(hn.root):
            ptype, dw = struct.unpack("<HI", rec)
            tag = (pid << 16) | ptype
            if ptype in (0x0003, 0x0002, 0x000B):
                props[tag] = dw
            else:
                data = hn.get(dw)
                props[tag] = data.decode("utf-16-le") if ptype == 0x1F else data
        return props

    def tc(self, bid_data, bid_sub):
        hn = self._hn(bid_data, bid_sub)
        _chk(hn.client == 0x7C, tr('ожидался Table Context'))
        info = hn.get(hn.root)
        _, ncol, t4, t2, t1, tbm, hid_ri, hnid_rows, _ = struct.unpack_from("<BB4HIII", info)
        cols = [struct.unpack_from("<IHBB", info, 22 + 8 * i) for i in range(ncol)]
        for tag, ib, cb, bit in cols:
            if tag == 0x67F20003:
                _chk(ib == 0 and cb == 4 and bit == 0, tr('COLDESC для RowID должен быть ib=0, iBit=0'))
            if tag == 0x67F30003:
                _chk(ib == 4 and cb == 4 and bit == 1, tr('COLDESC для RowVer должен быть ib=4, iBit=1'))
        index = hn.bth(hid_ri)
        rows = []
        if hnid_rows:
            if hnid_rows & 0x1F:
                blocks = self.chunks(hn.subs[hnid_rows][0])
            else:
                blocks = [hn.get(hnid_rows)]
            per = 8176 // tbm
            for rid, ridx in index:
                n = struct.unpack("<I", ridx)[0]
                blk = blocks[n // per]
                row = blk[(n % per) * tbm:(n % per + 1) * tbm]
                _chk(len(row) == tbm, tr('строка таблицы обрезана'))
                _chk(struct.unpack_from("<I", row)[0] == rid, tr('RowID не совпадает с индексом'))
                vals = {}
                for tag, ib, cb, bit in cols:
                    if not row[t1 + bit // 8] & (0x80 >> (bit % 8)):
                        continue
                    cell = row[ib:ib + cb]
                    if (tag & 0xFFFF) in (0x1F, 0x102):
                        d = hn.get(struct.unpack("<I", cell)[0])
                        vals[tag] = d.decode("utf-16-le") if tag & 0xFFFF == 0x1F else d
                    else:
                        vals[tag] = int.from_bytes(cell, "little")
                rows.append((rid, vals))
        return rows

    def node_pc(self, nid):
        d, s, _ = self.nbt[nid]
        return self.pc(d, s)

    def node_tc(self, nid):
        d, s, _ = self.nbt[nid]
        return self.tc(d, s)


def verify(path) -> dict:
    """Полная проверка файла. Возвращает сводку: {папка: число писем}."""
    r = PstReader(path)
    for nid in r.nbt:
        _chk((nid >> 5) < r.rgnid[nid & 0x1F], tr('счётчик NID в заголовке меньше узла {nid:#x}').format(nid=nid))
    store = r.node_pc(0x21)
    _chk(0x67FF0003 in store, tr('нет PR_PST_PASSWORD'))
    _chk(0x3001001F in r.node_pc(0x122), tr('у корневой папки нет имени'))
    _chk(0x35E00102 in store, tr('нет IPM Subtree в хранилище'))
    summary = {"_store": store.get(0x3001001F, ""), "_password": bool(store.get(0x67FF0003))}

    def walk(nid, path_):
        for child, vals in r.node_tc((nid & ~0x1F) | 0x0D):
            pc = r.node_pc(child)
            name = pc.get(0x3001001F, "?")
            rows = r.node_tc((child & ~0x1F) | 0x0E)
            _chk(len(rows) == pc.get(0x36020003, 0), tr('счётчик писем папки «{name}»').format(name=name))
            for mid, _ in rows:
                d, s, par = r.nbt[mid]
                _chk(par == child, tr('родитель письма'))
                m = r.pc(d, s)
                subs = r.subnodes(s)
                if 0x692 in subs:
                    r.tc(*subs[0x692])
                if 0x671 in subs:
                    for anid, _ in r.tc(*subs[0x671]):
                        a = r.pc(*subs[anid])
                        _chk(0x37010102 in a, tr('у вложения нет данных'))
                _chk(0x001A001F in m, tr('нет класса письма'))
            summary[path_ + name] = len(rows)
            walk(child, path_ + name + "/")
    walk(0x122, "")
    r.f.close()
    return summary
