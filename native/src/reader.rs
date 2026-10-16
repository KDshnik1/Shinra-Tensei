//! Независимая проверка PST по [MS-PST] — порт core/pst_reader.py.
//!
//! Читает файл заново, ничего не беря у писателя, кроме констант формата:
//! заголовок, CRC и подписи страниц и блоков, B-деревья, AMap, heap/BTH/PC/TC,
//! счётчики писем в папках.

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};

use crate::pst::pst_crc;

const FIRST_AMAP: u64 = 0x4400;
const REGION: u64 = 0x3E000;

/// Цикл или слишком глубокая вложенность: без этой проверки повреждённый файл
/// уводил бы рекурсию в бесконечность (переполнение стека роняет всю программу).
pub const BROKEN: &str = "повреждённая структура файла";
const MAX_BTREE_DEPTH: usize = 16;
const MAX_FOLDER_DEPTH: usize = 250;

/// Ошибка проверки: шаблон сообщения (переводится в Python) и значения подстановок.
#[derive(Debug)]
pub struct CheckError {
    pub template: &'static str,
    pub args: Vec<(&'static str, String)>,
}

type R<T> = Result<T, CheckError>;

fn fail<T>(template: &'static str) -> R<T> {
    Err(CheckError {
        template,
        args: vec![],
    })
}

fn chk(cond: bool, template: &'static str) -> R<()> {
    if cond { Ok(()) } else { fail(template) }
}

/// Проверка с подстановкой «{name:#x}» / «{0:#x}» / «{name}».
fn chk_a(cond: bool, template: &'static str, args: &[(&'static str, String)]) -> R<()> {
    if cond {
        Ok(())
    } else {
        Err(CheckError {
            template,
            args: args.to_vec(),
        })
    }
}

fn hx(v: u64) -> String {
    format!("{v:#x}")
}

fn u16_at(b: &[u8], o: usize) -> R<u16> {
    b.get(o..o + 2)
        .map(|s| u16::from_le_bytes([s[0], s[1]]))
        .ok_or(CheckError {
            template: "данные обрезаны",
            args: vec![],
        })
}

fn u32_at(b: &[u8], o: usize) -> R<u32> {
    b.get(o..o + 4)
        .map(|s| u32::from_le_bytes(s.try_into().unwrap()))
        .ok_or(CheckError {
            template: "данные обрезаны",
            args: vec![],
        })
}

fn u64_at(b: &[u8], o: usize) -> R<u64> {
    b.get(o..o + 8)
        .map(|s| u64::from_le_bytes(s.try_into().unwrap()))
        .ok_or(CheckError {
            template: "данные обрезаны",
            args: vec![],
        })
}

fn sig(ib: u64, bid: u64) -> u16 {
    let x = ib ^ bid;
    (((x >> 16) ^ x) & 0xFFFF) as u16
}

fn region_pages(k: u64) -> Vec<u64> {
    let mut pages = vec![0];
    if k.is_multiple_of(8) {
        pages.push(1);
        if k >= 128 && (k - 128).is_multiple_of(496) {
            pages.push(2);
        }
        if k >= 128 * 64 && (k - 128 * 64).is_multiple_of(496 * 64) {
            pages.push(3);
        }
    }
    pages
}

