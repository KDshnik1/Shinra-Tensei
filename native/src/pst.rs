//! Запись Unicode PST по [MS-PST] — порт core/pst_writer.py.
//!
//! Файл получается побайтно таким же, как у Python-версии (при одинаковом
//! идентификаторе хранилища): порядок аллокаций, блоков и страниц сохранён.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufWriter, Seek, SeekFrom, Write};

// ---------------------------------------------------------------- константы NDB
pub const BLOCK_MAX: usize = 8176; // максимум данных в блоке
pub const HEAP_MAX: usize = 3580; // максимум одной аллокации в heap
const PAGE: u64 = 512;
const REGION: u64 = 0x3E000; // область одной AMap (253 952 байт)
const FIRST_AMAP: u64 = 0x4400;
const DLIST_IB: u64 = 0x4200;
const PT_BBT: u8 = 0x80;
const PT_NBT: u8 = 0x81;
const PT_FMAP: u8 = 0x82;
const PT_PMAP: u8 = 0x83;
const PT_AMAP: u8 = 0x84;
const PT_FPMAP: u8 = 0x85;
const PT_DL: u8 = 0x86;

const FMAP_FIRST: u64 = 128;
const FMAP_EVERY: u64 = 496;
const FPMAP_FIRST: u64 = 128 * 64;
const FPMAP_EVERY: u64 = 496 * 64;

pub const NID_MESSAGE_STORE: u32 = 0x21;
pub const NID_NAME_TO_ID_MAP: u32 = 0x61;
pub const NID_ROOT_FOLDER: u32 = 0x122;
pub const NID_IPM_SUBTREE: u32 = 0x8022;
pub const NID_SEARCH_ROOT: u32 = 0x8042;
pub const NID_DELETED_ITEMS: u32 = 0x8082;
pub const NID_RECIPIENT_TABLE: u32 = 0x692;
pub const NID_ATTACHMENT_TABLE: u32 = 0x671;

// типы свойств
pub const PT_I2: u16 = 0x0002;
pub const PT_I4: u16 = 0x0003;
pub const PT_BOOL: u16 = 0x000B;
pub const PT_I8: u16 = 0x0014;
pub const PT_TIME: u16 = 0x0040;
pub const PT_STR: u16 = 0x001F;
pub const PT_BIN: u16 = 0x0102;

// шаблоны колонок таблиц (как в пустом PST, созданном Outlook)
const HIERARCHY_COLS: &[u32] = &[
    0x3001001F, 0x36020003, 0x36030003, 0x360A000B, 0x0E300102, 0x0E330014, 0x0E340102, 0x0E380003,
    0x3613001F, 0x66350003, 0x66360003,
];
pub const CONTENTS_COLS: &[u32] = &[
    0x0E170003, 0x001A001F, 0x0E070003, 0x00170003, 0x0042001F, 0x0037001F, 0x0E060040, 0x00390040,
    0x0E080003, 0x0E04001F, 0x0E03001F, 0x0057000B, 0x0058000B, 0x00360003, 0x10970003, 0x0070001F,
    0x00710102, 0x30130102, 0x65C60003, 0x30080040, 0x0E300102, 0x0E330014, 0x0E340102, 0x0E3D0102,
    0x0E3C0102, 0x0E380003,
];
const FAI_COLS: &[u32] = &[
    0x001A001F, 0x3001001F, 0x70030003, 0x70040102, 0x70050102, 0x7006001F, 0x70070003, 0x6800001F,
    0x6803000B, 0x68051003, 0x682F001F,
];
const SEARCH_CONTENTS_COLS: &[u32] = &[
    0x67F10003, 0x0E05001F, 0x0E170003, 0x001A001F, 0x0E070003, 0x00170003, 0x0042001F, 0x0037001F,
    0x0E060040, 0x0E080003, 0x0E04001F, 0x0E03001F, 0x0057000B, 0x0058000B, 0x00360003, 0x0E2A000B,
    0x30080040,
];
const RECIPIENT_COLS: &[u32] = &[
    0x0E0F000B, 0x3002001F, 0x3003001F, 0x0FFF0102, 0x3001001F, 0x0C150003, 0x300B0102, 0x0FF90102,
    0x0FFE0003, 0x39000003, 0x3A40000B, 0x39FF001F,
];
const ATTACHMENT_COLS: &[u32] = &[0x370B0003, 0x0E200003, 0x37050003, 0x3704001F];

// ---------------------------------------------------------------- ошибки

