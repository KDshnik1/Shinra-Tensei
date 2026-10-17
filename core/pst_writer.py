"""Создание Outlook .pst без Outlook и MAPI — по открытой спецификации [MS-PST].

Пишется Unicode PST (формат Outlook 2003+): слой NDB (блоки, B-деревья, карты
размещения), слой LTP (heap-on-node, BTH, Property/Table Context) и слой
сообщений (хранилище, папки, письма, получатели, вложения).
"""
import os
import re
import struct
from array import array
import uuid
import zlib
from .i18n import tr

# ---------------------------------------------------------------- константы NDB
BLOCK_MAX = 8176            # максимум данных в блоке
HEAP_MAX = 3580             # максимум одной аллокации в heap
PAGE = 512
REGION = 0x3E000            # область одной AMap (253 952 байт)
FIRST_AMAP = 0x4400
DLIST_IB = 0x4200
PT_BBT, PT_NBT, PT_FMAP, PT_PMAP, PT_AMAP, PT_FPMAP, PT_DL = 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86

# Служебные страницы в начале каждой области AMap (как в Microsoft outlook-pst-rs):
#   +0     AMap  — в каждой области
#   +512   PMap  — в каждой 8-й области
#   +1024  FMap  — в области 128, затем каждые 496 областей (первые 128 — в заголовке)
#   +1536  FPMap — в области 8192 (~2 ГБ), затем каждые 31 744 области (~8 ГБ)
FMAP_FIRST, FMAP_EVERY = 128, 496
FPMAP_FIRST, FPMAP_EVERY = 128 * 64, 496 * 64


def region_pages(k: int) -> list:
    """Номера служебных страниц (0..3) в области AMap № k."""
    pages = [0]
    if k % 8 == 0:
        pages.append(1)
        if k >= FMAP_FIRST and (k - FMAP_FIRST) % FMAP_EVERY == 0:
            pages.append(2)
        if k >= FPMAP_FIRST and (k - FPMAP_FIRST) % FPMAP_EVERY == 0:
            pages.append(3)
    return pages


def mark_bits(bits: bytearray, a: int, b: int):
    """Установить биты [a, b) в карте (старший бит байта — первый)."""
    while a < b and a & 7:
        bits[a >> 3] |= 0x80 >> (a & 7)
        a += 1
    while b > a and b & 7:
        b -= 1
        bits[b >> 3] |= 0x80 >> (b & 7)
    if a < b:
        bits[a >> 3:b >> 3] = b"\xff" * ((b - a) >> 3)


_ZERO_RUNS = re.compile(rb"\x00+")


def longest_free_run(amap: bytes) -> int:
    """Самая длинная серия свободных 64-байтовых ячеек (до 255) — значение для FMap."""
    best = max((len(m) for m in _ZERO_RUNS.findall(amap)), default=0) * 8
    if best >= 255:
        return 255
    bitstr = format(int.from_bytes(amap, "big"), f"0{len(amap) * 8}b")
    return min(255, max((len(r) for r in bitstr.split("1")), default=0))

NID_MESSAGE_STORE = 0x21
NID_NAME_TO_ID_MAP = 0x61
NID_ROOT_FOLDER = 0x122
NID_IPM_SUBTREE = 0x8022
NID_SEARCH_ROOT = 0x8042
NID_DELETED_ITEMS = 0x8082
NID_RECIPIENT_TABLE = 0x692
NID_ATTACHMENT_TABLE = 0x671

# типы свойств
PT_I2, PT_I4, PT_BOOL, PT_I8, PT_TIME = 0x0002, 0x0003, 0x000B, 0x0014, 0x0040
PT_STR, PT_BIN = 0x001F, 0x0102

# шаблоны колонок таблиц (как в пустом PST, созданном Outlook)
HIERARCHY_COLS = [0x3001001F, 0x36020003, 0x36030003, 0x360A000B, 0x0E300102, 0x0E330014,
                  0x0E340102, 0x0E380003, 0x3613001F, 0x66350003, 0x66360003]
