//! Разбор .eml и сборка письма для PST — порт mailparse.py + pst_export.eml_to_message.
//!
//! Структура MIME разбирается так же, как email.feedparser в Python (строки,
//! стек границ, обрезка перевода строки перед границей), чтобы тексты,
//! имена и данные вложений совпадали с Python-версией.

use std::fs;
use std::io::{self, Read};
use std::path::Path;
use std::sync::LazyLock;
use std::time::UNIX_EPOCH;

use regex::Regex;

use crate::addr;
use crate::date;
use crate::pst::{Message, Props, Val, filetime_f, filetime_i};
use crate::text::{self, Codec, decode_bytes, decode_hdr_raw, decode_hdr_str, py_strip};

#[derive(Debug)]
pub enum MailError {
    Io(io::Error),
}

impl From<io::Error> for MailError {
    fn from(e: io::Error) -> Self {
        MailError::Io(e)
    }
}

pub fn uuid4() -> [u8; 16] {
    let mut b = [0u8; 16];
    getrandom::fill(&mut b).expect("random");
    b[6] = (b[6] & 0x0F) | 0x40;
    b[8] = (b[8] & 0x3F) | 0x80;
    b
}

fn urandom16() -> Vec<u8> {
    let mut b = vec![0u8; 16];
    getrandom::fill(&mut b).expect("random");
    b
}

// ================================================================ строки и заголовки

/// Разбить на строки с окончаниями (\r\n, \r или \n), как NLCRE_crack.
fn split_lines(d: &[u8]) -> Vec<(usize, usize)> {
    let mut out = Vec::with_capacity(d.len() / 64 + 1);
    let mut start = 0;
    for i in memchr::memchr2_iter(b'\r', b'\n', d) {
        if i < start {
            continue; // \n из уже учтённого \r\n
        }
        let end = if d[i] == b'\r' && d.get(i + 1) == Some(&b'\n') {
            i + 2
        } else {
            i + 1
        };
        out.push((start, end));
        start = end;
    }
    if start < d.len() {
        out.push((start, d.len()));
    }
    out
}

fn eol_len(line: &[u8]) -> usize {
    if line.ends_with(b"\r\n") {
        2
    } else if line.ends_with(b"\r") || line.ends_with(b"\n") {
        1
    } else {
        0
    }
}

/// headerRE: ^(From |[\041-\071\073-\176]*:|[\t ])
fn is_header_line(l: &[u8]) -> bool {
    if l.starts_with(b"From ") || l.first().is_some_and(|c| *c == b' ' || *c == b'\t') {
        return true;
    }
    for &c in l {
        if c == b':' {
            return true;
        }
        if !(0x21..=0x7E).contains(&c) {
            return false;
        }
    }
    false
}

fn is_blank(l: &[u8]) -> bool {
    l.first().is_some_and(|c| *c == b'\r' || *c == b'\n')
}

#[derive(Default, Clone)]
pub struct Headers {
    list: Vec<(Vec<u8>, Vec<u8>)>, // (имя, значение) — значение без начальных пробелов и конечного \r\n
}

impl Headers {
    /// Первое значение заголовка (без учёта регистра имени).
    pub fn get(&self, name: &str) -> Option<&[u8]> {
        self.list
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name.as_bytes()))
            .map(|(_, v)| v.as_slice())
    }

    /// Значение без переводов строк (policy.default разворачивает заголовки так).
    fn unfolded(&self, name: &str) -> Option<Vec<u8>> {
        self.get(name).map(|v| {
            v.iter()
                .copied()
                .filter(|c| *c != b'\r' && *c != b'\n')
                .collect()
        })
    }
}

/// feedparser._parse_headers над готовым списком строк.
fn parse_header_lines(d: &[u8], lines: &[(usize, usize)]) -> Headers {
    let mut h = Headers::default();
    let mut cur: Option<(Vec<u8>, Vec<u8>)> = None;
    let flush = |cur: &mut Option<(Vec<u8>, Vec<u8>)>, h: &mut Headers| {
        if let Some((n, mut v)) = cur.take() {
            while v.last().is_some_and(|c| *c == b'\r' || *c == b'\n') {
                v.pop();
            }
            h.list.push((n, v));
        }
    };
    for (lineno, &(s, e)) in lines.iter().enumerate() {
        let line = &d[s..e];
        if line[0] == b' ' || line[0] == b'\t' {
            if let Some((_, v)) = cur.as_mut() {
                v.extend_from_slice(line);
            }
            continue;
        }
        flush(&mut cur, &mut h);
        if line.starts_with(b"From ") {
            let _ = lineno; // конверт «From » — пропускаем
            continue;
        }
        let Some(i) = line.iter().position(|c| *c == b':') else {
            continue;
        };
        if i == 0 {
            continue;
        }
        let name = line[..i].to_vec();
        let mut v = &line[i + 1..];
        while v.first().is_some_and(|c| *c == b' ' || *c == b'\t') {
            v = &v[1..];
        }
        cur = Some((name, v.to_vec()));
    }
    flush(&mut cur, &mut h);
    h
}

// ================================================================ параметры заголовков

/// utils.unquote
fn unquote(s: &str) -> String {
    if s.chars().count() > 1 {
        if s.starts_with('"') && s.ends_with('"') {
            return s[1..s.len() - 1]
                .replace("\\\\", "\\")
                .replace("\\\"", "\"");
        }
        if s.starts_with('<') && s.ends_with('>') {
            return s[1..s.len() - 1].to_string();
        }
    }
    s.to_string()
}