#[derive(Debug)]
pub enum PstError {
    Io(io::Error),
    /// Неподдерживаемый тип свойства (шаблон для перевода + тип).
    Unsupported(u16),
    /// Объект больше, чем помещается в дерево блоков.
    TooLarge,
    /// Папки с таким NID нет.
    NoFolder(u32),
    /// Внутренняя ошибка (паника в Rust), шаблон сообщения для перевода.
    Internal(&'static str),
}

impl From<io::Error> for PstError {
    fn from(e: io::Error) -> Self {
        PstError::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, PstError>;

// ---------------------------------------------------------------- значения свойств

#[derive(Clone, Debug)]
pub enum Val {
    Int(i64),
    Bool(bool),
    Str(String),
    Bin(Vec<u8>),
}

impl Val {
    fn as_int(&self) -> i64 {
        match self {
            Val::Int(i) => *i,
            Val::Bool(b) => *b as i64,
            _ => 0,
        }
    }
    fn truthy(&self) -> bool {
        match self {
            Val::Int(i) => *i != 0,
            Val::Bool(b) => *b,
            Val::Str(s) => !s.is_empty(),
            Val::Bin(b) => !b.is_empty(),
        }
    }
}

/// Свойства в порядке добавления (как dict в Python); None — свойство пропускается.
pub type Props = Vec<(u32, Option<Val>)>;

pub fn props_get(p: &Props, tag: u32) -> Option<&Val> {
    p.iter()
        .find(|(t, _)| *t == tag)
        .and_then(|(_, v)| v.as_ref())
}

fn props_has(p: &Props, tag: u32) -> bool {
    p.iter().any(|(t, _)| *t == tag)
}

/// props[tag] = v: существующий ключ сохраняет своё место, новый — в конец.
pub fn props_set(p: &mut Props, tag: u32, v: Option<Val>) {
    if let Some(e) = p.iter_mut().find(|(t, _)| *t == tag) {
        e.1 = v;
    } else {
        p.push((tag, v));
    }
}

fn encode(ptype: u16, v: &Val) -> Result<Vec<u8>> {
    Ok(match ptype {
        PT_STR => match v {
            Val::Str(s) => s.encode_utf16().flat_map(|u| u.to_le_bytes()).collect(),
            Val::Int(i) => i
                .to_string()
                .encode_utf16()
                .flat_map(|u| u.to_le_bytes())
                .collect(),
            Val::Bool(b) => (if *b { "True" } else { "False" })
                .encode_utf16()
                .flat_map(|u| u.to_le_bytes())
                .collect(),
            Val::Bin(_) => return Err(PstError::Unsupported(ptype)),
        },
        PT_BIN => match v {
            Val::Bin(b) => b.clone(),
            _ => return Err(PstError::Unsupported(ptype)),
        },
        PT_I8 | PT_TIME => v.as_int().to_le_bytes().to_vec(),
        PT_I4 => ((v.as_int() & 0xFFFF_FFFF) as u32).to_le_bytes().to_vec(),
        PT_I2 => ((v.as_int() & 0xFFFF) as u16).to_le_bytes().to_vec(),
        PT_BOOL => vec![v.truthy() as u8],
        _ => return Err(PstError::Unsupported(ptype)),
    })
}

// ---------------------------------------------------------------- вспомогательное

pub fn pst_crc(data: &[u8]) -> u32 {
    // CRC-32 по [MS-PST] 5.3 (без начальной/конечной инверсии)
    let mut h = crc32fast::Hasher::new_with_initial(0xFFFF_FFFF);
    h.update(data);
    h.finalize() ^ 0xFFFF_FFFF
}

fn sig(ib: u64, bid: u64) -> u16 {
    let x = ib ^ bid;
    (((x >> 16) ^ x) & 0xFFFF) as u16
}

fn align(x: u64, a: u64) -> u64 {
    x.div_ceil(a) * a
}

/// Как filetime() в Python для float-времени: int(ts * 10_000_000) + смещение.
pub fn filetime_f(ts: f64) -> i64 {
    (ts * 10_000_000.0) as i64 + 116_444_736_000_000_000
}

/// Для целого времени — точная целочисленная арифметика, как у Python int.
pub fn filetime_i(ts: i64) -> i64 {
    ts * 10_000_000 + 116_444_736_000_000_000
}

fn split(data: &[u8], size: usize) -> Vec<Vec<u8>> {
    if data.is_empty() {
        return vec![Vec::new()];
    }
    data.chunks(size).map(|c| c.to_vec()).collect()
}

/// Номера служебных страниц (0..3) в области AMap № k.
fn region_pages(k: u64) -> Vec<u64> {
    let mut pages = vec![0];
    if k.is_multiple_of(8) {
        pages.push(1);
        if k >= FMAP_FIRST && (k - FMAP_FIRST).is_multiple_of(FMAP_EVERY) {
            pages.push(2);
        }
        if k >= FPMAP_FIRST && (k - FPMAP_FIRST).is_multiple_of(FPMAP_EVERY) {
            pages.push(3);
        }
    }
    pages
}

/// Установить биты [a, b) в карте (старший бит байта — первый).
fn mark_bits(bits: &mut [u8], mut a: usize, mut b: usize) {
    while a < b && a & 7 != 0 {
        bits[a >> 3] |= 0x80 >> (a & 7);
        a += 1;
    }
    while b > a && b & 7 != 0 {
        b -= 1;
        bits[b >> 3] |= 0x80 >> (b & 7);
    }
    if a < b {
        bits[a >> 3..b >> 3].fill(0xFF);
    }
}

/// Самая длинная серия свободных 64-байтовых ячеек (до 255) — значение для FMap.
fn longest_free_run(amap: &[u8]) -> u8 {
    let (mut best, mut cur) = (0usize, 0usize);
    for &byte in amap {
        if byte == 0 {
            cur += 8;
        } else {
            for bit in (0..8).rev() {
                if byte >> bit & 1 == 0 {
                    cur += 1;
                } else {
                    best = best.max(cur);
                    cur = 0;
                }
            }
        }
    }
    best.max(cur).min(255) as u8
}

// ================================================================ LTP: heap / BTH / PC / TC

/// Описание узла до записи: данные (список блоков) + подузлы в порядке добавления.
pub struct Node {
    chunks: Vec<Vec<u8>>,
    subs: Vec<(u32, Node)>,
}

impl Node {
    fn new(chunks: Vec<Vec<u8>>) -> Self {
        Node {
            chunks,
            subs: Vec::new(),
        }
    }
    fn set_sub(&mut self, nid: u32, node: Node) {
        if let Some(e) = self.subs.iter_mut().find(|(n, _)| *n == nid) {
            e.1 = node;
        } else {
            self.subs.push((nid, node));
        }
    }
}

fn fill_level(free: i64) -> u8 {
    const LIMS: [i64; 15] = [
        3584, 2560, 2048, 1792, 1536, 1280, 1024, 768, 512, 256, 128, 64, 32, 16, 8,
    ];
    for (lvl, lim) in LIMS.iter().enumerate() {
        if free >= *lim {
            return lvl as u8;
        }
    }
    0xF
}

/// Heap-on-node (HN).
struct Heap {
    client: u8,
    pages: Vec<Vec<Vec<u8>>>,
    used: Vec<usize>, // сумма длин аллокаций страницы
    subs: Vec<(u32, Node)>,
    sub_index: u32,
}

impl Heap {
    fn new(client: u8) -> Self {
        Heap {
            client,
            pages: vec![Vec::new()],
            used: vec![0],
            subs: Vec::new(),
            sub_index: 0x100,
        }
    }

    fn hdr(i: usize) -> usize {
        if i == 0 {
            12
        } else if i >= 8 && (i - 8).is_multiple_of(128) {
            66
        } else {
            2
        }
    }

    fn alloc(&mut self, data: Vec<u8>) -> u32 {
        debug_assert!(data.len() <= HEAP_MAX);
        let mut i = self.pages.len() - 1;
        let n = self.pages[i].len();
        let used = Self::hdr(i) + self.used[i] + data.len() + 1 + 4 + 2 * (n + 2);
        if used > BLOCK_MAX || n >= 2000 {
            self.pages.push(Vec::new());
            self.used.push(0);
            i += 1;
        }
        self.used[i] += data.len();
        self.pages[i].push(data);
        ((self.pages[i].len() as u32) << 5) | ((i as u32) << 16)
    }

    fn new_sub_nid(&mut self) -> u32 {
        let nid = (self.sub_index << 5) | 0x1F;
        self.sub_index += 1;
        nid
    }

    /// Значение переменной длины: в heap или в подузел, если большое.
    fn value(&mut self, data: Vec<u8>) -> u32 {
        if data.is_empty() {
            return 0;
        }
        if data.len() <= HEAP_MAX {
            return self.alloc(data);
        }
        let nid = self.new_sub_nid();
        self.subs.push((nid, Node::new(split(&data, BLOCK_MAX))));
        nid
    }

    fn build(self, user_root: u32) -> (Vec<Vec<u8>>, Vec<(u32, Node)>) {
        struct Pg {
            off: usize,
            body: Vec<u8>,
            ib: usize,
            pmap: Vec<u8>,
        }
        let mut bodies = Vec::with_capacity(self.pages.len());
        for (i, allocs) in self.pages.iter().enumerate() {
            let off = Self::hdr(i);
            let mut body = Vec::with_capacity(self.used[i] + 1);
            let mut offs = Vec::with_capacity(allocs.len() + 1);
            for a in allocs {
                offs.push((off + body.len()) as u16);
                body.extend_from_slice(a);
            }
            let end = off + body.len();
            let pad = end & 1;
            offs.push(end as u16);
            let mut pmap = Vec::with_capacity(4 + 2 * offs.len());
            pmap.extend_from_slice(&(allocs.len() as u16).to_le_bytes());
            pmap.extend_from_slice(&0u16.to_le_bytes());
            for o in offs {
                pmap.extend_from_slice(&o.to_le_bytes());
            }
            body.resize(body.len() + pad, 0);
            bodies.push(Pg {
                off,
                body,
                ib: end + pad,
                pmap,
            });
        }
        let levels: Vec<u8> = bodies
            .iter()
            .map(|p| fill_level(BLOCK_MAX as i64 - (p.ib + p.pmap.len()) as i64))
            .collect();
        let pack_levels = |lv: &[u8], width: usize| {
            let mut out = vec![0u8; lv.len().div_ceil(2).max(width)];
            for (j, v) in lv.iter().enumerate() {
                out[j / 2] |= (v & 0xF) << (4 * (j % 2));
            }
            out
        };
        let mut pages = Vec::with_capacity(bodies.len());
        for (i, p) in bodies.into_iter().enumerate() {
            let mut page = Vec::with_capacity(p.off + p.body.len() + p.pmap.len());
            page.extend_from_slice(&(p.ib as u16).to_le_bytes());
            if i == 0 {
                page.push(0xEC);
                page.push(self.client);
                page.extend_from_slice(&user_root.to_le_bytes());
                page.extend_from_slice(&pack_levels(&levels[..levels.len().min(8)], 4));
            } else if p.off == 66 {
                let hi = levels.len().min(i + 128);
                page.extend_from_slice(&pack_levels(&levels[i..hi], 64));
            }
            page.extend_from_slice(&p.body);
            page.extend_from_slice(&p.pmap);
            pages.push(page);
        }
        (pages, self.subs)
    }
}

/// BTree-on-heap. records: [(key, data)]. Возвращает HID заголовка.
fn bth(heap: &mut Heap, cb_key: usize, cb_ent: usize, mut records: Vec<(u64, Vec<u8>)>) -> u32 {
    records.sort_by_key(|r| r.0); // стабильная, как sorted() в Python
    let (mut levels, mut root) = (0u8, 0u32);
    if !records.is_empty() {
        let per = HEAP_MAX / (cb_key + cb_ent);
        let mut nodes: Vec<(u64, u32)> = Vec::new();
        for part in records.chunks(per) {
            let mut data = Vec::with_capacity(part.len() * (cb_key + cb_ent));
            for (k, d) in part {
                data.extend_from_slice(&k.to_le_bytes()[..cb_key]);
                data.extend_from_slice(d);
            }
            nodes.push((part[0].0, heap.alloc(data)));
        }
        while nodes.len() > 1 {
            let per = HEAP_MAX / (cb_key + 4);
            let mut nxt = Vec::new();
            for part in nodes.chunks(per) {
                let mut data = Vec::with_capacity(part.len() * (cb_key + 4));
                for (k, h) in part {
                    data.extend_from_slice(&k.to_le_bytes()[..cb_key]);
                    data.extend_from_slice(&h.to_le_bytes());
                }
                nxt.push((part[0].0, heap.alloc(data)));
            }
            nodes = nxt;
            levels += 1;
        }
        root = nodes[0].1;
    }
    let mut hdr = vec![0xB5, cb_key as u8, cb_ent as u8, levels];
    hdr.extend_from_slice(&root.to_le_bytes());
    heap.alloc(hdr)
}

/// Property Context.
fn build_pc(props: &Props) -> Result<Node> {
    let mut heap = Heap::new(0xBC);
    let mut recs = Vec::new();
    for (tag, value) in props {
        let Some(value) = value else { continue };
        let (pid, ptype) = (tag >> 16, (tag & 0xFFFF) as u16);
        let dw = if matches!(ptype, PT_I4 | PT_I2 | PT_BOOL) {
            let mut b = encode(ptype, value)?;
            b.resize(4, 0);
            u32::from_le_bytes([b[0], b[1], b[2], b[3]])
        } else {
            let data = encode(ptype, value)?;
            if data.is_empty() {
                heap.alloc(Vec::new())
            } else {
                heap.value(data)
            }
        };
        let mut rec = Vec::with_capacity(6);
        rec.extend_from_slice(&ptype.to_le_bytes());
        rec.extend_from_slice(&dw.to_le_bytes());
        recs.push((pid as u64, rec));
    }
    let root = bth(&mut heap, 2, 6, recs);
    let (chunks, subs) = heap.build(root);
    Ok(Node { chunks, subs })
}

fn cell_size(ptype: u16) -> usize {
    match ptype {
        PT_I8 | PT_TIME => 8,
        PT_I2 => 2,
        PT_BOOL => 1,
        _ => 4, // I4 и HNID для переменных типов
    }
}

const ROW_ID: u32 = 0x67F20003;
const ROW_VER: u32 = 0x67F30003;

/// Table Context. rows: [(row_id, props)].
fn build_tc(columns: &[u32], rows: &[(u32, &Props)]) -> Result<Node> {
    let mut cols: Vec<u32> = columns.to_vec();
    cols.push(ROW_ID);
    cols.push(ROW_VER);
    cols.sort_unstable();
    cols.dedup();
    let rest: Vec<u32> = cols
        .iter()
        .copied()
        .filter(|c| *c != ROW_ID && *c != ROW_VER)
        .collect();
    let mut layout: HashMap<u32, (usize, usize)> = HashMap::new();
    layout.insert(ROW_ID, (0, 4));
    layout.insert(ROW_VER, (4, 4));
    let mut ib = 8;
    for want in [8, 4] {
        for &c in &rest {
            if cell_size((c & 0xFFFF) as u16) == want {
                layout.insert(c, (ib, want));
                ib += want;
            }
        }
    }
    let tci_4b = ib;
    for &c in &rest {
        if cell_size((c & 0xFFFF) as u16) == 2 {
            layout.insert(c, (ib, 2));
            ib += 2;
        }
    }
    let tci_2b = ib;
    for &c in &rest {
        if cell_size((c & 0xFFFF) as u16) == 1 {
            layout.insert(c, (ib, 1));
            ib += 1;
        }
    }
    let tci_1b = ib;
    let mut order = vec![ROW_ID, ROW_VER];
    order.extend_from_slice(&rest);
    let ibit: HashMap<u32, usize> = order.iter().enumerate().map(|(i, c)| (*c, i)).collect();
    let row_size = tci_1b + cols.len().div_ceil(8);

    let mut heap = Heap::new(0x7C);
    let mut matrix: Vec<u8> = Vec::with_capacity(rows.len() * row_size);
    let mut index = Vec::with_capacity(rows.len());
    for (n, (row_id, values)) in rows.iter().enumerate() {
        let mut row = vec![0u8; row_size];
        for &c in &cols {
            let v_owned;
            let v = match c {
                ROW_ID => {
                    v_owned = Val::Int(*row_id as i64);
                    &v_owned
                }
                ROW_VER => {
                    v_owned = Val::Int(n as i64 + 1);
                    &v_owned
                }
                _ => match props_get(values, c) {
                    Some(v) => v,
                    None => continue,
                },
            };
            let (off, size) = layout[&c];
            let ptype = (c & 0xFFFF) as u16;
            if ptype == PT_STR || ptype == PT_BIN {
                let data = encode(ptype, v)?;
                if data.is_empty() {
                    continue;
                }
                row[off..off + 4].copy_from_slice(&heap.value(data).to_le_bytes());
            } else {
                let mut cell = encode(ptype, v)?;
                cell.resize(size, 0);
                row[off..off + size].copy_from_slice(&cell);
            }
            let b = ibit[&c];
            row[tci_1b + b / 8] |= 0x80 >> (b % 8);
        }
        matrix.extend_from_slice(&row);
        index.push((*row_id as u64, (n as u32).to_le_bytes().to_vec()));
    }
    let hid_index = bth(&mut heap, 4, 4, index);
    let mut hnid_rows = 0u32;
    if !rows.is_empty() {
        if matrix.len() <= HEAP_MAX {
            hnid_rows = heap.alloc(matrix);
        } else {
            let per = BLOCK_MAX / row_size;
            let chunks = matrix.chunks(per * row_size).map(|c| c.to_vec()).collect();
            hnid_rows = heap.new_sub_nid();
            heap.subs.push((hnid_rows, Node::new(chunks)));
        }
    }
    let mut info = vec![0x7C, cols.len() as u8];
    for v in [tci_4b, tci_2b, tci_1b, row_size] {
        info.extend_from_slice(&(v as u16).to_le_bytes());
    }
    for v in [hid_index, hnid_rows, 0] {
        info.extend_from_slice(&v.to_le_bytes());
    }
    for &c in &cols {
        let (off, size) = layout[&c];
        info.extend_from_slice(&c.to_le_bytes());
        info.extend_from_slice(&(off as u16).to_le_bytes());
        info.push(size as u8);
        info.push(ibit[&c] as u8);
    }
    let root = heap.alloc(info);
    let (chunks, subs) = heap.build(root);
    Ok(Node { chunks, subs })
}

// ================================================================ NDB: файл, блоки, деревья

struct Allocator {
    pos: u64,
    bits: Vec<u8>, // 1 бит = 64 байта, от FIRST_AMAP
}

impl Allocator {
    fn new() -> Self {
        Allocator {
            pos: FIRST_AMAP + 2 * PAGE,
            bits: Vec::new(),
        } // после первой AMap и PMap
    }

    /// Сколько страниц в начале области занято служебными (с учётом пропусков).
    fn reserved_pages(k: u64) -> u64 {
        *region_pages(k).last().unwrap() + 1
    }

    fn mark(&mut self, ib: u64, size: u64) {
        let a = ((ib - FIRST_AMAP) / 64) as usize;
        let b = (ib + size - FIRST_AMAP).div_ceil(64) as usize;
        let need = b / 8 + 1;
        if self.bits.len() < need {
            let extra = need - self.bits.len() + (1 << 20);
            self.bits.resize(self.bits.len() + extra, 0);
        }
        mark_bits(&mut self.bits, a, b);
    }

    fn alloc(&mut self, size: u64, al: u64) -> u64 {
        let mut p = align(self.pos, al);
        loop {
            let k = (p - FIRST_AMAP) / REGION;
            let start = FIRST_AMAP + k * REGION;
            let res_end = start + Self::reserved_pages(k) * PAGE;
            if p < res_end {
                p = align(res_end, al);
                continue;
            }
            if p + size > start + REGION {
                p = start + REGION;
                continue;
            }
            break;
        }
        self.pos = p + size;
        self.mark(p, size);
        p
    }
}

struct Folder {
    nid: u32,
    parent: u32,
    name: String,
    cls: Option<&'static str>,
    children: Vec<u32>,
    rows: Vec<(u32, Props)>,
    unread: i64,
    count: i64,
}

/// Письмо, готовое к записи (то же, что dict из eml_to_message в Python).
pub struct Message {
    pub props: Props,
    pub recipients: Vec<Props>,
    pub attachments: Vec<(Props, Props)>, // (свойства вложения, строка таблицы)
    pub read: bool,
    /// Дата получения (секунды эпохи) — для отчёта об экспорте; в PST уже лежит в props.
    pub received: i64,
}

pub struct Writer {
    path: String,
    f: BufWriter<File>,
    fpos: u64,
    al: Allocator,
    // блоки пишутся с возрастающими BID, поэтому BBT уже отсортирована
    bbt: Vec<(u64, u64, u16)>,
    nbt: Vec<(u64, u64, u64, u32)>, // (nid, bid_data, bid_sub, nid_parent)
    bid: u64,
    pbid: u64,
    uid: [u8; 16],
    display_name: String,
    password: String,
    folder_idx: u32,
    msg_idx: u32,
    hw: HashMap<u32, u32>,
    folders: Vec<Folder>,
    folder_pos: HashMap<u32, usize>,
}

impl Writer {
    pub fn create(
        path: &str,
        display_name: &str,
        password: &str,
        uid: [u8; 16],
        deleted_name: String,
    ) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(path)?;
        let mut w = Writer {
            path: path.to_string(),
            f: BufWriter::with_capacity(1 << 20, file),
            fpos: 0,
            al: Allocator::new(),
            bbt: Vec::new(),
            nbt: Vec::new(),
            bid: 0,
            pbid: 0,
            uid,
            display_name: display_name.to_string(),
            password: password.to_string(),
            folder_idx: 0x405,
            msg_idx: 0x10000,
            hw: HashMap::new(),
            folders: Vec::new(),
            folder_pos: HashMap::new(),
        };
        w.add_folder_rec(NID_ROOT_FOLDER, NID_ROOT_FOLDER, String::new(), None);
        w.add_folder_rec(
            NID_IPM_SUBTREE,
            NID_ROOT_FOLDER,
            "Top of Personal Folders".into(),
            None,
        );
        w.add_folder_rec(NID_SEARCH_ROOT, NID_ROOT_FOLDER, "Search Root".into(), None);
        w.add_folder_rec(
            NID_DELETED_ITEMS,
            NID_IPM_SUBTREE,
            deleted_name,
            Some("IPF.Note"),
        );
        Ok(w)
    }

    // ---------- размер ----------
    pub fn size(&self) -> u64 {
        self.al.pos
    }

    /// Оценка (с запасом) того, сколько байт добавит close().
    pub fn close_overhead(&self) -> i64 {
        let rows: i64 = self.folders.iter().map(|f| f.count).sum();
        let blocks = self.bbt.len() as i64 + rows / 8 + 2000;
        let nodes = self.nbt.len() as i64 + 4 * self.folders.len() as i64 + 64;
        blocks * 28 + nodes * 37 + rows * 900 + 2 * REGION as i64 + 1024 * 1024
    }

    fn write_at(&mut self, pos: u64, data: &[u8]) -> io::Result<()> {
        if pos != self.fpos {
            self.f.seek(SeekFrom::Start(pos))?;
        }
        self.f.write_all(data)?;
        self.fpos = pos + data.len() as u64;
        Ok(())
    }

    // ---------- блоки ----------
    fn write_block(&mut self, data: &[u8], internal: bool) -> Result<u64> {
        self.bid += 4;
        let bid = self.bid | if internal { 2 } else { 0 };
        let cb = data.len();
        let total = align(cb as u64 + 16, 64);
        let ib = self.al.alloc(total, 64);
        let mut buf = Vec::with_capacity(total as usize);
        buf.extend_from_slice(data);
        buf.resize(total as usize - 16, 0);
        buf.extend_from_slice(&(cb as u16).to_le_bytes());
        buf.extend_from_slice(&sig(ib, bid).to_le_bytes());
        buf.extend_from_slice(&pst_crc(data).to_le_bytes());
        buf.extend_from_slice(&bid.to_le_bytes());
        self.write_at(ib, &buf)?;
        self.bbt.push((bid, ib, cb as u16));
        Ok(bid)
    }

    fn xblock(&mut self, level: u8, entries: &[(u64, u64)]) -> Result<u64> {
        let total: u64 = entries.iter().map(|e| e.1).sum();
        let mut data = vec![1, level];
        data.extend_from_slice(&(entries.len() as u16).to_le_bytes());
        data.extend_from_slice(&(total as u32).to_le_bytes());
        for (b, _) in entries {
            data.extend_from_slice(&b.to_le_bytes());
        }
        self.write_block(&data, true)
    }

    fn write_tree(&mut self, chunks: &[Vec<u8>]) -> Result<u64> {
        if chunks.len() == 1 {
            return self.write_block(&chunks[0], false);
        }
        // Outlook требует: все блоки дерева, кроме последнего, заполнены полностью
        let mut bids = Vec::with_capacity(chunks.len());
        let mut full = vec![0u8; BLOCK_MAX];
        for (i, c) in chunks.iter().enumerate() {
            if i + 1 < chunks.len() && c.len() < BLOCK_MAX {
                full[..c.len()].copy_from_slice(c);
                full[c.len()..].fill(0);
                let b = self.write_block(&full, false)?;
                bids.push((b, BLOCK_MAX as u64));
            } else {
                bids.push((self.write_block(c, false)?, c.len() as u64));
            }
        }
        let mut xs = Vec::new();
        for g in bids.chunks(1021) {
            let total: u64 = g.iter().map(|e| e.1).sum();
            xs.push((self.xblock(1, g)?, total));
        }
        if xs.len() == 1 {
            return Ok(xs[0].0);
        }
        if xs.len() > 1021 {
            return Err(PstError::TooLarge);
        }
        self.xblock(2, &xs)
    }

    fn write_subnodes(&mut self, mut entries: Vec<(u64, u64, u64)>) -> Result<u64> {
        if entries.is_empty() {
            return Ok(0);
        }
        entries.sort_unstable();
        if entries.len() <= 340 {
            let mut data = vec![2, 0];
            data.extend_from_slice(&(entries.len() as u16).to_le_bytes());
            data.extend_from_slice(&0u32.to_le_bytes());
            for (n, d, s) in &entries {
                data.extend_from_slice(&n.to_le_bytes());
                data.extend_from_slice(&d.to_le_bytes());
                data.extend_from_slice(&s.to_le_bytes());
            }
            return self.write_block(&data, true);
        }
        let mut sls = Vec::new();
        for part in entries.chunks(340) {
            sls.push((part[0].0, self.write_subnodes(part.to_vec())?));
        }
        let mut data = vec![2, 1];
        data.extend_from_slice(&(sls.len() as u16).to_le_bytes());
        data.extend_from_slice(&0u32.to_le_bytes());
        for (n, b) in &sls {
            data.extend_from_slice(&n.to_le_bytes());
            data.extend_from_slice(&b.to_le_bytes());
        }
        self.write_block(&data, true)
    }

    fn note_nid(&mut self, nid: u32) {
        let (t, idx) = (nid & 0x1F, nid >> 5);
        let e = self.hw.entry(t).or_insert(0);
        if idx + 1 > *e {
            *e = idx + 1;
        }
    }

    fn write_node(&mut self, node: Node) -> Result<(u64, u64)> {
        for (nid, _) in &node.subs {
            self.note_nid(*nid);
        }
        let mut subs = Vec::with_capacity(node.subs.len());
        for (nid, child) in node.subs {
            let (d, s) = self.write_node(child)?;
            subs.push((nid as u64, d, s));
        }
        let d = self.write_tree(&node.chunks)?;
        let s = self.write_subnodes(subs)?;
        Ok((d, s))
    }

    fn put(&mut self, nid: u32, node: Node, parent: u32) -> Result<()> {
        let (d, s) = self.write_node(node)?;
        self.note_nid(nid);
        self.nbt.push((nid as u64, d, s, parent));
        Ok(())
    }

    // ---------- папки ----------
    fn add_folder_rec(&mut self, nid: u32, parent: u32, name: String, cls: Option<&'static str>) {
        if let Some(&old) = self.folder_pos.get(&nid) {
            self.folders[old] = Folder {
                nid,
                parent,
                name,
                cls,
                children: vec![],
                rows: vec![],
                unread: 0,
                count: 0,
            };
        } else {
            self.folder_pos.insert(nid, self.folders.len());
            self.folders.push(Folder {
                nid,
                parent,
                name,
                cls,
                children: vec![],
                rows: vec![],
                unread: 0,
                count: 0,
            });
        }
        if parent != nid
            && let Some(&p) = self.folder_pos.get(&parent)
        {
            self.folders[p].children.push(nid);
        }
    }

    pub fn add_folder(&mut self, parent_nid: u32, name: &str) -> u32 {
        let nid = (self.folder_idx << 5) | 0x02;
        self.folder_idx += 1;
        self.add_folder_rec(nid, parent_nid, name.to_string(), Some("IPF.Note"));
        nid
    }

    pub fn entry_id(&self, nid: u32) -> Vec<u8> {
        let mut v = vec![0u8; 4];
        v.extend_from_slice(&self.uid);
        v.extend_from_slice(&nid.to_le_bytes());
        v
    }

    // ---------- письма ----------
    pub fn add_message(&mut self, folder_nid: u32, msg: Message) -> Result<()> {
        let nid = (self.msg_idx << 5) | 0x04;
        self.msg_idx += 1;
        let mut props = msg.props;
        let has_atts = !msg.attachments.is_empty();
        let flags = (if msg.read { 0x01 } else { 0 }) | (if has_atts { 0x10 } else { 0 });
        props_set(&mut props, 0x0E070003, Some(Val::Int(flags)));
        props_set(&mut props, 0x0E1B000B, Some(Val::Bool(has_atts)));
        let mut node = build_pc(&props)?;

        let rec_rows: Vec<(u32, &Props)> = msg
            .recipients
            .iter()
            .enumerate()
            .map(|(i, r)| (i as u32, r))
            .collect();
        node.set_sub(NID_RECIPIENT_TABLE, build_tc(RECIPIENT_COLS, &rec_rows)?);
        if has_atts {
            let mut att_rows = Vec::with_capacity(msg.attachments.len());
            for (i, (ap, row)) in msg.attachments.iter().enumerate() {
                let anid = ((0x400 + i as u32) << 5) | 0x05;
                node.set_sub(anid, build_pc(ap)?);
                att_rows.push((anid, row));
            }
            node.set_sub(NID_ATTACHMENT_TABLE, build_tc(ATTACHMENT_COLS, &att_rows)?);
        }
        self.put(nid, node, folder_nid)?;

        let &fi = self
            .folder_pos
            .get(&folder_nid)
            .ok_or(PstError::NoFolder(folder_nid))?;
        let mut row: Props = Vec::new();
        for &t in CONTENTS_COLS {
            if props_has(&props, t) {
                row.push((t, props_get(&props, t).cloned()));
            }
        }
        props_set(&mut row, 0x0E070003, Some(Val::Int(flags)));
        let f = &mut self.folders[fi];
        f.rows.push((nid, row));
        f.count += 1;
        if !msg.read {
            f.unread += 1;
        }
        Ok(())
    }

    // ---------- завершение ----------
    fn folder_props(f: &Folder) -> Props {
        let mut p: Props = vec![
            (0x36020003, Some(Val::Int(f.count))),
            (0x36030003, Some(Val::Int(f.unread))),
            (0x360A000B, Some(Val::Bool(!f.children.is_empty()))),
            (0x3001001F, Some(Val::Str(f.name.clone()))),
        ];
        if let Some(c) = f.cls {
            p.push((0x3613001F, Some(Val::Str(c.to_string()))));
        }
        p
    }

    fn write_folders(&mut self) -> Result<()> {
        for i in 0..self.folders.len() {
            let (nid, parent) = (self.folders[i].nid, self.folders[i].parent);
            let props = Self::folder_props(&self.folders[i]);
            self.put(nid, build_pc(&props)?, parent)?;
            let hier: Vec<(u32, Props)> = self.folders[i]
                .children
                .iter()
                .map(|c| (*c, Self::folder_props(&self.folders[self.folder_pos[c]])))
                .collect();
            let hier_rows: Vec<(u32, &Props)> = hier.iter().map(|(c, p)| (*c, p)).collect();
            let base = nid & !0x1F;
            self.put(base | 0x0D, build_tc(HIERARCHY_COLS, &hier_rows)?, 0)?;
            let rows = std::mem::take(&mut self.folders[i].rows);
            let row_refs: Vec<(u32, &Props)> = rows.iter().map(|(n, p)| (*n, p)).collect();
            let tc = build_tc(CONTENTS_COLS, &row_refs)?;
            drop(row_refs);
            drop(rows);
            self.put(base | 0x0E, tc, 0)?;
            self.put(base | 0x0F, build_tc(FAI_COLS, &[])?, 0)?;
        }
        Ok(())
    }

    fn write_special(&mut self) -> Result<()> {
        let pw = if self.password.is_empty() {
            0
        } else {
            pst_crc(self.password.as_bytes()) as i64
        };
        let store: Props = vec![
            (0x0FF90102, Some(Val::Bin(self.uid.to_vec()))),
            (0x3001001F, Some(Val::Str(self.display_name.clone()))),
            (0x35E00102, Some(Val::Bin(self.entry_id(NID_IPM_SUBTREE)))),
            (0x35E30102, Some(Val::Bin(self.entry_id(NID_DELETED_ITEMS)))),
            (0x35E70102, Some(Val::Bin(self.entry_id(NID_SEARCH_ROOT)))),
            (0x35DF0003, Some(Val::Int(0x89))),
            (0x67FF0003, Some(Val::Int(pw))),
        ];
        self.put(NID_MESSAGE_STORE, build_pc(&store)?, 0)?;
        let n2id: Props = vec![
            (0x00010003, Some(Val::Int(251))),
            (0x00020102, Some(Val::Bin(vec![]))),
            (0x00030102, Some(Val::Bin(vec![]))),
            (0x00040102, Some(Val::Bin(vec![]))),
        ];
        self.put(NID_NAME_TO_ID_MAP, build_pc(&n2id)?, 0)?;
        self.put(0xA1, build_pc(&vec![])?, 0)?;
        self.put(0xC1, build_pc(&vec![])?, 0)?;
        for nid in [
            0x1E1, 0x201, 0x241, 0x261, 0x281, 0x2A1, 0x2E1, 0x301, 0x321,
        ] {
            self.put(nid, Node::new(vec![Vec::new()]), 0)?;
        }
        self.put(0x60D, build_tc(HIERARCHY_COLS, &[])?, 0)?;
        self.put(0x60E, build_tc(CONTENTS_COLS, &[])?, 0)?;
        self.put(0x60F, build_tc(FAI_COLS, &[])?, 0)?;
        self.put(0x610, build_tc(SEARCH_CONTENTS_COLS, &[])?, 0)?;
        self.put(NID_ATTACHMENT_TABLE, build_tc(ATTACHMENT_COLS, &[])?, 0)?;
        self.put(NID_RECIPIENT_TABLE, build_tc(RECIPIENT_COLS, &[])?, 0)?;
        Ok(())
    }

    fn write_page(&mut self, body: &[u8], ptype: u8) -> Result<(u64, u64)> {
        self.pbid += 1;
        let bid = self.pbid;
        let ib = self.al.alloc(PAGE, PAGE);
        let mut buf = Vec::with_capacity(PAGE as usize);
        buf.extend_from_slice(body);
        buf.extend_from_slice(&[ptype, ptype]);
        buf.extend_from_slice(&sig(ib, bid).to_le_bytes());
        buf.extend_from_slice(&pst_crc(body).to_le_bytes());
        buf.extend_from_slice(&bid.to_le_bytes());
        self.write_at(ib, &buf)?;
        Ok((bid, ib))
    }

    /// B-дерево: keys[i] — ключ записи i, entries — байты записей подряд.
    fn write_btree(
        &mut self,
        keys: &[u64],
        entries: &[u8],
        cbent: usize,
        ptype: u8,
    ) -> Result<(u64, u64)> {
        let maxent = 488 / cbent;
        let mut pages: Vec<(u64, u64, u64)> = Vec::new();
        let count = keys.len();
        let mut i = 0;
        while i < count {
            let n = maxent.min(count - i);
            let mut body = entries[i * cbent..(i + n) * cbent].to_vec();
            body.resize(488, 0);
            body.extend_from_slice(&[n as u8, maxent as u8, cbent as u8, 0, 0, 0, 0, 0]);
            let (bid, ib) = self.write_page(&body, ptype)?;
            pages.push((keys[i], bid, ib));
            i += maxent;
        }
        let mut level = 1u8;
        while pages.len() > 1 {
            let maxent = 488 / 24;
            let mut nxt = Vec::new();
            for part in pages.chunks(maxent) {
                let mut body = Vec::with_capacity(496);
                for (k, b, ib) in part {
                    body.extend_from_slice(&k.to_le_bytes());
                    body.extend_from_slice(&b.to_le_bytes());
                    body.extend_from_slice(&ib.to_le_bytes());
                }
                body.resize(488, 0);
                body.extend_from_slice(&[part.len() as u8, maxent as u8, 24, level, 0, 0, 0, 0]);
                let (bid, ib) = self.write_page(&body, ptype)?;
                nxt.push((part[0].0, bid, ib));
            }
            pages = nxt;
            level += 1;
        }
        Ok((pages[0].1, pages[0].2))
    }

    fn trailer_page(&mut self, ib: u64, body: &[u8], ptype: u8) -> Result<()> {
        let mut buf = Vec::with_capacity(PAGE as usize);
        buf.extend_from_slice(body);
        buf.extend_from_slice(&[ptype, ptype, 0, 0]);
        buf.extend_from_slice(&pst_crc(body).to_le_bytes());
        buf.extend_from_slice(&ib.to_le_bytes());
        self.write_at(ib, &buf)?;
        Ok(())
    }

    fn write_maps(&mut self) -> Result<(u64, u64, u64)> {
        let last_k = (self.al.pos - 1 - FIRST_AMAP) / REGION;
        for k in 0..=last_k {
            let start = FIRST_AMAP + k * REGION;
            for pg in region_pages(k) {
                self.al.mark(start + pg * PAGE, PAGE);
            }
        }
        let eof = FIRST_AMAP + (last_k + 1) * REGION;
        let need = ((last_k + 1) * 496) as usize;
        if self.al.bits.len() < need {
            self.al.bits.resize(need, 0);
        }
        let mut free_total = 0u64;
        let mut longest = Vec::with_capacity(last_k as usize + 1);
        let ff = [0xFFu8; 496];
        for k in 0..=last_k {
            let amap: Vec<u8> = self.al.bits[(k * 496) as usize..((k + 1) * 496) as usize].to_vec();
            let ones: u64 = amap.iter().map(|b| b.count_ones() as u64).sum();
            free_total += (496 * 8 - ones) * 64;
            longest.push(longest_free_run(&amap));
            let start = FIRST_AMAP + k * REGION;
            self.trailer_page(start, &amap, PT_AMAP)?;
            let pages = region_pages(k);
            if pages.contains(&1) {
                self.trailer_page(start + PAGE, &ff, PT_PMAP)?;
            }
            if pages.contains(&3) {
                self.trailer_page(start + 3 * PAGE, &ff, PT_FPMAP)?;
            }
        }
        for k in 0..=last_k {
            if region_pages(k).contains(&2) {
                let lo = k as usize;
                let hi = (lo + 496).min(longest.len());
                let mut fm = longest[lo..hi].to_vec();
                fm.resize(496, 0);
                self.trailer_page(FIRST_AMAP + k * REGION + 2 * PAGE, &fm, PT_FMAP)?;
            }
        }
        let mut dl = vec![0x01, 0, 0, 0, 0, 0, 0, 0];
        dl.resize(496, 0);
        self.trailer_page(DLIST_IB, &dl, PT_DL)?;
        self.write_at(eof - 1, &[0])?;
        Ok((eof, FIRST_AMAP + last_k * REGION, free_total))
    }

    pub fn close(mut self) -> Result<()> {
        self.write_folders()?;
        self.write_special()?;
        let mut nbt = std::mem::take(&mut self.nbt);
        nbt.sort_unstable();
        let keys: Vec<u64> = nbt.iter().map(|e| e.0).collect();
        let mut ents = Vec::with_capacity(nbt.len() * 32);
        for (n, d, s, p) in &nbt {
            ents.extend_from_slice(&n.to_le_bytes());
            ents.extend_from_slice(&d.to_le_bytes());
            ents.extend_from_slice(&s.to_le_bytes());
            ents.extend_from_slice(&p.to_le_bytes());
            ents.extend_from_slice(&0u32.to_le_bytes());
        }
        drop(nbt);
        let (nbt_bid, nbt_ib) = self.write_btree(&keys, &ents, 32, PT_NBT)?;
        let bbt = std::mem::take(&mut self.bbt);
        let keys: Vec<u64> = bbt.iter().map(|e| e.0).collect();
        let mut ents = Vec::with_capacity(bbt.len() * 24);
        for (b, ib, cb) in &bbt {
            ents.extend_from_slice(&b.to_le_bytes());
            ents.extend_from_slice(&ib.to_le_bytes());
            ents.extend_from_slice(&cb.to_le_bytes());
            ents.extend_from_slice(&2u16.to_le_bytes());
            ents.extend_from_slice(&0u32.to_le_bytes());
        }
        drop(bbt);
        let (bbt_bid, bbt_ib) = self.write_btree(&keys, &ents, 24, PT_BBT)?;
        let (eof, amap_last, amap_free) = self.write_maps()?;

        let mut rgnid = [0x400u32; 32];
        rgnid[3] = 0x4000;
        rgnid[4] = 0x10000;
        rgnid[8] = 0x8000;
        for (&t, &nxt) in &self.hw {
            rgnid[t as usize] = rgnid[t as usize].max(nxt);
        }
        let mut h = vec![0u8; 564];
        h[0..4].copy_from_slice(b"!BDN");
        h[8..10].copy_from_slice(&0x4D53u16.to_le_bytes());
        h[10..12].copy_from_slice(&23u16.to_le_bytes());
        h[12..14].copy_from_slice(&19u16.to_le_bytes());
        h[14] = 1;
        h[15] = 1;
        h[32..40].copy_from_slice(&(self.pbid + 1).to_le_bytes());
        h[40..44].copy_from_slice(&1u32.to_le_bytes());
        for (i, v) in rgnid.iter().enumerate() {
            h[44 + 4 * i..48 + 4 * i].copy_from_slice(&v.to_le_bytes());
        }
        let mut root = Vec::with_capacity(72);
        root.extend_from_slice(&0u32.to_le_bytes());
        for v in [
            eof, amap_last, amap_free, 0, nbt_bid, nbt_ib, bbt_bid, bbt_ib,
        ] {
            root.extend_from_slice(&v.to_le_bytes());
        }
        root.extend_from_slice(&[2, 0, 0, 0]);
        h[180..252].copy_from_slice(&root);
        h[256..512].fill(0xFF);
        h[512] = 0x80;
        h[513] = 0; // NDB_CRYPT_NONE
        h[516..524].copy_from_slice(&(self.bid + 4).to_le_bytes());
        let partial = pst_crc(&h[8..8 + 471]);
        h[4..8].copy_from_slice(&partial.to_le_bytes());
        let full = pst_crc(&h[8..8 + 516]);
        h[524..528].copy_from_slice(&full.to_le_bytes());
        self.write_at(0, &h)?;
        self.f.flush()?;
        Ok(())
    }

    pub fn abort(self) {
        let path = self.path.clone();
        drop(self);
        let _ = fs::remove_file(path);
    }
}