CONTENTS_COLS = [0x0E170003, 0x001A001F, 0x0E070003, 0x00170003, 0x0042001F, 0x0037001F,
                 0x0E060040, 0x00390040, 0x0E080003, 0x0E04001F, 0x0E03001F, 0x0057000B,
                 0x0058000B, 0x00360003, 0x10970003, 0x0070001F, 0x00710102, 0x30130102,
                 0x65C60003, 0x30080040, 0x0E300102, 0x0E330014, 0x0E340102, 0x0E3D0102,
                 0x0E3C0102, 0x0E380003]
FAI_COLS = [0x001A001F, 0x3001001F, 0x70030003, 0x70040102, 0x70050102, 0x7006001F,
            0x70070003, 0x6800001F, 0x6803000B, 0x68051003, 0x682F001F]
SEARCH_CONTENTS_COLS = [0x67F10003, 0x0E05001F, 0x0E170003, 0x001A001F, 0x0E070003, 0x00170003,
                        0x0042001F, 0x0037001F, 0x0E060040, 0x0E080003, 0x0E04001F, 0x0E03001F,
                        0x0057000B, 0x0058000B, 0x00360003, 0x0E2A000B, 0x30080040]
RECIPIENT_COLS = [0x0E0F000B, 0x3002001F, 0x3003001F, 0x0FFF0102, 0x3001001F, 0x0C150003,
                  0x300B0102, 0x0FF90102, 0x0FFE0003, 0x39000003, 0x3A40000B, 0x39FF001F]
ATTACHMENT_COLS = [0x370B0003, 0x0E200003, 0x37050003, 0x3704001F]


def pst_crc(data: bytes) -> int:
    """CRC-32 по [MS-PST] 5.3 (без начальной/конечной инверсии)."""
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def _sig(ib: int, bid: int) -> int:
    x = (ib ^ bid) & 0xFFFFFFFFFFFFFFFF
    return ((x >> 16) ^ x) & 0xFFFF


def _align(x: int, a: int) -> int:
    return (x + a - 1) // a * a


def filetime(ts: float | None) -> int:
    if ts is None:
        return 0
    return int(ts * 10_000_000) + 116444736000000000


def _split(data: bytes, size=BLOCK_MAX) -> list:
    return [data[i:i + size] for i in range(0, len(data), size)] or [b""]


class Node:
    """Описание узла до записи: данные (список блоков) + подузлы {nid: Node}."""
    __slots__ = ("chunks", "subs")

    def __init__(self, chunks, subs=None):
        self.chunks = chunks
        self.subs = subs or {}


# ================================================================ LTP: heap / BTH / PC / TC

def _fill_level(free: int) -> int:
    for lvl, lim in enumerate((3584, 2560, 2048, 1792, 1536, 1280, 1024, 768, 512, 256, 128,
                               64, 32, 16, 8)):
        if free >= lim:
            return lvl
    return 0xF