/// message._parseparam
fn parseparam(s: &str) -> Vec<String> {
    let mut s = format!(";{s}");
    let mut plist = Vec::new();
    while s.starts_with(';') {
        s = s[1..].to_string();
        let count_q = |t: &str| t.matches('"').count() as i64 - t.matches("\\\"").count() as i64;
        let mut end = s.find(';').map(|e| e as i64).unwrap_or(-1);
        while end > 0 && count_q(&s[..end as usize]) % 2 != 0 {
            end = s[end as usize + 1..]
                .find(';')
                .map(|e| e as i64 + end + 1)
                .unwrap_or(-1);
        }
        let end = if end < 0 { s.len() } else { end as usize };
        let mut f = s[..end].to_string();
        if let Some(i) = f.find('=') {
            f = format!(
                "{}={}",
                py_strip(&f[..i]).to_lowercase(),
                py_strip(&f[i + 1..])
            );
        }
        plist.push(py_strip(&f).to_string());
        s = s[end..].to_string();
    }
    plist
}

#[derive(Debug, Clone)]
enum Param {
    Plain(String),
    Rfc2231 {
        charset: Option<String>,
        text: String,
    },
}

static RFC2231_CONT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?P<name>[A-Za-z0-9_]+)\*((?P<num>[0-9]+)\*?)?$").unwrap());