fn mark_bits(bits: &mut [u8], mut a: usize, mut b: usize) {
    b = b.min(bits.len() * 8);
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

#[derive(Debug, Clone)]
pub enum PVal {
    Int(u64),
    Str(String),
    Bin, // для проверки содержимое не нужно
}

type PropMap = HashMap<u32, PVal>;

struct Reader {
    f: File,
    eof: u64,
    rgnid: [u32; 32],
    bbt: HashMap<u64, (u64, u16)>,
    nbt: HashMap<u64, (u64, u64, u32)>,
    nbt_order: Vec<u64>,
    pages: Vec<u64>,
    seen_pages: HashSet<u64>,
}

fn io_err(_: std::io::Error) -> CheckError {
    CheckError {
        template: "данные обрезаны",
        args: vec![],
    }
}

impl Reader {
    fn read_at(&mut self, ib: u64, n: usize) -> R<Vec<u8>> {
        let mut buf = vec![0u8; n];
        self.f.seek(SeekFrom::Start(ib)).map_err(io_err)?;
        self.f.read_exact(&mut buf).map_err(io_err)?;
        Ok(buf)
    }

    fn open(path: &str) -> Result<Self, std::io::Error> {
        let f = File::open(path)?;
        Ok(Reader {
            f,
            eof: 0,
            rgnid: [0; 32],
            bbt: HashMap::new(),
            nbt: HashMap::new(),
            nbt_order: Vec::new(),
            pages: Vec::new(),
            seen_pages: HashSet::new(),
        })
    }

    fn init(&mut self) -> R<()> {
        let h = self.read_at(0, 564)?;
        chk(&h[..4] == b"!BDN", "нет сигнатуры !BDN")?;
        chk(
            u32_at(&h, 4)? == pst_crc(&h[8..479]),
            "CRC заголовка (partial)",
        )?;
        chk(
            u32_at(&h, 524)? == pst_crc(&h[8..524]),
            "CRC заголовка (full)",
        )?;
        chk(u16_at(&h, 10)? >= 23, "не Unicode PST")?;
        self.eof = u64_at(&h, 184)?;
        let (nbt_bid, nbt_ib, bbt_bid, bbt_ib) = (
            u64_at(&h, 216)?,
            u64_at(&h, 224)?,
            u64_at(&h, 232)?,
            u64_at(&h, 240)?,
        );
        let len = self.f.seek(SeekFrom::End(0)).map_err(io_err)?;
        chk(len == self.eof, "размер файла не совпадает с ibFileEof")?;
        chk(
            self.eof >= FIRST_AMAP && (self.eof - FIRST_AMAP).is_multiple_of(REGION),
            "конец файла не на границе AMap",
        )?;
        for i in 0..32 {
            self.rgnid[i] = u32_at(&h, 44 + 4 * i)?;
        }
        self.walk(bbt_ib, bbt_bid, 0x80, 0)?;
        self.walk(nbt_ib, nbt_bid, 0x81, 0)?;
        self.check_amaps()
    }

    fn page(&mut self, ib: u64, bid: u64, ptype: u8) -> R<Vec<u8>> {
        chk(self.seen_pages.insert(ib), BROKEN)?;
        self.pages.push(ib);
        let p = self.read_at(ib, 512)?;
        let a = [("ib", hx(ib))];
        chk_a(
            p[496] == ptype && p[497] == ptype,
            "тип страницы @{ib:#x}",
            &a,
        )?;
        chk_a(
            u32_at(&p, 500)? == pst_crc(&p[..496]),
            "CRC страницы @{ib:#x}",
            &a,
        )?;
        chk_a(u64_at(&p, 504)? == bid, "BID страницы @{ib:#x}", &a)?;
        chk_a(
            u16_at(&p, 498)? == sig(ib, bid),
            "подпись страницы @{ib:#x}",
            &a,
        )?;
        Ok(p)
    }

    fn walk(&mut self, ib: u64, bid: u64, ptype: u8, depth: usize) -> R<()> {
        chk(depth < MAX_BTREE_DEPTH, BROKEN)?;
        let p = self.page(ib, bid, ptype)?;
        let (cent, cbent, level) = (p[488] as usize, p[490] as usize, p[491]);
        let mut prev: Option<u64> = None;
        for i in 0..cent {
            let e = p.get(i * cbent..(i + 1) * cbent).ok_or(CheckError {
                template: "данные обрезаны",
                args: vec![],
            })?;
            let key = u64_at(e, 0)?;
            chk(
                prev.is_none_or(|pv| key > pv),
                "ключи B-дерева не по возрастанию",
            )?;
            prev = Some(key);
            // размеры записей как в Python-версии (struct.unpack / unpack_from)
            let need = match (level, ptype) {
                (0, 0x80) => 20,
                (0, _) => 28,
                _ => 24,
            };
            chk(
                if level != 0 {
                    e.len() == need
                } else {
                    e.len() >= need
                },
                "данные обрезаны",
            )?;
            if level != 0 {
                let (cbid, cib) = (u64_at(e, 8)?, u64_at(e, 16)?);
                self.walk(cib, cbid, ptype, depth + 1)?;
            } else if ptype == 0x80 {
                self.bbt.insert(key, (u64_at(e, 8)?, u16_at(e, 16)?));
            } else {
                if !self.nbt.contains_key(&key) {
                    self.nbt_order.push(key);
                }
                self.nbt
                    .insert(key, (u64_at(e, 8)?, u64_at(e, 16)?, u32_at(e, 24)?));
            }
        }
        Ok(())
    }

    fn check_amaps(&mut self) -> R<()> {
        let nreg = ((self.eof - FIRST_AMAP) / REGION) as usize;
        let mut actual = vec![0u8; nreg * 496];
        let mut expect = vec![0u8; nreg * 496];
        for k in 0..nreg as u64 {
            let start = FIRST_AMAP + k * REGION;
            let p = self.read_at(start, 512)?;
            chk_a(
                p[496] == 0x84,
                "нет AMap @{start:#x}",
                &[("start", hx(start))],
            )?;
            chk_a(
                u32_at(&p, 500)? == pst_crc(&p[..496]),
                "CRC AMap @{start:#x}",
                &[("start", hx(start))],
            )?;
            actual[k as usize * 496..(k as usize + 1) * 496].copy_from_slice(&p[..496]);
            for pg in region_pages(k) {
                let base = (k * REGION / 64 + pg * 8) as usize;
                mark_bits(&mut expect, base, base + 8);
                if pg != 0 {
                    let (ptype, name) = match pg {
                        1 => (0x83, "PMap"),
                        2 => (0x82, "FMap"),
                        _ => (0x85, "FPMap"),
                    };
                    let at = start + pg * 512;
                    let q = self.read_at(at, 512)?;
                    chk_a(
                        q[496] == ptype && q[497] == ptype,
                        "нет {name} @{0:#x}",
                        &[("0", hx(at)), ("name", name.into())],
                    )?;
                    chk_a(
                        u32_at(&q, 500)? == pst_crc(&q[..496]),
                        "CRC {name} @{0:#x}",
                        &[("0", hx(at)), ("name", name.into())],
                    )?;
                }
            }
        }
        for &(ib, cb) in self.bbt.values() {
            chk(ib >= FIRST_AMAP, BROKEN)?;
            chk(
                ib.saturating_add((cb as u64 + 16).div_ceil(64) * 64) <= self.eof,
                "данные обрезаны",
            )?;
            let a = ((ib - FIRST_AMAP) / 64) as usize;
            mark_bits(&mut expect, a, a + (cb as usize + 16).div_ceil(64));
        }
        for &ib in &self.pages {
            chk(ib >= FIRST_AMAP, BROKEN)?;
            let a = ((ib - FIRST_AMAP) / 64) as usize;
            mark_bits(&mut expect, a, a + 8);
        }
        for k in 0..nreg {
            let r = k * 496..(k + 1) * 496;
            let ok = expect[r.clone()]
                .iter()
                .zip(&actual[r])
                .all(|(e, a)| e & !a == 0);
            chk_a(
                ok,
                "AMap @{0:#x} не отмечает занятые данные",
                &[("0", hx(FIRST_AMAP + k as u64 * REGION))],
            )?;
        }
        Ok(())
    }

    // ---------- блоки и узлы ----------
    fn block(&mut self, bid: u64) -> R<Vec<u8>> {
        let a = [("bid", hx(bid))];
        let Some(&(ib, cb)) = self.bbt.get(&bid) else {
            return chk_a(false, "блок {bid:#x} отсутствует в BBT", &a).map(|_| vec![]);
        };
        let cb = cb as usize;
        let total = (cb + 16).div_ceil(64) * 64;
        let raw = self.read_at(ib, total)?;
        let t = total - 16;
        chk_a(
            u16_at(&raw, t)? as usize == cb && u64_at(&raw, t + 8)? == bid,
            "трейлер блока {bid:#x}",
            &a,
        )?;
        chk_a(
            u16_at(&raw, t + 2)? == sig(ib, bid),
            "подпись блока {bid:#x}",
            &a,
        )?;
        chk_a(
            u32_at(&raw, t + 4)? == pst_crc(&raw[..cb]),
            "CRC блока {bid:#x}",
            &a,
        )?;
        let mut raw = raw;
        raw.truncate(cb);
        Ok(raw)
    }

    fn chunks(&mut self, bid: u64) -> R<Vec<Vec<u8>>> {
        self.chunks_at(bid, 0)
    }

    fn chunks_at(&mut self, bid: u64, depth: usize) -> R<Vec<Vec<u8>>> {
        if bid == 0 {
            return Ok(vec![]);
        }
        let data = self.block(bid)?;
        if bid & 2 == 0 {
            return Ok(vec![data]);
        }
        chk(depth < 2, BROKEN)?; // XBLOCK -> XXBLOCK -> данные, не глубже
        let (btype, level, cent, total) = (
            data.first().copied().unwrap_or(0),
            data.get(1).copied().unwrap_or(0),
            u16_at(&data, 2)? as usize,
            u32_at(&data, 4)? as usize,
        );
        chk(btype == 1, "ожидался XBLOCK")?;
        let mut out = Vec::new();
        for i in 0..cent {
            let sub = self.chunks_at(u64_at(&data, 8 + 8 * i)?, depth + 1)?;
            if i + 1 < cent {
                let n: usize = sub.iter().map(|c| c.len()).sum();
                let want = if level == 1 { 8176 } else { 8176 * 1021 };
                chk_a(
                    n == want,
                    "неполный промежуточный блок в дереве {bid:#x} (Outlook это отвергает)",
                    &[("bid", hx(bid))],
                )?;
            }
            out.extend(sub);
        }
        chk(
            out.iter().map(|c| c.len()).sum::<usize>() == total,
            "lcbTotal не совпадает",
        )?;
        Ok(out)
    }

    fn subnodes(&mut self, bid: u64) -> R<HashMap<u64, (u64, u64)>> {
        self.subnodes_at(bid, 0)
    }

    fn subnodes_at(&mut self, bid: u64, depth: usize) -> R<HashMap<u64, (u64, u64)>> {
        let mut res = HashMap::new();
        if bid == 0 {
            return Ok(res);
        }
        chk(depth < 2, BROKEN)?; // SIBLOCK -> SLBLOCK, не глубже
        let data = self.block(bid)?;
        let (btype, level, cent) = (
            data.first().copied().unwrap_or(0),
            data.get(1).copied().unwrap_or(0),
            u16_at(&data, 2)? as usize,
        );
        chk(btype == 2, "ожидался SLBLOCK/SIBLOCK")?;
        for i in 0..cent {
            if level == 0 {
                let o = 8 + 24 * i;
                res.insert(
                    u64_at(&data, o)?,
                    (u64_at(&data, o + 8)?, u64_at(&data, o + 16)?),
                );
            } else {
                let o = 8 + 16 * i;
                let b = u64_at(&data, o + 8)?;
                res.extend(self.subnodes_at(b, depth + 1)?);
            }
        }
        Ok(res)
    }

    // ---------- LTP ----------
    fn hn(&mut self, bid_data: u64, bid_sub: u64) -> R<Hn> {
        let pages = self.chunks(bid_data)?;
        let subs = self.subnodes(bid_sub)?;
        chk(
            !pages.is_empty() && pages[0].len() > 3 && pages[0][2] == 0xEC,
            "нет сигнатуры heap 0xEC",
        )?;
        let client = pages[0][3];
        let root = u32_at(&pages[0], 4)?;
        let mut maps = Vec::with_capacity(pages.len());
        for p in &pages {
            let ibm = u16_at(p, 0)? as usize;
            let calloc = u16_at(p, ibm)? as usize;
            let mut m = Vec::with_capacity(calloc + 1);
            for j in 0..=calloc {
                m.push(u16_at(p, ibm + 4 + 2 * j)? as usize);
            }
            maps.push(m);
        }
        Ok(Hn {
            pages,
            subs,
            client,
            root,
            maps,
        })
    }

    fn hn_get(&mut self, hn: &Hn, hid: u32) -> R<Vec<u8>> {
        if hid == 0 {
            return Ok(vec![]);
        }
        if hid & 0x1F != 0 {
            let Some(&(d, _)) = hn.subs.get(&(hid as u64)) else {
                return chk_a(
                    false,
                    "подузел {hid:#x} не найден",
                    &[("hid", hx(hid as u64))],
                )
                .map(|_| vec![]);
            };
            return Ok(self.chunks(d)?.concat());
        }
        let (idx, blk) = (((hid >> 5) & 0x7FF) as usize, (hid >> 16) as usize);
        let m = hn.maps.get(blk).ok_or(CheckError {
            template: "HID {hid:#x} вне карты heap",
            args: vec![("hid", hx(hid as u64))],
        })?;
        chk_a(
            1 <= idx && idx < m.len(),
            "HID {hid:#x} вне карты heap",
            &[("hid", hx(hid as u64))],
        )?;
        let p = &hn.pages[blk];
        p.get(m[idx - 1]..m[idx])
            .map(|s| s.to_vec())
            .ok_or(CheckError {
                template: "данные обрезаны",
                args: vec![],
            })
    }

    fn bth(&mut self, hn: &Hn, hid: u32) -> R<Vec<(u64, Vec<u8>)>> {
        let h = self.hn_get(hn, hid)?;
        chk(h.len() == 8 && h[0] == 0xB5, "нет сигнатуры BTH")?;
        let (ck, ce, lv, root) = (h[1] as usize, h[2] as usize, h[3], u32_at(&h, 4)?);
        let mut out = Vec::new();
        if root != 0 {
            self.bth_walk(hn, root, lv, ck, ce, &mut out)?;
        }
        chk(
            out.windows(2).all(|w| w[0].0 <= w[1].0),
            "BTH не отсортирован",
        )?;
        Ok(out)
    }

    fn bth_walk(
        &mut self,
        hn: &Hn,
        hid: u32,
        lvl: u8,
        ck: usize,
        ce: usize,
        out: &mut Vec<(u64, Vec<u8>)>,
    ) -> R<()> {
        let d = self.hn_get(hn, hid)?;
        let step = ck + if lvl == 0 { ce } else { 4 };
        chk(step > 0 && d.len() % step == 0, "размер узла BTH")?;
        for i in (0..d.len()).step_by(step) {
            let mut kb = [0u8; 8];
            kb[..ck.min(8)].copy_from_slice(&d[i..i + ck.min(8)]);
            let k = u64::from_le_bytes(kb);
            if lvl != 0 {
                self.bth_walk(hn, u32_at(&d, i + ck)?, lvl - 1, ck, ce, out)?;
            } else {
                out.push((k, d[i + ck..i + step].to_vec()));
            }
        }
        Ok(())
    }

    fn pc(&mut self, bid_data: u64, bid_sub: u64) -> R<PropMap> {
        let hn = self.hn(bid_data, bid_sub)?;
        chk(hn.client == 0xBC, "ожидался Property Context")?;
        let mut props = PropMap::new();
        for (pid, rec) in self.bth(&hn, hn.root)? {
            let (ptype, dw) = (u16_at(&rec, 0)?, u32_at(&rec, 2)?);
            let tag = ((pid as u32) << 16) | ptype as u32;
            let v = if matches!(ptype, 0x0003 | 0x0002 | 0x000B) {
                PVal::Int(dw as u64)
            } else {
                let data = self.hn_get(&hn, dw)?;
                if ptype == 0x1F {
                    PVal::Str(utf16(&data)?)
                } else {
                    PVal::Bin
                }
            };
            props.insert(tag, v);
        }
        Ok(props)
    }

    fn tc(&mut self, bid_data: u64, bid_sub: u64) -> R<Vec<(u64, PropMap)>> {
        let hn = self.hn(bid_data, bid_sub)?;
        chk(hn.client == 0x7C, "ожидался Table Context")?;
        let info = self.hn_get(&hn, hn.root)?;
        let ncol = *info.get(1).ok_or(CheckError {
            template: "данные обрезаны",
            args: vec![],
        })? as usize;
        let t1 = u16_at(&info, 6)? as usize;
        let tbm = u16_at(&info, 8)? as usize;
        let hid_ri = u32_at(&info, 10)?;
        let hnid_rows = u32_at(&info, 14)?;
        let mut cols = Vec::with_capacity(ncol);
        for i in 0..ncol {
            let o = 22 + 8 * i;
            let cb_bit = u16_at(&info, o + 6)?.to_le_bytes(); // cbData, iBit
            let (tag, ib, cb, bit) = (
                u32_at(&info, o)?,
                u16_at(&info, o + 4)? as usize,
                cb_bit[0] as usize,
                cb_bit[1] as usize,
            );
            if tag == 0x67F20003 {
                chk(
                    ib == 0 && cb == 4 && bit == 0,
                    "COLDESC для RowID должен быть ib=0, iBit=0",
                )?;
            }
            if tag == 0x67F30003 {
                chk(
                    ib == 4 && cb == 4 && bit == 1,
                    "COLDESC для RowVer должен быть ib=4, iBit=1",
                )?;
            }
            cols.push((tag, ib, cb, bit));
        }
        let index = self.bth(&hn, hid_ri)?;
        let mut rows = Vec::with_capacity(index.len());
        if hnid_rows != 0 {
            let blocks = if hnid_rows & 0x1F != 0 {
                let &(d, _) = hn.subs.get(&(hnid_rows as u64)).ok_or(CheckError {
                    template: "подузел {hid:#x} не найден",
                    args: vec![("hid", hx(hnid_rows as u64))],
                })?;
                self.chunks(d)?
            } else {
                vec![self.hn_get(&hn, hnid_rows)?]
            };
            chk(tbm > 0 && tbm <= 8176, "строка таблицы обрезана")?;
            let per = 8176 / tbm;
            for (rid, ridx) in index {
                let n = u32_at(&ridx, 0)? as usize;
                let blk = blocks.get(n / per).ok_or(CheckError {
                    template: "строка таблицы обрезана",
                    args: vec![],
                })?;
                let row = blk.get((n % per) * tbm..(n % per + 1) * tbm).unwrap_or(&[]);
                chk(row.len() == tbm, "строка таблицы обрезана")?;
                chk(
                    u32_at(row, 0)? as u64 == rid,
                    "RowID не совпадает с индексом",
                )?;
                let mut vals = PropMap::new();
                for &(tag, ib, cb, bit) in &cols {
                    let flag = *row.get(t1 + bit / 8).ok_or(CheckError {
                        template: "строка таблицы обрезана",
                        args: vec![],
                    })?;
                    if flag & (0x80 >> (bit % 8)) == 0 {
                        continue;
                    }
                    let cell = row.get(ib..ib + cb).ok_or(CheckError {
                        template: "строка таблицы обрезана",
                        args: vec![],
                    })?;
                    let pt = tag & 0xFFFF;
                    let v = if pt == 0x1F || pt == 0x102 {
                        let d = self.hn_get(&hn, u32_at(cell, 0)?)?;
                        if pt == 0x1F {
                            PVal::Str(utf16(&d)?)
                        } else {
                            PVal::Bin
                        }
                    } else {
                        let mut b = [0u8; 8];
                        b[..cb.min(8)].copy_from_slice(&cell[..cb.min(8)]);
                        PVal::Int(u64::from_le_bytes(b))
                    };
                    vals.insert(tag, v);
                }
                rows.push((rid, vals));
            }
        }
        Ok(rows)
    }

    fn node(&self, nid: u64) -> R<(u64, u64, u32)> {
        self.nbt.get(&nid).copied().ok_or(CheckError {
            template: "узел {nid:#x} отсутствует в NBT",
            args: vec![("nid", hx(nid))],
        })
    }

    fn node_pc(&mut self, nid: u64) -> R<PropMap> {
        let (d, s, _) = self.node(nid)?;
        self.pc(d, s)
    }

    fn node_tc(&mut self, nid: u64) -> R<Vec<(u64, PropMap)>> {
        let (d, s, _) = self.node(nid)?;
        self.tc(d, s)
    }
}

struct Hn {
    pages: Vec<Vec<u8>>,
    subs: HashMap<u64, (u64, u64)>,
    client: u8,
    root: u32,
    maps: Vec<Vec<usize>>,
}

fn utf16(d: &[u8]) -> R<String> {
    chk(d.len().is_multiple_of(2), "строка не в UTF-16")?;
    let u: Vec<u16> = d
        .chunks(2)
        .map(|c| u16::from_le_bytes([c[0], *c.get(1).unwrap_or(&0)]))
        .collect();
    String::from_utf16(&u).map_err(|_| CheckError {
        template: "строка не в UTF-16",
        args: vec![],
    })
}

pub enum Summary {
    Str(String),
    Bool(bool),
    Count(usize),
}

fn as_str(v: Option<&PVal>, default: &str) -> String {
    match v {
        Some(PVal::Str(s)) => s.clone(),
        _ => default.to_string(),
    }
}

/// Полная проверка файла. Возвращает сводку: [(ключ, значение)] в порядке Python-версии.
pub fn verify(path: &str) -> Result<Vec<(String, Summary)>, CheckError> {
    let mut r = Reader::open(path).map_err(|_| CheckError {
        template: "данные обрезаны",
        args: vec![],
    })?;
    r.init()?;
    for nid in r.nbt_order.clone() {
        chk_a(
            (nid >> 5) < r.rgnid[(nid & 0x1F) as usize] as u64,
            "счётчик NID в заголовке меньше узла {nid:#x}",
            &[("nid", hx(nid))],
        )?;
    }
    let store = r.node_pc(0x21)?;
    chk(store.contains_key(&0x67FF0003), "нет PR_PST_PASSWORD")?;
    chk(
        r.node_pc(0x122)?.contains_key(&0x3001001F),
        "у корневой папки нет имени",
    )?;
    chk(
        store.contains_key(&0x35E00102),
        "нет IPM Subtree в хранилище",
    )?;
    let pw = matches!(store.get(&0x67FF0003), Some(PVal::Int(v)) if *v != 0);
    let mut summary = vec![
        (
            "_store".to_string(),
            Summary::Str(as_str(store.get(&0x3001001F), "")),
        ),
        ("_password".to_string(), Summary::Bool(pw)),
    ];
    let mut seen = HashSet::from([0x122]);
    walk(&mut r, 0x122, String::new(), &mut summary, &mut seen, 0)?;
    Ok(summary)
}

fn walk(
    r: &mut Reader,
    nid: u64,
    path: String,
    summary: &mut Vec<(String, Summary)>,
    seen: &mut HashSet<u64>,
    depth: usize,
) -> R<()> {
    chk(depth < MAX_FOLDER_DEPTH, BROKEN)?;
    for (child, _) in r.node_tc((nid & !0x1F) | 0x0D)? {
        chk(seen.insert(child), BROKEN)?; // папка встречается дважды — цикл
        let pc = r.node_pc(child)?;
        let name = as_str(pc.get(&0x3001001F), "?");
        let rows = r.node_tc((child & !0x1F) | 0x0E)?;
        let want = match pc.get(&0x36020003) {
            Some(PVal::Int(v)) => *v as usize,
            _ => 0,
        };
        chk_a(
            rows.len() == want,
            "счётчик писем папки «{name}»",
            &[("name", name.clone())],
        )?;
        for (mid, _) in &rows {
            let (d, s, par) = r.node(*mid)?;
            chk(par as u64 == child, "родитель письма")?;
            let m = r.pc(d, s)?;
            let subs = r.subnodes(s)?;
            if let Some(&(d2, s2)) = subs.get(&0x692) {
                r.tc(d2, s2)?;
            }
            if let Some(&(d2, s2)) = subs.get(&0x671) {
                for (anid, _) in r.tc(d2, s2)? {
                    let &(ad, asub) = subs.get(&anid).ok_or(CheckError {
                        template: "подузел {hid:#x} не найден",
                        args: vec![("hid", hx(anid))],
                    })?;
                    let a = r.pc(ad, asub)?;
                    chk(a.contains_key(&0x37010102), "у вложения нет данных")?;
                }
            }
            chk(m.contains_key(&0x001A001F), "нет класса письма")?;
        }
        let full = format!("{path}{name}");
        summary.push((full.clone(), Summary::Count(rows.len())));
        walk(r, child, format!("{full}/"), summary, seen, depth + 1)?;
    }
    Ok(())
}