class Heap:
    """Heap-on-node (HN)."""

    def __init__(self, client_sig: int):
        self.client = client_sig
        self.pages = [[]]
        self.subs = {}
        self._sub_index = 0x100

    @staticmethod
    def _hdr(i):
        if i == 0:
            return 12
        return 66 if i >= 8 and (i - 8) % 128 == 0 else 2

    def alloc(self, data: bytes) -> int:
        if len(data) > HEAP_MAX:          # не assert: сборка exe идёт с optimize=2
            raise ValueError(f"heap item too large: {len(data)}")
        i = len(self.pages) - 1
        page = self.pages[i]
        used = self._hdr(i) + sum(len(a) for a in page) + len(data) + 1 + 4 + 2 * (len(page) + 2)
        if used > BLOCK_MAX or len(page) >= 2000:
            self.pages.append([])
            i += 1
            page = self.pages[i]
        page.append(data)
        return (len(page) << 5) | (i << 16)

    def value(self, data: bytes) -> int:
        """Значение переменной длины: в heap или в подузел, если большое."""
        if not data:
            return 0
        if len(data) <= HEAP_MAX:
            return self.alloc(data)
        nid = (self._sub_index << 5) | 0x1F
        self._sub_index += 1
        self.subs[nid] = Node(_split(data))
        return nid

    def build(self, user_root: int) -> list:
        bodies, maps = [], []
        for i, allocs in enumerate(self.pages):
            off = self._hdr(i)
            offs = []
            body = b""
            for a in allocs:
                offs.append(off + len(body))
                body += a
            end = off + len(body)
            pad = end & 1
            offs.append(end)
            pmap = struct.pack("<HH", len(allocs), 0) + b"".join(struct.pack("<H", o) for o in offs)
            bodies.append((off, body + b"\0" * pad, end + pad, pmap))
        levels = [_fill_level(BLOCK_MAX - (ib + len(pm))) for (_, _, ib, pm) in bodies]

        def pack_levels(lv):
            out = bytearray((len(lv) + 1) // 2)
            for j, v in enumerate(lv):
                out[j // 2] |= (v & 0xF) << (4 * (j % 2))
            return bytes(out)

        pages = []
        for i, (off, body, ib, pmap) in enumerate(bodies):
            if i == 0:
                hdr = struct.pack("<HBBI", ib, 0xEC, self.client, user_root) + \
                    pack_levels(levels[:8]).ljust(4, b"\0")
            elif off == 66:
                hdr = struct.pack("<H", ib) + pack_levels(levels[i:i + 128]).ljust(64, b"\0")
            else:
                hdr = struct.pack("<H", ib)
            pages.append(hdr + body + pmap)
        return pages


def _bth(heap: Heap, cb_key: int, cb_ent: int, records: list) -> int:
    """BTree-on-heap. records: [(key_int, data_bytes)]. Возвращает HID заголовка."""
    records = sorted(records, key=lambda r: r[0])
    levels, root = 0, 0
    if records:
        rec = cb_key + cb_ent
        per = HEAP_MAX // rec
        nodes = []
        for i in range(0, len(records), per):
            part = records[i:i + per]
            data = b"".join(k.to_bytes(cb_key, "little") + d for k, d in part)
            nodes.append((part[0][0], heap.alloc(data)))
        while len(nodes) > 1:
            per = HEAP_MAX // (cb_key + 4)
            nxt = []
            for i in range(0, len(nodes), per):
                part = nodes[i:i + per]
                data = b"".join(k.to_bytes(cb_key, "little") + struct.pack("<I", h) for k, h in part)
                nxt.append((part[0][0], heap.alloc(data)))
            nodes = nxt
            levels += 1
        root = nodes[0][1]
    return heap.alloc(struct.pack("<BBBBI", 0xB5, cb_key, cb_ent, levels, root))


def _encode(ptype: int, value) -> bytes:
    if ptype == PT_STR:
        return str(value).encode("utf-16-le", "replace")   # одиночные суррогаты -> «?»
    if ptype == PT_BIN:
        return bytes(value)
    if ptype in (PT_I8, PT_TIME):
        return struct.pack("<q", int(value))
    if ptype == PT_I4:
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if ptype == PT_I2:
        return struct.pack("<H", int(value) & 0xFFFF)
    if ptype == PT_BOOL:
        return b"\x01" if value else b"\x00"
    raise ValueError(tr('тип свойства 0x{ptype:04X} не поддерживается').format(ptype=ptype))


def build_pc(props: dict) -> Node:
    """Property Context. props: {tag: value}."""
    heap = Heap(0xBC)
    recs = []
    for tag, value in props.items():
        if value is None:
            continue
        pid, ptype = tag >> 16, tag & 0xFFFF
        if ptype in (PT_I4, PT_I2, PT_BOOL):
            dw = int.from_bytes(_encode(ptype, value).ljust(4, b"\0"), "little")
        else:
            data = _encode(ptype, value)
            dw = heap.value(data) if data else heap.alloc(b"")
        recs.append((pid, struct.pack("<HI", ptype, dw)))
    root = _bth(heap, 2, 6, recs)
    return Node(heap.build(root), heap.subs)


def _cell_size(ptype):
    if ptype in (PT_I8, PT_TIME):
        return 8
    if ptype == PT_I2:
        return 2
    if ptype == PT_BOOL:
        return 1
    return 4                                   # I4 и HNID для переменных типов


def build_tc(columns: list, rows: list) -> Node:
    """Table Context. rows: [(row_id, {tag: value})]."""
    cols = sorted(set(columns) | {0x67F20003, 0x67F30003})
    rest = [c for c in cols if c not in (0x67F20003, 0x67F30003)]
    layout = {0x67F20003: (0, 4), 0x67F30003: (4, 4)}
    ib = 8
    for want in (8, 4):
        for c in rest:
            if _cell_size(c & 0xFFFF) == want:
                layout[c] = (ib, want)
                ib += want
    tci_4b = ib
    for c in rest:
        if _cell_size(c & 0xFFFF) == 2:
            layout[c] = (ib, 2)
            ib += 2
    tci_2b = ib
    for c in rest:
        if _cell_size(c & 0xFFFF) == 1:
            layout[c] = (ib, 1)
            ib += 1
    tci_1b = ib
    order = [0x67F20003, 0x67F30003] + rest
    ibit = {c: i for i, c in enumerate(order)}
    row_size = tci_1b + (len(cols) + 7) // 8

    heap = Heap(0x7C)
    matrix, index = [], []
    for n, (row_id, values) in enumerate(rows):
        row = bytearray(row_size)
        vals = dict(values)
        vals[0x67F20003] = row_id
        vals[0x67F30003] = n + 1
        for c in cols:
            v = vals.get(c)
            if v is None:
                continue
            off, size = layout[c]
            ptype = c & 0xFFFF
            if ptype in (PT_STR, PT_BIN):
                data = _encode(ptype, v)
                if not data:
                    continue
                cell = struct.pack("<I", heap.value(data))
            else:
                cell = _encode(ptype, v).ljust(size, b"\0")[:size]
            row[off:off + size] = cell
            b = ibit[c]
            row[tci_1b + b // 8] |= 0x80 >> (b % 8)
        matrix.append(bytes(row))
        index.append((row_id, struct.pack("<I", n)))
    hid_index = _bth(heap, 4, 4, index)
    hnid_rows = 0
    if matrix:
        blob = b"".join(matrix)
        if len(blob) <= HEAP_MAX:
            hnid_rows = heap.alloc(blob)
        else:
            per = BLOCK_MAX // row_size
            chunks = [b"".join(matrix[i:i + per]) for i in range(0, len(matrix), per)]
            hnid_rows = (heap._sub_index << 5) | 0x1F
            heap._sub_index += 1
            heap.subs[hnid_rows] = Node(chunks)
    info = struct.pack("<BB4HIII", 0x7C, len(cols), tci_4b, tci_2b, tci_1b, row_size,
                       hid_index, hnid_rows, 0)
    for c in cols:
        off, size = layout[c]
        info += struct.pack("<IHBB", c, off, size, ibit[c])
    return Node(heap.build(heap.alloc(info)), heap.subs)


# ================================================================ NDB: файл, блоки, деревья

class _Allocator:
    def __init__(self):
        self.pos = FIRST_AMAP + 2 * PAGE          # после первой AMap и PMap
        self.bits = bytearray()                   # 1 бит = 64 байта, от FIRST_AMAP

    @staticmethod
    def reserved_pages(k):
        """Сколько страниц в начале области занято служебными (с учётом пропусков)."""
        return region_pages(k)[-1] + 1

    def mark(self, ib, size):
        a = (ib - FIRST_AMAP) // 64
        b = (ib + size - FIRST_AMAP + 63) // 64
        need = b // 8 + 1
        if len(self.bits) < need:
            self.bits.extend(b"\0" * (need - len(self.bits) + (1 << 20)))
        mark_bits(self.bits, a, b)

    def alloc(self, size, align):
        p = _align(self.pos, align)
        while True:
            k = (p - FIRST_AMAP) // REGION
            start = FIRST_AMAP + k * REGION
            res_end = start + self.reserved_pages(k) * PAGE
            if p < res_end:
                p = _align(res_end, align)
                continue
            if p + size > start + REGION:
                p = start + REGION
                continue
            break
        self.pos = p + size
        self.mark(p, size)
        return p


class PstFile:
    def __init__(self, path, display_name: str, password: str = ""):
        self.path = str(path)
        self.f = open(self.path, "w+b")
        self.al = _Allocator()
        # блоки пишутся с возрастающими BID, поэтому BBT уже отсортирована;
        # компактные массивы вместо кортежей — для файлов до 10 ГБ
        self.bbt_bid, self.bbt_ib, self.bbt_cb = array("Q"), array("Q"), array("H")
        self.nbt = []                      # (nid, bid_data, bid_sub, nid_parent)
        self._bid = 0
        self._pbid = 0
        self.uid = uuid.uuid4().bytes
        self.display_name = display_name
        self.password = password
        self._folder_idx = 0x405
        self._msg_idx = 0x10000
        self._hw = {}
        self.folders = {}
        self._add_folder_rec(NID_ROOT_FOLDER, NID_ROOT_FOLDER, "", None)
        self._add_folder_rec(NID_IPM_SUBTREE, NID_ROOT_FOLDER, "Top of Personal Folders", None)
        self._add_folder_rec(NID_SEARCH_ROOT, NID_ROOT_FOLDER, "Search Root", None)
        self._add_folder_rec(NID_DELETED_ITEMS, NID_IPM_SUBTREE, tr('Удаленные'), "IPF.Note")

    # ---------- размер ----------
    def size(self) -> int:
        return self.al.pos

    def close_overhead(self) -> int:
        """Оценка (с запасом) того, сколько байт добавит close(): таблицы папок,
        B-деревья узлов и блоков, служебные страницы и выравнивание конца файла."""
        rows = sum(f["count"] for f in self.folders.values())
        blocks = len(self.bbt_bid) + rows // 8 + 2000
        nodes = len(self.nbt) + 4 * len(self.folders) + 64
        return (blocks * 28 + nodes * 37        # листья и узлы BBT/NBT (+ верхние уровни)
                + rows * 900                    # строки таблиц содержимого папок
                + 2 * REGION + 1024 * 1024)     # последняя область AMap и мелочи

    # ---------- блоки ----------
    def _write_block(self, data: bytes, internal=False) -> int:
        self._bid += 4
        bid = self._bid | (2 if internal else 0)
        cb = len(data)
        total = _align(cb + 16, 64)
        ib = self.al.alloc(total, 64)
        buf = data + b"\0" * (total - 16 - cb) + struct.pack("<HHIQ", cb, _sig(ib, bid),
                                                             pst_crc(data), bid)
        self.f.seek(ib)
        self.f.write(buf)
        self.bbt_bid.append(bid)
        self.bbt_ib.append(ib)
        self.bbt_cb.append(cb)
        return bid

    def _write_tree(self, chunks: list) -> int:
        if len(chunks) == 1:
            return self._write_block(chunks[0])
        # Outlook требует: все блоки дерева, кроме последнего, заполнены полностью
        chunks = [c.ljust(BLOCK_MAX, b"\0") for c in chunks[:-1]] + [chunks[-1]]
        bids = [(self._write_block(c), len(c)) for c in chunks]
        groups = [bids[i:i + 1021] for i in range(0, len(bids), 1021)]
        xs = []
        for g in groups:
            total = sum(s for _, s in g)
            data = struct.pack("<BBHI", 1, 1, len(g), total) + b"".join(
                struct.pack("<Q", b) for b, _ in g)
            xs.append((self._write_block(data, True), total))
        if len(xs) == 1:
            return xs[0][0]
        if len(xs) > 1021:
            raise ValueError(tr('слишком большой объект для PST'))
        data = struct.pack("<BBHI", 1, 2, len(xs), sum(s for _, s in xs)) + b"".join(
            struct.pack("<Q", b) for b, _ in xs)
        return self._write_block(data, True)

    def _write_subnodes(self, entries: list) -> int:
        if not entries:
            return 0
        entries.sort()
        if len(entries) <= 340:
            data = struct.pack("<BBHI", 2, 0, len(entries), 0) + b"".join(
                struct.pack("<QQQ", n, d, s) for n, d, s in entries)
            return self._write_block(data, True)
        sls = []
        for i in range(0, len(entries), 340):
            part = entries[i:i + 340]
            sls.append((part[0][0], self._write_subnodes(part)))
        data = struct.pack("<BBHI", 2, 1, len(sls), 0) + b"".join(
            struct.pack("<QQ", n, b) for n, b in sls)
        return self._write_block(data, True)

    def _note_nid(self, nid):
        t, idx = nid & 0x1F, nid >> 5
        if idx + 1 > self._hw.get(t, 0):
            self._hw[t] = idx + 1

    def _write_node(self, node: Node):
        for nid in node.subs:
            self._note_nid(nid)
        subs = [(nid, *self._write_node(child)) for nid, child in node.subs.items()]
        return self._write_tree(node.chunks), self._write_subnodes(subs)

    def _put(self, nid, node: Node, parent=0):
        d, s = self._write_node(node)
        self._note_nid(nid)
        self.nbt.append((nid, d, s, parent))

    # ---------- папки ----------
    def _add_folder_rec(self, nid, parent, name, container_class):
        self.folders[nid] = {"nid": nid, "parent": parent, "name": name, "cls": container_class,
                             "children": [], "rows": [], "unread": 0, "count": 0}
        if parent != nid and parent in self.folders:
            self.folders[parent]["children"].append(nid)

    def add_folder(self, parent_nid: int, name: str) -> int:
        nid = (self._folder_idx << 5) | 0x02
        self._folder_idx += 1
        self._add_folder_rec(nid, parent_nid, name, "IPF.Note")
        return nid

    def entry_id(self, nid: int) -> bytes:
        return b"\0\0\0\0" + self.uid + struct.pack("<I", nid)

    # ---------- письма ----------
    def add_message(self, folder_nid: int, msg: dict):
        """msg: props (dict tag->value), recipients [dict], attachments [dict], read (bool)."""
        nid = (self._msg_idx << 5) | 0x04
        self._msg_idx += 1
        props = dict(msg["props"])
        atts = msg.get("attachments") or []
        flags = (0x01 if msg.get("read") else 0) | (0x10 if atts else 0)
        props[0x0E070003] = flags
        props[0x0E1B000B] = bool(atts)
        node = build_pc(props)

        rec_rows = [(i, r) for i, r in enumerate(msg.get("recipients") or [])]
        node.subs[NID_RECIPIENT_TABLE] = build_tc(RECIPIENT_COLS, rec_rows)
        if atts:
            att_rows = []
            for i, a in enumerate(atts):
                anid = ((0x400 + i) << 5) | 0x05
                node.subs[anid] = build_pc(a["props"])
                att_rows.append((anid, a["row"]))
            node.subs[NID_ATTACHMENT_TABLE] = build_tc(ATTACHMENT_COLS, att_rows)
        self._put(nid, node, folder_nid)

        f = self.folders[folder_nid]
        row = {t: props.get(t) for t in CONTENTS_COLS if t in props}
        row[0x0E070003] = flags
        f["rows"].append((nid, row))
        f["count"] += 1
        if not msg.get("read"):
            f["unread"] += 1

    # ---------- завершение ----------
    def _folder_props(self, f) -> dict:
        p = {0x36020003: f["count"], 0x36030003: f["unread"],
             0x360A000B: bool(f["children"])}
        p[0x3001001F] = f["name"]
        if f["cls"]:
            p[0x3613001F] = f["cls"]
        return p

    def _write_folders(self):
        for nid, f in self.folders.items():
            props = self._folder_props(f)
            self._put(nid, build_pc(props), f["parent"])
            hier_rows = [(c, self._folder_props(self.folders[c])) for c in f["children"]]
            base = nid & ~0x1F
            self._put(base | 0x0D, build_tc(HIERARCHY_COLS, hier_rows))
            self._put(base | 0x0E, build_tc(CONTENTS_COLS, f["rows"]))
            self._put(base | 0x0F, build_tc(FAI_COLS, []))
            f["rows"] = []

    def _write_special(self):
        store = {0x0FF90102: self.uid, 0x3001001F: self.display_name,
                 0x35E00102: self.entry_id(NID_IPM_SUBTREE),
                 0x35E30102: self.entry_id(NID_DELETED_ITEMS),
                 0x35E70102: self.entry_id(NID_SEARCH_ROOT),
                 0x35DF0003: 0x89}
        store[0x67FF0003] = pst_crc(self.password.encode("ascii")) if self.password else 0
        self._put(NID_MESSAGE_STORE, build_pc(store))
        self._put(NID_NAME_TO_ID_MAP, build_pc({0x00010003: 251, 0x00020102: b"",
                                                0x00030102: b"", 0x00040102: b""}))
        self._put(0xA1, build_pc({}))
        self._put(0xC1, build_pc({}))
        for nid in (0x1E1, 0x201, 0x241, 0x261, 0x281, 0x2A1, 0x2E1, 0x301, 0x321):
            self._put(nid, Node([b""]))
        self._put(0x60D, build_tc(HIERARCHY_COLS, []))
        self._put(0x60E, build_tc(CONTENTS_COLS, []))
        self._put(0x60F, build_tc(FAI_COLS, []))
        self._put(0x610, build_tc(SEARCH_CONTENTS_COLS, []))
        self._put(NID_ATTACHMENT_TABLE, build_tc(ATTACHMENT_COLS, []))
        self._put(NID_RECIPIENT_TABLE, build_tc(RECIPIENT_COLS, []))

    def _write_page(self, body: bytes, ptype: int) -> tuple:
        self._pbid += 1
        bid = self._pbid
        ib = self.al.alloc(PAGE, PAGE)
        self.f.seek(ib)
        self.f.write(body + struct.pack("<BBHIQ", ptype, ptype, _sig(ib, bid), pst_crc(body), bid))
        return bid, ib

    def _write_btree(self, count: int, key_at, entry_at, ptype: int) -> tuple:
        """B-дерево из count записей; key_at(i)/entry_at(i) дают ключ и байты записи i."""
        cbent = {PT_NBT: 32, PT_BBT: 24}[ptype]
        maxent = 488 // cbent
        pages = []
        for i in range(0, count, maxent):
            n = min(maxent, count - i)
            raw = b"".join(entry_at(j) for j in range(i, i + n))
            body = raw.ljust(488, b"\0") + struct.pack("<BBBBI", n, maxent, cbent, 0, 0)
            bid, ib = self._write_page(body, ptype)
            pages.append((key_at(i), bid, ib))
        level = 1
        while len(pages) > 1:
            maxent = 488 // 24
            nxt = []
            for i in range(0, len(pages), maxent):
                part = pages[i:i + maxent]
                raw = b"".join(struct.pack("<QQQ", k, b, ib) for k, b, ib in part)
                body = raw.ljust(488, b"\0") + struct.pack("<BBBBI", len(part), maxent, 24, level, 0)
                bid, ib = self._write_page(body, ptype)
                nxt.append((part[0][0], bid, ib))
            pages = nxt
            level += 1
        return pages[0][1], pages[0][2]

    def _write_maps(self):
        last_k = (self.al.pos - 1 - FIRST_AMAP) // REGION
        for k in range(last_k + 1):
            start = FIRST_AMAP + k * REGION
            for pg in region_pages(k):
                self.al.mark(start + pg * PAGE, PAGE)
        eof = FIRST_AMAP + (last_k + 1) * REGION
        bits = self.al.bits
        if len(bits) < (last_k + 1) * 496:
            bits.extend(b"\0" * ((last_k + 1) * 496 - len(bits)))
        free_total = 0
        longest = []

        def trailer_page(ib, body, ptype):
            self.f.seek(ib)
            self.f.write(body + struct.pack("<BBHIQ", ptype, ptype, 0, pst_crc(body), ib))

        for k in range(last_k + 1):
            amap = bytes(bits[k * 496:(k + 1) * 496])
            free_total += (496 * 8 - int.from_bytes(amap, "big").bit_count()) * 64
            longest.append(longest_free_run(amap))
            start = FIRST_AMAP + k * REGION
            trailer_page(start, amap, PT_AMAP)
            pages = region_pages(k)
            if 1 in pages:
                trailer_page(start + PAGE, b"\xff" * 496, PT_PMAP)
            if 3 in pages:
                trailer_page(start + 3 * PAGE, b"\xff" * 496, PT_FPMAP)
        for k in range(last_k + 1):
            if 2 in region_pages(k):
                fm = bytes(longest[k:k + 496]).ljust(496, b"\0")
                trailer_page(FIRST_AMAP + k * REGION + 2 * PAGE, fm, PT_FMAP)
        dl = struct.pack("<BBHI", 0x01, 0, 0, 0).ljust(496, b"\0")
        trailer_page(DLIST_IB, dl, PT_DL)
        self.f.seek(eof - 1)
        self.f.write(b"\0")
        return eof, FIRST_AMAP + last_k * REGION, free_total

    def close(self):
        self._write_folders()
        self._write_special()
        nbt = sorted(self.nbt)
        self.nbt = []
        nbt_bid, nbt_ib = self._write_btree(
            len(nbt), lambda i: nbt[i][0],
            lambda i: struct.pack("<QQQII", *nbt[i], 0), PT_NBT)
        bb, bi, bc = self.bbt_bid, self.bbt_ib, self.bbt_cb
        bbt_bid, bbt_ib = self._write_btree(
            len(bb), lambda i: bb[i],
            lambda i: struct.pack("<QQHHI", bb[i], bi[i], bc[i], 2, 0), PT_BBT)
        eof, amap_last, amap_free = self._write_maps()

        rgnid = [0x400] * 32
        rgnid[3] = 0x4000
        rgnid[4] = 0x10000
        rgnid[8] = 0x8000
        for t, nxt in self._hw.items():
            rgnid[t] = max(rgnid[t], nxt)
        root = struct.pack("<IQQQQQQQQBBH", 0, eof, amap_last, amap_free, 0,
                           nbt_bid, nbt_ib, bbt_bid, bbt_ib, 2, 0, 0)
        h = bytearray(564)
        h[0:4] = b"!BDN"
        struct.pack_into("<HHHBBII", h, 8, 0x4D53, 23, 19, 1, 1, 0, 0)
        struct.pack_into("<QQI", h, 24, 0, self._pbid + 1, 1)
        struct.pack_into("<32I", h, 44, *rgnid)
        h[180:252] = root
        h[256:512] = b"\xff" * 256
        h[512] = 0x80
        h[513] = 0                                   # NDB_CRYPT_NONE
        struct.pack_into("<Q", h, 516, self._bid + 4)
        struct.pack_into("<I", h, 4, pst_crc(bytes(h[8:8 + 471])))
        struct.pack_into("<I", h, 524, pst_crc(bytes(h[8:8 + 516])))
        self.f.seek(0)
        self.f.write(bytes(h))
        self.f.close()
        self._closed = True

    def abort(self):
        if getattr(self, "_closed", False):     # готовый файл не удаляем
            return
        try:
            self.f.close()
            os.remove(self.path)
        except OSError:
            pass


# ================================================================ нативное ядро (Rust)
# Если собран модуль shinra_core (папка native/), PstFile берётся из него: файл
# получается побайтно тем же, но письма разбираются и пишутся на Rust без GIL —
# несколько экспортов в разных потоках идут параллельно. Без модуля, или с
# переменной окружения SHINRA_PURE_PYTHON=1, работает Python-версия выше.
PyPstFile = PstFile
NATIVE = False
if not os.environ.get("SHINRA_PURE_PYTHON"):
    try:
        import shinra_core as _native
    except ImportError:
        _native = None
    if _native is not None:
        _native.set_translator(tr)
        PstFile = _native.PstFile
        NATIVE = True