fn pct_unquote_latin1(s: &str) -> String {
    // urllib.parse.unquote(s, encoding="latin-1"): %XX -> символ U+00XX
    let b = s.as_bytes();
    let mut out = String::new();
    let mut i = 0;
    let hex = |c: u8| (c as char).to_digit(16);
    while i < b.len() {
        if b[i] == b'%'
            && let (Some(a), Some(c)) = (
                b.get(i + 1).and_then(|&x| hex(x)),
                b.get(i + 2).and_then(|&x| hex(x)),
            )
        {
            out.push(char::from_u32(a * 16 + c).unwrap());
            i += 3;
            continue;
        }
        let ch = s[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

/// _get_params_preserve + utils.decode_params: [(имя, значение)].
fn get_params(value: &str) -> Vec<(String, Param)> {
    let mut params: Vec<(String, String)> = Vec::new();
    for p in parseparam(value) {
        match p.split_once('=') {
            Some((n, v)) => params.push((py_strip(n).to_string(), py_strip(v).to_string())),
            None => params.push((py_strip(&p).to_string(), String::new())),
        }
    }
    let mut out: Vec<(String, Param)> = Vec::new();
    let Some(first) = params.first() else {
        return out;
    };
    out.push((first.0.clone(), Param::Plain(unquote(&first.1))));
    let mut rfc: Vec<(String, Vec<(Option<u32>, String, bool)>)> = Vec::new();
    for (name, value) in &params[1..] {
        let encoded = name.ends_with('*');
        let value = unquote(value);
        if let Some(c) = RFC2231_CONT.captures(name) {
            let n = c["name"].to_string();
            let num = c.name("num").and_then(|m| m.as_str().parse().ok());
            match rfc.iter_mut().find(|(k, _)| *k == n) {
                Some((_, v)) => v.push((num, value, encoded)),
                None => rfc.push((n, vec![(num, value, encoded)])),
            }
        } else {
            out.push((name.clone(), Param::Plain(value)));
        }
    }
    for (name, mut conts) in rfc {
        conts.sort_by_key(|c| c.0.unwrap_or(0));
        let mut value = String::new();
        let mut extended = false;
        for (_, s, enc) in conts {
            if enc {
                value.push_str(&pct_unquote_latin1(&s));
                extended = true;
            } else {
                value.push_str(&s);
            }
        }
        if extended {
            let parts: Vec<&str> = value.splitn(3, '\'').collect();
            if parts.len() <= 2 {
                out.push((
                    name,
                    Param::Rfc2231 {
                        charset: None,
                        text: value,
                    },
                ));
            } else {
                out.push((
                    name,
                    Param::Rfc2231 {
                        charset: Some(parts[0].to_string()),
                        text: parts[2].to_string(),
                    },
                ));
            }
        } else {
            out.push((name, Param::Plain(value)));
        }
    }
    out
}

fn get_param(h: &Headers, header: &str, param: &str) -> Option<Param> {
    let raw = h.unfolded(header)?;
    // байты как символы U+0000..U+00FF: 8-битные имена файлов декодируются позже цепочкой кодировок
    let value: String = raw.iter().map(|&b| b as char).collect();
    get_params(&value)
        .into_iter()
        .skip(1)
        .find(|(k, _)| k.eq_ignore_ascii_case(param))
        .map(|(_, v)| v)
}

/// utils.collapse_rfc2231_value
fn collapse_param(p: Param) -> String {
    match p {
        Param::Plain(s) => s,
        Param::Rfc2231 { charset, text } => {
            let raw: Vec<u8> = text
                .chars()
                .map(|c| c as u32)
                .map(|u| if u < 256 { u as u8 } else { b'?' })
                .collect();
            let cs = charset.unwrap_or_else(|| "us-ascii".into());
            match text::lookup(&cs) {
                Some(c) if !cs.is_empty() => text::decode_replace(c, &raw),
                _ => unquote(&text),
            }
        }
    }
}

// ================================================================ MIME-дерево (как feedparser)

pub struct Part {
    pub headers: Headers,
    default_type: &'static str,
    pub payload: Payload,
    /// Границы исходного текста части (заголовки + тело) в данных письма.
    span: (usize, usize),
}

pub enum Payload {
    None,
    Text(Vec<u8>),
    Parts(Vec<Part>),
}

impl Part {
    pub fn content_type(&self) -> String {
        match self.headers.unfolded("content-type") {
            None => self.default_type.to_string(),
            Some(v) => {
                let v = String::from_utf8_lossy(&v);
                let main = v.split(';').next().unwrap_or("");
                let ct = py_strip(main).to_lowercase();
                if ct.matches('/').count() != 1 {
                    "text/plain".into()
                } else {
                    ct
                }
            }
        }
    }

    fn maintype(&self) -> String {
        self.content_type()
            .split('/')
            .next()
            .unwrap_or("")
            .to_string()
    }

    fn boundary(&self) -> Option<String> {
        let p = get_param(&self.headers, "content-type", "boundary")?;
        Some(
            collapse_param(p)
                .trim_end_matches(text::py_space)
                .to_string(),
        )
    }

    fn content_disposition(&self) -> Option<String> {
        let v = self.headers.unfolded("content-disposition")?;
        let v = String::from_utf8_lossy(&v);
        Some(py_strip(v.split(';').next().unwrap_or("")).to_lowercase())
    }

    /// get_filename() + decode_hdr()
    fn filename(&self) -> String {
        let p = get_param(&self.headers, "content-disposition", "filename")
            .or_else(|| get_param(&self.headers, "content-type", "name"));
        let Some(p) = p else { return String::new() };
        let rfc = matches!(p, Param::Rfc2231 { .. });
        let s = collapse_param(p);
        let s = py_strip(&s).to_string();
        if s.is_empty() {
            return s;
        }
        if rfc {
            decode_hdr_str(&s)
        } else {
            // параметр без RFC 2231: исходные байты (с 8-битными символами — цепочкой кодировок)
            let raw: Vec<u8> = s
                .chars()
                .map(|c| c as u32)
                .map(|u| if u < 256 { u as u8 } else { b'?' })
                .collect();
            decode_hdr_raw(&raw)
        }
    }

    fn cte(&self) -> String {
        self.headers
            .unfolded("content-transfer-encoding")
            .map(|v| py_strip(&String::from_utf8_lossy(&v)).to_lowercase())
            .unwrap_or_default()
    }

    /// get_payload(decode=True)
    fn decoded_payload(&self) -> Option<Vec<u8>> {
        let Payload::Text(raw) = &self.payload else {
            return None;
        };
        Some(match self.cte().as_str() {
            "quoted-printable" => text::a2b_qp(raw),
            "base64" => decode_b(raw),
            _ => raw.clone(),
        })
    }

    fn charset_param(&self) -> Option<Param> {
        get_param(&self.headers, "content-type", "charset")
    }
}

/// _encoded_words.decode_b для тела (строки склеены без переводов строк).
fn decode_b(raw: &[u8]) -> Vec<u8> {
    let mut joined = Vec::with_capacity(raw.len());
    let mut start = 0;
    for i in memchr::memchr2_iter(b'\r', b'\n', raw) {
        joined.extend_from_slice(&raw[start..i]);
        start = i + 1;
    }
    joined.extend_from_slice(&raw[start..]);
    let pad = joined.len() % 4;
    if pad == 0 {
        if let Some(v) = strict_b64(&joined) {
            return v;
        }
    } else {
        let mut t = joined.clone();
        t.extend_from_slice(&b"==="[..4 - pad]);
        if let Some(v) = strict_b64(&t) {
            return v;
        }
    }
    if let Some(v) = text::a2b_base64(&joined) {
        return v;
    }
    let mut t2 = joined.clone();
    t2.extend_from_slice(b"==");
    if let Some(v) = text::a2b_base64(&t2) {
        return v;
    }
    // как Python: «неисправимо» — отдаём исходные байты
    raw.to_vec()
}

/// base64.b64decode(validate=True): только алфавит и не больше двух «=» в конце.
fn strict_b64(t: &[u8]) -> Option<Vec<u8>> {
    let body_len = t.len() - t.iter().rev().take_while(|c| **c == b'=').count();
    if t.len() - body_len > 2
        || !t[..body_len]
            .iter()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'/'))
    {
        return None;
    }
    text::a2b_base64(t)
}

#[derive(Clone)]
enum Eof {
    Blank,
    Boundary(Vec<u8>),
}

fn boundary_match(line: &[u8], sep: &[u8]) -> Option<bool> {
    let rest = line.strip_prefix(sep)?;
    let (end, mut rest) = match rest.strip_prefix(b"--") {
        Some(r) => (true, r),
        None => (false, rest),
    };
    while rest.first().is_some_and(|c| *c == b' ' || *c == b'\t') {
        rest = &rest[1..];
    }
    matches!(rest, b"" | b"\r\n" | b"\r" | b"\n").then_some(end)
}

struct Input<'a> {
    d: &'a [u8],
    lines: Vec<(usize, usize)>,
    pos: usize,
    eof: Vec<Eof>,
    /// Глубина вложенности частей: дальше MAX_DEPTH не разбираем (иначе переполнение стека).
    depth: usize,
}

const MAX_DEPTH: usize = 100;

impl<'a> Input<'a> {
    fn at_eof_matcher(&self, l: &[u8]) -> bool {
        self.eof.iter().rev().any(|m| match m {
            Eof::Blank => is_blank(l),
            Eof::Boundary(sep) => boundary_match(l, sep).is_some(),
        })
    }

    /// Следующая строка или None (конец файла или сработала «граница»).
    fn readline(&mut self) -> Option<usize> {
        if self.pos >= self.lines.len() {
            return None;
        }
        let (s, e) = self.lines[self.pos];
        if self.at_eof_matcher(&self.d[s..e]) {
            return None;
        }
        self.pos += 1;
        Some(self.pos - 1)
    }

    fn line(&self, i: usize) -> &'a [u8] {
        let (s, e) = self.lines[i];
        &self.d[s..e]
    }

    fn offset(&self) -> usize {
        self.lines
            .get(self.pos)
            .map(|l| l.0)
            .unwrap_or(self.d.len())
    }
}

