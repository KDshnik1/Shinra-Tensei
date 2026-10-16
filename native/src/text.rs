//! Кодировки и заголовки так, как их понимает Python (codecs + email.header).

use encoding_rs::Encoding;
use regex::bytes::Regex;
use std::sync::LazyLock;

#[derive(Clone, Copy)]
pub enum Codec {
    Ascii,
    Latin1,
    Rs(&'static Encoding),
}

/// Нормализация имени кодировки, как encodings.normalize_encoding в Python.
fn normalize(label: &str) -> String {
    let mut out = String::new();
    let mut punct = false;
    for c in label.trim().chars() {
        if c.is_ascii_alphanumeric() || c == '.' {
            if punct && !out.is_empty() {
                out.push('_');
            }
            out.push(c.to_ascii_lowercase());
            punct = false;
        } else {
            punct = true;
        }
    }
    out
}

/// codecs.lookup(): None — LookupError.
pub fn lookup(label: &str) -> Option<Codec> {
    let n = normalize(label);
    match n.as_str() {
        "ascii" | "us_ascii" | "646" | "us" | "iso646_us" | "cp367" | "csascii" | "ibm367"
        | "iso_ir_6" | "ansi_x3.4_1968" | "ansi_x3_4_1968" | "ansi_x3.4_1986" => {
            return Some(Codec::Ascii);
        }
        "latin_1" | "latin1" | "latin" | "l1" | "iso8859_1" | "iso_8859_1" | "8859" | "cp819"
        | "iso_ir_100" | "csisolatin1" | "iso_8859_1_1987" | "ibm819" => {
            return Some(Codec::Latin1);
        }
        "utf_8" | "utf8" | "u8" | "utf" | "cp65001" | "utf8_ucs2" | "utf8_ucs4" => {
            return Some(Codec::Rs(encoding_rs::UTF_8));
        }
        "mac_cyrillic" | "maccyrillic" => return Some(Codec::Rs(encoding_rs::X_MAC_CYRILLIC)),
        "koi8_r" | "koi8r" => return Some(Codec::Rs(encoding_rs::KOI8_R)),
        "koi8_u" | "koi8u" => return Some(Codec::Rs(encoding_rs::KOI8_U)),
        "cp1251" | "windows_1251" => return Some(Codec::Rs(encoding_rs::WINDOWS_1251)),
        _ => {}
    }
    let enc = Encoding::for_label(label.trim().as_bytes())
        .or_else(|| Encoding::for_label(n.replace('_', "-").as_bytes()))?;
    if enc == encoding_rs::REPLACEMENT {
        return None;
    }
    Some(Codec::Rs(enc))
}

/// windows-125x: WHATWG отдаёт управляющие C1 там, где у Python «нет символа».
fn c1_is_error(enc: &'static Encoding) -> bool {
    enc.name().starts_with("windows-")
}

/// bytes.decode(codec) — None при ошибке (strict).
pub fn decode_strict(c: Codec, b: &[u8]) -> Option<String> {
    match c {
        Codec::Ascii => b.is_ascii().then(|| String::from_utf8(b.to_vec()).unwrap()),
        Codec::Latin1 => Some(b.iter().map(|&x| x as char).collect()),
        Codec::Rs(enc) => {
            let s = enc.decode_without_bom_handling_and_without_replacement(b)?;
            if c1_is_error(enc) && s.chars().any(|ch| ('\u{80}'..='\u{9f}').contains(&ch)) {
                return None;
            }
            Some(s.into_owned())
        }
    }
}

/// bytes.decode(codec, errors="replace").
pub fn decode_replace(c: Codec, b: &[u8]) -> String {
    match c {
        Codec::Ascii => b
            .iter()
            .map(|&x| if x < 0x80 { x as char } else { '\u{FFFD}' })
            .collect(),
        Codec::Latin1 => b.iter().map(|&x| x as char).collect(),
        Codec::Rs(enc) => {
            let (s, _) = enc.decode_without_bom_handling(b);
            if c1_is_error(enc) {
                s.chars()
                    .map(|ch| {
                        if ('\u{80}'..='\u{9f}').contains(&ch) {
                            '\u{FFFD}'
                        } else {
                            ch
                        }
                    })
                    .collect()
            } else {
                s.into_owned()
            }
        }
    }
}

/// mailparse._decode_bytes: объявленная кодировка, затем utf-8, cp1251, koi8-r, latin-1.
pub fn decode_bytes(b: &[u8], charset: Option<&str>) -> String {
    if let Some(cs) = charset {
        let low = cs.to_lowercase();
        if !matches!(low.as_str(), "unknown-8bit" | "x-unknown" | "unknown")
            && let Some(s) = lookup(cs).and_then(|c| decode_strict(c, b))
        {
            return s;
        }
    }
    for c in [
        Codec::Rs(encoding_rs::UTF_8),
        Codec::Rs(encoding_rs::WINDOWS_1251),
        Codec::Rs(encoding_rs::KOI8_R),
    ] {
        if let Some(s) = decode_strict(c, b) {
            return s;
        }
    }
    b.iter().map(|&x| x as char).collect()
}

/// Пробельный символ в смысле str.split() Python.
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// " ".join(s.split())
pub fn collapse_ws(s: &str) -> String {
    s.split(py_space)
        .filter(|w| !w.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

static ECRE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s-u)=\?([^?]*?)\?([qQbB])\?(.*?)\?=").unwrap());

const B64_INVALID: u8 = 0xFF;
static B64_TABLE: [u8; 256] = {
    let mut t = [B64_INVALID; 256];
    let alpha = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut i = 0;
    while i < 64 {
        t[alpha[i] as usize] = i as u8;
        i += 1;
    }
    t
};

/// binascii.a2b_base64 (нестрогий режим, как в Python 3.10–3.12). None — ошибка.
pub fn a2b_base64(data: &[u8]) -> Option<Vec<u8>> {
    use base64::Engine;
    // быстрый путь: корректный base64 без мусора
    if data.len().is_multiple_of(4)
        && let Ok(v) = base64::engine::general_purpose::STANDARD.decode(data)
    {
        return Some(v);
    }
    let mut out = Vec::with_capacity(data.len() / 4 * 3 + 3);
    let (mut quad, mut left, mut pads) = (0u32, 0u8, 0u32);
    for &c in data {
        if c == b'=' {
            if quad >= 2 {
                pads += 1;
                if quad + pads >= 4 {
                    return Some(out);
                }
            }
            continue;
        }
        let v = B64_TABLE[c as usize];
        if v == B64_INVALID {
            continue;
        }
        pads = 0;
        match quad {
            0 => {
                quad = 1;
                left = v;
            }
            1 => {
                quad = 2;
                out.push((left << 2) | (v >> 4));
                left = v & 0x0F;
            }
            2 => {
                quad = 3;
                out.push((left << 4) | (v >> 2));
                left = v & 0x03;
            }
            _ => {
                quad = 0;
                out.push((left << 6) | v);
                left = 0;
            }
        }
    }
    if quad != 0 { None } else { Some(out) }
}

/// binascii.a2b_qp(header=False) — как quopri.decodestring.
pub fn a2b_qp(d: &[u8]) -> Vec<u8> {
    let hex = |c: u8| (c as char).to_digit(16).map(|v| v as u8);
    let mut out = Vec::with_capacity(d.len());
    let mut i = 0;
    while i < d.len() {
        if d[i] == b'=' {
            i += 1;
            if i >= d.len() {
                break;
            }
            if d[i] == b'\n' || d[i] == b'\r' {
                if d[i] != b'\n' {
                    while i < d.len() && d[i] != b'\n' {
                        i += 1;
                    }
                }
                if i < d.len() {
                    i += 1;
                }
            } else if d[i] == b'=' {
                out.push(b'=');
                i += 1;
            } else if i + 1 < d.len() && hex(d[i]).is_some() && hex(d[i + 1]).is_some() {
                out.push(hex(d[i]).unwrap() << 4 | hex(d[i + 1]).unwrap());
                i += 2;
            } else {
                out.push(b'=');
            }
        } else {
            out.push(d[i]);
            i += 1;
        }
    }
    out
}

/// bytes(str, 'raw-unicode-escape')
fn raw_unicode_escape(s: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(s.len());
    for c in s.chars() {
        let u = c as u32;
        if u < 0x100 {
            out.push(u as u8);
        } else if u < 0x10000 {
            out.extend_from_slice(format!("\\u{u:04x}").as_bytes());
        } else {
            out.extend_from_slice(format!("\\U{u:08x}").as_bytes());
        }
    }
    out
}

/// str.splitlines() Python.
fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let brk = matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if brk {
            out.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r'
                && let Some(&(_, '\n')) = it.peek()
            {
                it.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// email.header.decode_header для строки. Err — HeaderParseError.
fn decode_header(header: &str) -> Result<Vec<(Vec<u8>, Option<String>)>, ()> {
    // words: (строка, кодирование, кодировка)
    let mut words: Vec<(Vec<u8>, Option<u8>, Option<String>)> = Vec::new();
    for line in py_splitlines(header) {
        let lb = line.as_bytes();
        let mut first = true;
        let mut last = 0;
        let push_unenc = |seg: &[u8], first: &mut bool, words: &mut Vec<_>| {
            let seg = std::str::from_utf8(seg).unwrap_or("");
            let seg = if *first {
                *first = false;
                seg.trim_start_matches(py_space)
            } else {
                seg
            };
            if !seg.is_empty() {
                words.push((seg.as_bytes().to_vec(), None, None));
            }
        };
        for caps in ECRE.captures_iter(lb) {
            let m = caps.get(0).unwrap();
            push_unenc(&lb[last..m.start()], &mut first, &mut words);
            let charset = String::from_utf8_lossy(&caps[1]).to_lowercase();
            let enc = caps[2][0].to_ascii_lowercase();
            words.push((caps[3].to_vec(), Some(enc), Some(charset)));
            last = m.end();
        }
        push_unenc(&lb[last..], &mut first, &mut words);
    }
    // убрать пробелы между двумя закодированными словами
    let is_space = |w: &[u8]| {
        let s = std::str::from_utf8(w).unwrap_or("x");
        !s.is_empty() && s.chars().all(py_space)
    };
    let mut drop = Vec::new();
    for n in 2..words.len() {
        if words[n].1.is_some() && words[n - 2].1.is_some() && is_space(&words[n - 1].0) {
            drop.push(n - 1);
        }
    }
    for d in drop.into_iter().rev() {
        words.remove(d);
    }
    let mut decoded: Vec<(Vec<u8>, Option<String>)> = Vec::new();
    for (s, enc, cs) in words {
        match enc {
            None => {
                let st = std::str::from_utf8(&s).unwrap_or("");
                decoded.push((raw_unicode_escape(st), cs));
            }
            Some(b'q') => {
                let mut out = Vec::with_capacity(s.len());
                let mut i = 0;
                let hex = |c: u8| (c as char).to_digit(16).map(|v| v as u8);
                while i < s.len() {
                    if s[i] == b'='
                        && i + 2 < s.len()
                        && let (Some(a), Some(b)) = (
                            s.get(i + 1).and_then(|&c| hex(c)),
                            s.get(i + 2).and_then(|&c| hex(c)),
                        )
                    {
                        out.push(a << 4 | b);
                        i += 3;
                        continue;
                    }
                    out.push(if s[i] == b'_' { b' ' } else { s[i] });
                    i += 1;
                }
                // header_decode возвращает str, затем raw-unicode-escape -> те же байты
                decoded.push((out, cs));
            }
            Some(_) => {
                let mut t = s.clone();
                let pad = t.len() % 4;
                if pad != 0 {
                    t.extend_from_slice(&b"==="[..4 - pad]);
                }
                let w = if t.is_empty() {
                    Vec::new()
                } else {
                    a2b_base64(&t).ok_or(())?
                };
                decoded.push((w, cs));
            }
        }
    }
    let mut collapsed: Vec<(Vec<u8>, Option<String>)> = Vec::new();
    for (w, cs) in decoded {
        match collapsed.last_mut() {
            Some(last) if last.1 == cs => {
                if cs.is_none() {
                    last.0.push(b' ');
                }
                last.0.extend_from_slice(&w);
            }
            _ => collapsed.push((w, cs)),
        }
    }
    Ok(collapsed)
}

/// mailparse.decode_hdr для «сырых» байтов заголовка.
/// 8-битные байты: в Python это Header(unknown-8bit) — весь заголовок декодируется цепочкой.
pub fn decode_hdr_raw(raw: &[u8]) -> String {
    if !raw.is_ascii() {
        return collapse_ws(&decode_bytes(raw, Some("unknown-8bit")));
    }
    decode_hdr_str(std::str::from_utf8(raw).unwrap())
}

/// Закодированное слово =?..?B?..?= с символами не из base64 — его не декодируем
/// (получилась бы бессмыслица), заголовок остаётся как есть.
fn bad_b64_word(s: &str) -> bool {
    ECRE.captures_iter(s.as_bytes()).any(|c| {
        c[2].eq_ignore_ascii_case(b"b")
            && !c[3]
                .iter()
                .all(|&b| b.is_ascii_alphanumeric() || b == b'+' || b == b'/' || b == b'=')
    })
}

/// mailparse.decode_hdr для уже готовой строки.
pub fn decode_hdr_str(s: &str) -> String {
    if !ECRE.is_match(s.as_bytes()) || bad_b64_word(s) {
        return collapse_ws(s);
    }
    match decode_header(s) {
        Ok(parts) => {
            let mut out = String::new();
            for (b, cs) in parts {
                out.push_str(&decode_bytes(&b, cs.as_deref()));
            }
            collapse_ws(&out)
        }
        Err(()) => collapse_ws(s),
    }
}