fn join(inp: &Input, idx: &[usize]) -> Vec<u8> {
    match (idx.first(), idx.last()) {
        (Some(&a), Some(&b)) => inp.d[inp.lines[a].0..inp.lines[b].1].to_vec(),
        _ => Vec::new(),
    }
}

/// feedparser._parsegen для одного сообщения.
fn parse_part(inp: &mut Input, default_type: &'static str) -> Part {
    let start = inp.offset();
    // заголовки
    let mut hlines = Vec::new();
    while let Some(i) = inp.readline() {
        let l = inp.line(i);
        if !is_header_line(l) {
            if !is_blank(l) {
                inp.pos -= 1; // строка — уже тело
            }
            break;
        }
        hlines.push(inp.lines[i]);
    }
    // «From » на последней строке заголовков в Python уходит в тело — здесь просто отбрасываем
    if hlines.len() > 1 {
        let (s, e) = *hlines.last().unwrap();
        if inp.d[s..e].starts_with(b"From ") {
            hlines.pop();
        }
    }
    let headers = parse_header_lines(inp.d, &hlines);
    let mut part = Part {
        headers,
        default_type,
        payload: Payload::None,
        span: (start, start),
    };
    let ctype = part.content_type();
    let nested = inp.depth < MAX_DEPTH;
    inp.depth += 1;

    if !nested {
        // слишком глубоко: остаток — просто текст
        let mut idx = Vec::new();
        while let Some(i) = inp.readline() {
            idx.push(i);
        }
        part.payload = Payload::Text(join(inp, &idx));
    } else if ctype == "message/delivery-status" {
        let mut blocks = Vec::new();
        loop {
            inp.eof.push(Eof::Blank);
            let b = parse_part(inp, "text/plain");
            inp.eof.pop();
            blocks.push(b);
            // съесть пустую строку после блока и проверить, не конец ли
            inp.readline();
            match inp.readline() {
                None => break,
                Some(_) => inp.pos -= 1,
            }
        }
        part.payload = Payload::Parts(blocks);
    } else if part.maintype() == "message" {
        let inner = parse_part(inp, "text/plain");
        part.payload = Payload::Parts(vec![inner]);
    } else if part.maintype() == "multipart" {
        match part.boundary() {
            None => {
                let mut idx = Vec::new();
                while let Some(i) = inp.readline() {
                    idx.push(i);
                }
                part.payload = Payload::Text(join(inp, &idx));
            }
            Some(b) => {
                let sep = [b"--".as_slice(), b.as_bytes()].concat();
                let child_default = if ctype == "multipart/digest" {
                    "message/rfc822"
                } else {
                    "text/plain"
                };
                let mut preamble = Vec::new();
                let mut capturing = true;
                let mut parts: Vec<Part> = Vec::new();
                while let Some(i) = inp.readline() {
                    let l = inp.line(i);
                    match boundary_match(l, &sep) {
                        Some(true) => break, // закрывающая граница
                        Some(false) => {
                            if capturing {
                                capturing = false;
                                inp.pos -= 1;
                                continue;
                            }
                            // несколько границ подряд
                            while let Some(j) = inp.readline() {
                                if boundary_match(inp.line(j), &sep).is_none() {
                                    inp.pos -= 1;
                                    break;
                                }
                            }
                            inp.eof.push(Eof::Boundary(sep.clone()));
                            let mut sub = parse_part(inp, child_default);
                            inp.eof.pop();
                            // перевод строки перед границей принадлежит границе
                            strip_last(&mut sub);
                            parts.push(sub);
                        }
                        None => {
                            if capturing {
                                preamble.push(i);
                            }
                        }
                    }
                }
                if capturing {
                    part.payload = Payload::Text(join(inp, &preamble));
                } else {
                    part.payload = Payload::Parts(parts);
                    // эпилог до конца (или до внешней границы) пропускаем
                    while inp.readline().is_some() {}
                }
            }
        }
    } else {
        let mut idx = Vec::new();
        while let Some(i) = inp.readline() {
            idx.push(i);
        }
        part.payload = Payload::Text(join(inp, &idx));
    }
    inp.depth -= 1;
    part.span = (start, inp.offset());
    part
}

/// Python обрезает перевод строки у последнего созданного сообщения подчасти
/// (feedparser: self._last), если это не multipart.
fn strip_last(p: &mut Part) {
    let main = p.maintype();
    match &mut p.payload {
        Payload::Parts(v) if main == "message" => {
            if let Some(last) = v.last_mut() {
                strip_last(last);
            }
        }
        Payload::Text(t) if main != "multipart" => {
            let n = eol_len(t);
            t.truncate(t.len() - n);
        }
        _ => {}
    }
}

/// Универсальные переводы строк (\r\n и \r -> \n): так читает BytesParser.parse(файл).
fn universal_newlines(d: &[u8]) -> std::borrow::Cow<'_, [u8]> {
    if memchr::memchr(b'\r', d).is_none() {
        return std::borrow::Cow::Borrowed(d);
    }
    let mut out = Vec::with_capacity(d.len());
    let mut start = 0;
    for i in memchr::memchr_iter(b'\r', d) {
        out.extend_from_slice(&d[start..i]);
        out.push(b'\n');
        start = if d.get(i + 1) == Some(&b'\n') {
            i + 2
        } else {
            i + 1
        };
    }
    out.extend_from_slice(&d[start..]);
    std::borrow::Cow::Owned(out)
}

pub fn parse_message(d: &[u8]) -> Part {
    let mut inp = Input {
        d,
        lines: split_lines(d),
        pos: 0,
        eof: Vec::new(),
        depth: 0,
    };
    parse_part(&mut inp, "text/plain")
}

// ================================================================ mailparse

/// Листовые части; вложенное письмо (message/rfc822) — одна часть.
fn walk<'a>(p: &'a Part, out: &mut Vec<&'a Part>) {
    if p.content_type() == "message/rfc822" {
        out.push(p);
        return;
    }
    if let Payload::Parts(subs) = &p.payload {
        for s in subs {
            walk(s, out);
        }
    } else {
        out.push(p);
    }
}

#[derive(PartialEq, Clone, Copy)]
enum Kind {
    Body,
    Inline,
    Attachment,
}

struct PartInfo<'a> {
    part: &'a Part,
    ctype: String,
    fname: String,
    cid: String,
    kind: Kind,
}

const INVALID: &str = "<>:\"/\\|?*";

fn iter_parts<'a>(msg: &'a Part, forwarded: &str) -> Vec<PartInfo<'a>> {
    let mut leaves = Vec::new();
    walk(msg, &mut leaves);
    let (mut seen_html, mut seen_text) = (false, false);
    let mut out = Vec::new();
    for part in leaves {
        let ctype = part.content_type();
        let disp = part.content_disposition();
        let mut fname = part.filename();
        let cid = part
            .headers
            .unfolded("content-id")
            .map(|v| {
                py_strip(&String::from_utf8_lossy(&v))
                    .trim_matches(|c| c == '<' || c == '>')
                    .to_string()
            })
            .unwrap_or_default();
        if ctype == "message/rfc822" {
            if fname.is_empty() {
                let subj = match &part.payload {
                    Payload::Parts(inner) if !inner.is_empty() => inner[0]
                        .headers
                        .get("subject")
                        .map(decode_hdr_raw)
                        .unwrap_or_default(),
                    _ => String::new(),
                };
                let safe: String = subj
                    .chars()
                    .map(|c| {
                        if INVALID.contains(c) || (c as u32) < 32 {
                            '_'
                        } else {
                            c
                        }
                    })
                    .collect();
                let safe: String = safe
                    .trim_matches(|c| c == ' ' || c == '.')
                    .chars()
                    .take(80)
                    .collect();
                fname = (if safe.is_empty() {
                    forwarded.to_string()
                } else {
                    safe
                }) + ".eml";
            }
            out.push(PartInfo {
                part,
                ctype,
                fname,
                cid,
                kind: Kind::Attachment,
            });
            continue;
        }
        if disp.as_deref() != Some("attachment") && fname.is_empty() {
            if ctype == "text/html" && !seen_html {
                seen_html = true;
                out.push(PartInfo {
                    part,
                    ctype,
                    fname,
                    cid,
                    kind: Kind::Body,
                });
                continue;
            }
            if ctype == "text/plain" && !seen_text {
                seen_text = true;
                out.push(PartInfo {
                    part,
                    ctype,
                    fname,
                    cid,
                    kind: Kind::Body,
                });
                continue;
            }
        }
        let inline =
            !cid.is_empty() && disp.as_deref() != Some("attachment") && ctype.starts_with("image/");
        out.push(PartInfo {
            part,
            ctype,
            fname,
            cid,
            kind: if inline {
                Kind::Inline
            } else {
                Kind::Attachment
            },
        });
    }
    out
}

/// mailparse._part_text
fn part_text(p: &Part) -> String {
    let payload = p.decoded_payload().unwrap_or_default();
    // get_content_charset()
    let cs = p.charset_param().map(|cp| match cp {
        Param::Plain(s) => s,
        Param::Rfc2231 { charset, text } => {
            let pc = charset
                .filter(|c| !c.is_empty())
                .unwrap_or_else(|| "us-ascii".into());
            let raw: Vec<u8> = text.chars().map(|c| c as u32 as u8).collect();
            text::lookup(&pc)
                .and_then(|c| text::decode_strict(c, &raw))
                .unwrap_or(text)
        }
    });
    let cs = cs.filter(|c| c.is_ascii()).map(|c| c.to_lowercase());
    // кодировка объявлена (и не ASCII) — ей верим, отдельные битые байты заменяются
    if let Some(c) = cs.as_deref().and_then(text::lookup)
        && !matches!(c, Codec::Ascii)
    {
        return text::decode_replace(c, &payload);
    }
    // не указана, ASCII или неизвестна: подбираем (utf-8, windows-1251, koi8-r)
    decode_bytes(&payload, cs.as_deref())
}

/// Имя вложения без пути («C:\\dir\\a.txt» -> «a.txt»).
fn safe_attach_name(name: &str) -> String {
    py_strip(name.rsplit(['/', '\\']).next().unwrap_or("")).to_string()
}

pub struct Attachment {
    pub name: String,
    pub ctype: String,
    pub data: Vec<u8>,
    pub cid: String,
    pub inline: bool,
}

pub struct Body {
    pub text: Option<String>,
    pub html: Option<String>,
    pub attachments: Vec<Attachment>,
}

/// mailparse.parse_full
pub fn parse_full(raw: &[u8], forwarded: &str) -> Body {
    let norm = universal_newlines(raw);
    let d = &norm[..];
    let msg = parse_message(d);
    let mut body = Body {
        text: None,
        html: None,
        attachments: Vec::new(),
    };
    let mut n = 0;
    for pi in iter_parts(&msg, forwarded) {
        if pi.kind == Kind::Body {
            let t = part_text(pi.part);
            if pi.ctype == "text/html" {
                body.html = Some(t);
            } else {
                body.text = Some(t);
            }
            continue;
        }
        n += 1;
        let mut fname = safe_attach_name(&pi.fname);
        if fname.is_empty() {
            let ext = match pi.ctype.as_str() {
                "text/plain" => ".txt".to_string(),
                "text/html" => ".html".to_string(),
                "message/rfc822" => ".eml".to_string(),
                c if c.contains('/') => format!(".{}", c.rsplit('/').next().unwrap()),
                _ => ".bin".to_string(),
            };
            fname = format!("attachment_{n}{ext}");
        }
        let data = if pi.ctype == "message/rfc822" {
            // исходный текст вложенного письма (Python пересобирает его — здесь байты как есть)
            match &pi.part.payload {
                Payload::Parts(inner) if !inner.is_empty() => {
                    // исходный текст без перевода строки, принадлежащего границе
                    let (s, e) = inner[0].span;
                    let mut v = d[s..e].to_vec();
                    let k = eol_len(&v);
                    v.truncate(v.len() - k);
                    v
                }
                _ => Vec::new(),
            }
        } else {
            match pi.part.decoded_payload() {
                Some(v) => v,
                None => continue, // в Python здесь исключение -> часть пропускается
            }
        };
        body.attachments.push(Attachment {
            name: fname,
            ctype: pi.ctype,
            data,
            cid: pi.cid,
            inline: pi.kind == Kind::Inline,
        });
    }
    body
}

/// Письмо целиком для просмотрщика: .eml или .eml.gz с диска.
pub fn parse_body(path: &Path, forwarded: &str) -> Result<Body, MailError> {
    let (data, _, _) = read_file(path)?;
    Ok(parse_full(&data, forwarded))
}

/// Текст письма для поиска в просмотрщике: обычный текст, иначе HTML без разметки
/// (mailparse.search_text). Вложения не раскодируются — так в разы быстрее parse_full.
pub fn search_text(path: &Path, forwarded: &str) -> Result<String, MailError> {
    let (data, _, _) = read_file(path)?;
    let norm = universal_newlines(&data);
    let msg = parse_message(&norm);
    let (mut text, mut html) = (None, None);
    for pi in iter_parts(&msg, forwarded) {
        if pi.kind == Kind::Body {
            let t = part_text(pi.part);
            if pi.ctype == "text/html" {
                html = Some(t);
            } else {
                text = Some(t);
            }
        }
    }
    Ok(text.unwrap_or_else(|| html.map(|h| html_to_text(&h)).unwrap_or_default()))
}

/// Заголовки письма: первые 512 КБ до пустой строки (+ \r\n\r\n), как _read_head.
fn read_head(d: &[u8]) -> Vec<u8> {
    let data = &d[..d.len().min(512 * 1024)];
    let find = |pat: &[u8]| memchr::memmem::find(data, pat);
    let cut = match (find(b"\r\n\r\n"), find(b"\n\n")) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (a, b) => a.or(b),
    };
    let mut head = cut
        .map(|c| data[..c].to_vec())
        .unwrap_or_else(|| data.to_vec());
    head.extend_from_slice(b"\r\n\r\n");
    head
}

fn header_only(head: &[u8]) -> Headers {
    let lines = split_lines(head);
    let mut hl = Vec::new();
    for &(s, e) in &lines {
        let l = &head[s..e];
        if !is_header_line(l) {
            break;
        }
        hl.push((s, e));
    }
    if hl.len() > 1 {
        let (s, e) = *hl.last().unwrap();
        if head[s..e].starts_with(b"From ") {
            hl.pop();
        }
    }
    parse_header_lines(head, &hl)
}

// ================================================================ eml_to_message

static SCRIPT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)<(script|style)[^>]*>").unwrap());
static BR_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)<br\s*/?>|</p>|</div>|</tr>").unwrap());
static TAG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").unwrap());
static NL3_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\n{3,}").unwrap());

/// re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h) — без обратных ссылок.
fn strip_script_style(h: &str) -> String {
    let lower = h.to_ascii_lowercase(); // длины в байтах совпадают с исходной строкой
    let mut out = String::with_capacity(h.len());
    let (mut pos, mut search) = (0, 0);
    while search < h.len() {
        let Some(m) = SCRIPT_RE.captures_at(h, search) else {
            break;
        };
        let whole = m.get(0).unwrap();
        let close = format!("</{}>", m[1].to_ascii_lowercase());
        match lower[whole.end()..].find(&close) {
            Some(i) => {
                out.push_str(&h[pos..whole.start()]);
                out.push(' ');
                pos = whole.end() + i + close.len();
                search = pos;
            }
            // в этой позиции совпадения нет — дальше на один символ
            None => search = whole.start() + h[whole.start()..].chars().next().unwrap().len_utf8(),
        }
    }
    out.push_str(&h[pos..]);
    out
}

fn html_to_text(h: &str) -> String {
    let h = strip_script_style(h);
    let h = BR_RE.replace_all(&h, "\n");
    let h = TAG_RE.replace_all(&h, "");
    let h = htmlize::unescape(h.as_ref());
    let h = NL3_RE.replace_all(&h, "\n\n");
    py_strip(&h).to_string()
}

/// storage.safe_name
fn safe_name(s: &str, maxlen: usize) -> String {
    const RESERVED: [&str; 22] = [
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
        "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    ];
    let out: String = s
        .chars()
        .map(|c| {
            if INVALID.contains(c) || (c as u32) < 32 {
                '_'
            } else {
                c
            }
        })
        .collect();
    let mut out = out.trim_matches(|c| c == ' ' || c == '.').to_string();
    if out.is_empty() {
        out = "_".into();
    }
    if RESERVED.contains(&out.to_uppercase().as_str()) {
        out = format!("_{out}");
    }
    out.chars().take(maxlen).collect()
}

/// Path(name).stem / .suffix (Windows: разделители / и \).
fn stem_suffix(name: &str) -> (String, String) {
    let base = name.trim_end_matches(['/', '\\']);
    let base = base.rsplit(['/', '\\']).next().unwrap_or("");
    let chars: Vec<char> = base.chars().collect();
    match chars.iter().rposition(|c| *c == '.') {
        Some(i) if i > 0 && i < chars.len() - 1 => {
            (chars[..i].iter().collect(), chars[i..].iter().collect())
        }
        _ => (base.to_string(), String::new()),
    }
}

fn s(v: impl Into<String>) -> Option<Val> {
    Some(Val::Str(v.into()))
}

fn i(v: i64) -> Option<Val> {
    Some(Val::Int(v))
}

fn read_file(path: &Path) -> io::Result<(Vec<u8>, f64, u64)> {
    let meta = fs::metadata(path)?;
    let mtime = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs() as f64 + d.subsec_nanos() as f64 * 1e-9)
        .unwrap_or(0.0);
    let raw = fs::read(path)?;
    let packed = path.to_string_lossy().to_lowercase().ends_with(".eml.gz");
    if packed {
        let size = if raw.len() >= 4 {
            u32::from_le_bytes(raw[raw.len() - 4..].try_into().unwrap()) as u64
        } else {
            meta.len()
        };
        // ISIZE из повреждённого файла может быть любым (до 4 ГБ) — это лишь подсказка
        let mut out = Vec::with_capacity((size as usize).min(raw.len().saturating_mul(16)));
        flate2::read::MultiGzDecoder::new(&raw[..]).read_to_end(&mut out)?;
        Ok((out, mtime, size))
    } else {
        Ok((raw, mtime, meta.len()))
    }
}

const MIN_TS: i64 = 315_532_800; // 01.01.1980: всё, что раньше, — заведомо неверная дата

/// mailparse._hdr_ts: время из даты заголовка, если она разобрана и правдоподобна.
fn hdr_ts(v: &[u8]) -> Option<i64> {
    if !v.is_ascii() {
        return None;
    }
    let t = date::sent_timestamp(std::str::from_utf8(v).ok()?)?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(i64::MAX / 2);
    (MIN_TS..=now + 2 * 86400).contains(&t).then_some(t)
}

/// mailparse.header_received: дата из верхнего Received (её ставит почтовый сервер при
/// приёме), для писем без него — из Date.
fn header_received(hh: &Headers) -> Option<i64> {
    hh.get("Received")
        .and_then(|r| {
            let i = r.iter().rposition(|&c| c == b';')?;
            hdr_ts(&r[i + 1..])
        })
        .or_else(|| hh.get("Date").and_then(hdr_ts))
}

/// mailparse.received_from_bytes: дата получения письма в памяти (по заголовкам).
pub fn received_from_bytes(data: &[u8]) -> Option<i64> {
    header_received(&header_only(&read_head(data)))
}

/// mailparse.summarize: краткие сведения о письме для списка в просмотрщике.
pub struct Summary {
    pub from: String,
    pub from_name: String,
    pub from_addr: String,
    pub to: String,
    pub cc: String,
    pub subject: String,
    pub msgid: String,
    pub sent: Option<String>,
    pub received: i64,
    pub size: u64,
    pub fsize: u64,
    pub mtime: i64,
    pub att_names: Vec<String>,
}

pub fn summarize(path: &Path, forwarded: &str) -> Result<Summary, MailError> {
    let fsize = fs::metadata(path)?.len();
    let (data, mtime, size) = read_file(path)?;
    let head = read_head(&data);
    let hh = header_only(&head);
    let hdr = |n: &str| hh.get(n).map(decode_hdr_raw).unwrap_or_default();
    let frm = hdr("From");
    let (from_name, from_addr) = addr::parseaddr(&frm);
    let sent = hh
        .get("Date")
        .filter(|v| v.is_ascii())
        .and_then(|v| date::sent_iso(std::str::from_utf8(v).unwrap()));
    // вложения: только если письмо не простой текст/HTML (как в Python-версии)
    let norm = universal_newlines(&data);
    let msg = parse_message(&norm);
    let ctype = msg.content_type();
    let mut att_names = Vec::new();
    if ctype.starts_with("multipart/") || (ctype != "text/plain" && ctype != "text/html") {
        for pi in iter_parts(&msg, forwarded) {
            if pi.kind == Kind::Attachment {
                att_names.push(if pi.fname.is_empty() {
                    pi.ctype
                } else {
                    pi.fname
                });
            }
        }
    }
    Ok(Summary {
        to: hdr("To"),
        cc: hdr("Cc"),
        subject: hdr("Subject"),
        msgid: hh
            .get("Message-ID")
            .map(|v| py_strip(&decode_bytes(v, None)).to_string())
            .unwrap_or_default(),
        sent,
        received: header_received(&hh).unwrap_or(mtime.trunc() as i64),
        size,
        fsize,
        mtime: mtime.trunc() as i64,
        att_names,
        from: frm,
        from_name,
        from_addr,
    })
}

/// pst_export.eml_to_message: письмо -> свойства PST.
pub fn eml_to_message(path: &Path, read: bool, forwarded: &str) -> Result<Message, MailError> {
    let (data, mtime, size) = read_file(path)?;
    // summarize()
    let head = read_head(&data);
    let hh = header_only(&head);
    let hdr = |n: &str| hh.get(n).map(decode_hdr_raw).unwrap_or_default();
    let frm = hdr("From");
    let (from_name, from_addr) = addr::parseaddr(&frm);
    let to = hdr("To");
    let cc = hdr("Cc");
    let subject = hdr("Subject");
    let msgid = hh
        .get("Message-ID")
        .map(|v| py_strip(&decode_bytes(v, None)).to_string())
        .unwrap_or_default();
    let sent = hh
        .get("Date")
        .filter(|v| v.is_ascii())
        .and_then(|v| date::sent_timestamp(std::str::from_utf8(v).unwrap()));
    // дата получения: по заголовкам, иначе дата файла (IMAP INTERNALDATE)
    let received = header_received(&hh).unwrap_or(mtime.trunc() as i64);

    let body = parse_full(&data, forwarded);
    let sent_ft = sent.map(|t| filetime_f(t as f64));
    let recv_ft = if received != 0 {
        Some(filetime_i(received))
    } else {
        sent_ft
    };
    let sender_name = if from_name.is_empty() {
        from_addr.clone()
    } else {
        from_name
    };
    let text = match (&body.text, &body.html) {
        (Some(t), _) => t.clone(),
        (None, Some(h)) if !h.is_empty() => html_to_text(h),
        _ => String::new(),
    };
    let head_text = {
        let mut h: &[u8] = &head;
        while h.last().is_some_and(|c| *c == b'\r' || *c == b'\n') {
            h = &h[..h.len() - 1];
        }
        decode_bytes(h, None)
    };
    let ft = |v: Option<i64>| v.map(Val::Int);

    let mut props: Props = vec![
        (0x001A001F, s("IPM.Note")),
        (0x0037001F, s(&subject)),
        (0x0E1D001F, s(&subject)),
        (0x0070001F, s(&subject)),
        (0x00170003, i(1)),
        (0x00360003, i(0)),
        (0x00260003, i(0)),
        (0x0E080003, i(size as i64)),
        (0x0E170003, i(0)),
        (0x00390040, ft(sent_ft)),
        (0x0E060040, ft(recv_ft)),
        (0x30070040, ft(recv_ft)),
        (0x30080040, ft(recv_ft)),
        (0x0C1A001F, s(&sender_name)),
        (0x0C1F001F, s(&from_addr)),
        (0x0C1E001F, s("SMTP")),
        (0x5D01001F, s(&from_addr)),
        (0x0042001F, s(&sender_name)),
        (0x0065001F, s(&from_addr)),
        (0x0064001F, s("SMTP")),
        (0x5D02001F, s(&from_addr)),
        (0x1035001F, if msgid.is_empty() { None } else { s(&msgid) }),
        (0x007D001F, s(head_text)),
        (0x1000001F, s(text)),
        (0x300B0102, Some(Val::Bin(urandom16()))),
    ];
    if let Some(h) = body.html.as_ref().filter(|h| !h.is_empty()) {
        props.push((0x10130102, Some(Val::Bin(h.as_bytes().to_vec()))));
        props.push((0x3FDE0003, i(65001)));
    }

    let mut recipients = Vec::new();
    let mut disp: [Vec<String>; 2] = [Vec::new(), Vec::new()];
    for (rtype, field) in [(1i64, &to), (2, &cc)] {
        for (name, a) in addr::getaddresses(field) {
            if name.is_empty() && a.is_empty() {
                continue;
            }
            let nm = if name.is_empty() { a.clone() } else { name };
            disp[(rtype - 1) as usize].push(nm.clone());
            let mut entry = format!("SMTP:{}", a.to_uppercase()).into_bytes();
            entry.push(0);
            recipients.push(vec![
                (0x0C150003, i(rtype)),
                (0x0E0F000B, Some(Val::Bool(true))),
                (0x0FFE0003, i(6)),
                (0x39000003, i(0)),
                (0x3001001F, s(&nm)),
                (0x3002001F, s("SMTP")),
                (0x3003001F, s(&a)),
                (0x39FE001F, s(&a)),
                (0x39FF001F, s(&nm)),
                (0x0FF90102, Some(Val::Bin(urandom16()))),
                (0x300B0102, Some(Val::Bin(entry))),
                (0x3A40000B, Some(Val::Bool(false))),
            ]);
        }
    }
    props.push((0x0E04001F, s(disp[0].join("; "))));
    props.push((0x0E03001F, s(disp[1].join("; "))));

    let mut attachments = Vec::new();
    for a in body.attachments {
        let (stem, ext) = stem_suffix(&a.name);
        let short = if !ext.is_empty() {
            safe_name(&stem, 8) + &ext.chars().take(4).collect::<String>()
        } else {
            safe_name(&a.name, 8)
        };
        let n = a.data.len() as i64;
        let mut ap: Props = vec![
            (0x37050003, i(1)),
            (0x0E200003, i(n)),
            (0x3707001F, s(&a.name)),
            (0x3704001F, s(&short)),
            (0x3001001F, s(&a.name)),
            (0x370E001F, s(&a.ctype)),
            (0x370B0003, i(0xFFFF_FFFF)),
            (0x0FFE0003, i(7)),
            (0x37010102, Some(Val::Bin(a.data))),
            (0x30070040, ft(recv_ft)),
            (0x30080040, ft(recv_ft)),
        ];
        if !a.cid.is_empty() {
            ap.push((0x3712001F, s(&a.cid)));
        }
        if a.inline {
            ap.push((0x7FFE000B, Some(Val::Bool(true))));
        }
        let row: Props = vec![
            (0x0E200003, i(n)),
            (0x3704001F, s(&short)),
            (0x37050003, i(1)),
            (0x370B0003, i(0xFFFF_FFFF)),
        ];
        attachments.push((ap, row));
    }
    Ok(Message {
        props,
        recipients,
        attachments,
        read,
        received,
    })
}
