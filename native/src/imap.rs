//! IMAP-клиент загрузчика — то же, что core/imap_client.py: подключение (SSL/TLS, STARTTLS
//! или без шифрования), вход, список папок, поиск и выдача писем.
//!
//! «Стоп» закрывает сокеты всех соединений ящика (Stopper), поэтому ожидание ответа
//! сервера обрывается сразу, а не после 90 секунд тишины.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use base64::Engine as _;
use regex::Regex;
use regex::bytes::Regex as BRegex;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, ClientConnection, DigitallySignedStruct, SignatureScheme, StreamOwned};

use crate::{fmt_args, t};

/// Сколько ждать ответа сервера (как TIMEOUT в imap_client.py).
pub const TIMEOUT: Duration = Duration::from_secs(90);

/// Остановка загрузки: флаг и сокеты открытых соединений. set() закрывает сокеты — чтение,
/// ждущее ответа сервера, сразу завершается.
#[derive(Default)]
pub struct Stopper {
    flag: AtomicBool,
    next: AtomicU64,
    socks: Mutex<HashMap<u64, TcpStream>>,
}

impl Stopper {
    pub fn is_set(&self) -> bool {
        self.flag.load(Ordering::SeqCst)
    }

    pub fn set(&self) {
        self.flag.store(true, Ordering::SeqCst);
        for s in self.socks().values() {
            let _ = s.shutdown(Shutdown::Both);
        }
    }

    fn socks(&self) -> std::sync::MutexGuard<'_, HashMap<u64, TcpStream>> {
        self.socks.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn register(self: &Arc<Self>, s: &TcpStream) -> Registration {
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        if let Ok(c) = s.try_clone() {
            self.socks().insert(id, c);
        }
        if self.is_set() {
            let _ = s.shutdown(Shutdown::Both); // «Стоп» нажали, пока соединялись
        }
        Registration {
            stop: self.clone(),
            id,
        }
    }
}

/// Сокет соединения в Stopper; снимается, когда соединение закрыто.
struct Registration {
    stop: Arc<Stopper>,
    id: u64,
}

impl Drop for Registration {
    fn drop(&mut self) {
        self.stop.socks().remove(&self.id);
    }
}

// ---------------------------------------------------------------- ошибки

#[derive(Debug, Clone)]
pub enum Error {
    /// Обрыв связи, таймаут, сбой протокола — лечится переподключением.
    Conn(String),
    /// Сервер отказал (NO/BAD) или не дал нужного.
    Imap(String),
    /// Ошибка записи на диск.
    Io(String),
    /// TLS-рукопожатие не удалось (не из-за сертификата): сервер может знать только
    /// устаревший TLS — Python-модуль загрузки договаривается с такими.
    Tls(String),
    /// Остановлено пользователем.
    Stop,
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    pub fn is_connection(&self) -> bool {
        matches!(self, Error::Conn(_))
    }

    /// Имя ошибки для вопроса пользователю («ImapError: …», как в Python-версии).
    pub fn kind(&self) -> &'static str {
        match self {
            Error::Conn(_) => "ConnectionError",
            Error::Imap(_) | Error::Tls(_) => "ImapError",
            Error::Io(_) => "OSError",
            Error::Stop => "StopRequested",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Conn(m) | Error::Imap(m) | Error::Io(m) | Error::Tls(m) => f.write_str(m),
            Error::Stop => f.write_str("stopped"),
        }
    }
}

fn conn_err(e: io::Error) -> Error {
    Error::Conn(e.to_string())
}

// ---------------------------------------------------------------- папки

#[derive(Debug, Clone, Default)]
pub struct Folder {
    /// Имя на сервере (mUTF-7).
    pub raw: String,
    /// Человекочитаемое имя.
    pub display: String,
    pub delimiter: String,
    pub flags: Vec<String>,
}

impl Folder {
    pub fn selectable(&self) -> bool {
        !self.flags.iter().any(|f| {
            let f = f.to_lowercase();
            f == "\\noselect" || f == "\\nonexistent"
        })
    }
}

/// Modified UTF-7 (RFC 3501, 5.1.3) -> строка; битые куски остаются как есть.
pub fn utf7_decode(s: &str) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(i) = rest.find('&') {
        out.push_str(&rest[..i]);
        let tail = &rest[i..];
        let Some(j) = tail.find('-') else {
            out.push_str(tail);
            return out;
        };
        if j == 1 {
            out.push('&');
        } else {
            let chunk = tail[1..j].replace(',', "/");
            let decoded = base64::engine::general_purpose::STANDARD_NO_PAD
                .decode(chunk.trim_end_matches('='))
                .ok()
                .filter(|b| b.len() % 2 == 0)
                .and_then(|b| {
                    let units: Vec<u16> = b
                        .chunks_exact(2)
                        .map(|c| u16::from_be_bytes([c[0], c[1]]))
                        .collect();
                    String::from_utf16(&units).ok()
                });
            match decoded {
                Some(d) => out.push_str(&d),
                None => out.push_str(&tail[..=j]),
            }
        }
        rest = &tail[j + 1..];
    }
    out.push_str(rest);
    out
}

fn unquote(s: &str) -> String {
    let s = s.trim();
    if s.len() >= 2 && s.starts_with('"') && s.ends_with('"') {
        let mut out = String::new();
        let mut it = s[1..s.len() - 1].chars();
        while let Some(c) = it.next() {
            if c == '\\' {
                if let Some(n) = it.next() {
                    out.push(n);
                }
            } else {
                out.push(c);
            }
        }
        return out;
    }
    s.to_string()
}

fn list_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r#"(?is)^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$"#,
        )
        .unwrap()
    })
}

/// Строка «* LIST …» -> папка (imap_client.parse_list_response).
fn parse_list(resp: &Resp) -> Option<Folder> {
    let (head, lit) = &resp[0];
    let head = String::from_utf8_lossy(head);
    let rest = strip_prefix_ci(&head, "* LIST ")?;
    let m = list_re().captures(rest)?;
    let name = match lit {
        Some(l) => String::from_utf8_lossy(l).into_owned(),
        None => unquote(&m["name"]),
    };
    let delim = &m["delim"];
    let delimiter = if delim.eq_ignore_ascii_case("NIL") {
        String::new()
    } else {
        unquote(delim)
    };
    Some(Folder {
        display: utf7_decode(&name),
        raw: name,
        delimiter,
        flags: m["flags"].split_whitespace().map(str::to_string).collect(),
    })
}

fn strip_prefix_ci<'s>(s: &'s str, prefix: &str) -> Option<&'s str> {
    (s.len() >= prefix.len()
        && s.is_char_boundary(prefix.len())
        && s[..prefix.len()].eq_ignore_ascii_case(prefix))
    .then(|| &s[prefix.len()..])
}

// ---------------------------------------------------------------- наборы UID

/// «1:5,7,9:12» — UID подряд сворачиваются в диапазоны.
pub fn uid_set(uids: &[u64]) -> String {
    let mut nums: Vec<u64> = uids.to_vec();
    nums.sort_unstable();
    nums.dedup();
    let mut out = Vec::new();
    let mut i = 0;
    while i < nums.len() {
        let mut j = i;
        while j + 1 < nums.len() && nums[j + 1] == nums[j] + 1 {
            j += 1;
        }
        out.push(if i == j {
            nums[i].to_string()
        } else {
            format!("{}:{}", nums[i], nums[j])
        });
        i = j + 1;
    }
    out.join(",")
}

/// UID-наборы не длиннее limit символов: длинную команду сервер может отвергнуть.
fn uid_chunks(uids: &[u64], limit: usize) -> Vec<String> {
    let mut nums: Vec<u64> = uids.to_vec();
    nums.sort_unstable();
    nums.dedup();
    let mut out = Vec::new();
    let (mut chunk, mut size) = (Vec::new(), 0);
    for n in nums {
        chunk.push(n);
        size += n.to_string().len() + 1;
        if size >= limit {
            out.push(uid_set(&chunk));
            chunk.clear();
            size = 0;
        }
    }
    if !chunk.is_empty() {
        out.push(uid_set(&chunk));
    }
    out
}

// ---------------------------------------------------------------- разбор FETCH

fn re_uid() -> &'static BRegex {
    static RE: OnceLock<BRegex> = OnceLock::new();
    RE.get_or_init(|| BRegex::new(r"(?i)\bUID\s+(\d+)").unwrap())
}

fn re_size() -> &'static BRegex {
    static RE: OnceLock<BRegex> = OnceLock::new();
    RE.get_or_init(|| BRegex::new(r"(?i)RFC822\.SIZE\s+(\d+)").unwrap())
}

fn re_flags() -> &'static BRegex {
    static RE: OnceLock<BRegex> = OnceLock::new();
    RE.get_or_init(|| BRegex::new(r"FLAGS \(([^)]*)\)").unwrap())
}

fn re_fetch() -> &'static BRegex {
    static RE: OnceLock<BRegex> = OnceLock::new();
    RE.get_or_init(|| BRegex::new(r"(?i)^\*\s+\d+\s+FETCH\b").unwrap())
}

fn re_idate() -> &'static BRegex {
    static RE: OnceLock<BRegex> = OnceLock::new();
    RE.get_or_init(|| {
        BRegex::new(
            r#"(?i)INTERNALDATE\s+"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})(?:\s*([-+])(\d{2}):?(\d{2}))?\s*""#,
        )
        .unwrap()
    })
}

fn num_of(re: &BRegex, meta: &[u8]) -> Option<u64> {
    re.captures(meta)
        .and_then(|c| std::str::from_utf8(&c[1]).ok()?.parse().ok())
}

/// Дней от 1970-01-01 до даты (григорианский календарь).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// (год, месяц, день) по номеру дня от 1970-01-01.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

const MONTHS: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/// INTERNALDATE -> секунды эпохи (imap_client.parse_internaldate).
pub fn parse_internaldate(meta: &[u8]) -> Option<i64> {
    let c = re_idate().captures(meta)?;
    let s = |i: usize| std::str::from_utf8(c.get(i)?.as_bytes()).ok();
    let n = |i: usize| s(i)?.parse::<i64>().ok();
    let mon = s(2)?;
    let month = MONTHS.iter().position(|m| m.eq_ignore_ascii_case(mon))? as i64 + 1;
    let year = n(3)?;
    if !(1..=9999).contains(&year) {
        return None;
    }
    let days = days_from_civil(year, month, 1) + n(1)? - 1;
    let mut utc = days * 86400 + n(4)? * 3600 + n(5)? * 60 + n(6)?;
    if let Some(sign) = s(7) {
        let zone = (n(8)? * 60 + n(9)?) * 60;
        utc -= if sign == "+" { zone } else { -zone };
    }
    Some(utc)
}

/// «ГГГГ-ММ-ДД» -> дата для SEARCH («05-Jan-2015»); shift — сдвиг в днях.
pub fn imap_date(iso: &str, shift: i64) -> Option<String> {
    let mut it = iso.trim().splitn(3, '-').map(|p| p.parse::<i64>().ok());
    let (y, m, d) = (it.next()??, it.next()??, it.next()??);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    let (y, m, d) = civil_from_days(days_from_civil(y, m, d) + shift);
    Some(format!("{d:02}-{}-{y}", MONTHS[(m - 1) as usize]))
}

/// Ответ на FETCH одного письма: метаданные (текст вне литералов) и тело.
struct FetchItem<'r> {
    meta: Vec<u8>,
    body: Option<&'r [u8]>,
}

fn fetch_item(resp: &Resp) -> Option<FetchItem<'_>> {
    if !re_fetch().is_match(&resp[0].0) {
        return None;
    }
    let mut meta = Vec::new();
    let mut body = None;
    for (text, lit) in resp {
        meta.extend_from_slice(text);
        meta.push(b' ');
        if let Some(l) = lit
            && body.is_none()
            && text.to_ascii_uppercase().windows(5).any(|w| w == b"BODY[")
        {
            body = Some(l.as_slice());
        }
    }
    Some(FetchItem { meta, body })
}

fn flags_of(meta: &[u8]) -> String {
    re_flags()
        .captures(meta)
        .map(|c| String::from_utf8_lossy(&c[1]).into_owned())
        .unwrap_or_default()
}

/// Message-ID из заголовка (с продолжениями строк), как _MSGID + msgid_key в Python.
fn header_msgid(head: &[u8]) -> String {
    let mut out = String::new();
    let mut take = false;
    for line in head.split(|&c| c == b'\n') {
        if take && matches!(line.first(), Some(b' ' | b'\t')) {
            out.extend(line.iter().map(|&b| b as char));
            continue;
        }
        if take {
            break;
        }
        if line.len() >= 11 && line[..11].eq_ignore_ascii_case(b"message-id:") {
            take = true;
            out.extend(line[11..].iter().map(|&b| b as char)); // latin-1
        }
    }
    msgid_key(&out)
}

/// Message-ID для поиска повторов: без пробелов и переносов строк.
pub fn msgid_key(s: &str) -> String {
    s.split_whitespace().collect()
}

// ---------------------------------------------------------------- TLS

/// «Принимать все сертификаты»: просроченные, самоподписанные, с чужим именем.
#[derive(Debug)]
struct AcceptAll(Vec<SignatureScheme>);

impl ServerCertVerifier for AcceptAll {
    fn verify_server_cert(
        &self,
        _: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.clone()
    }
}

fn tls_config(accept_all: bool) -> Arc<ClientConfig> {
    static STRICT: OnceLock<Arc<ClientConfig>> = OnceLock::new();
    static LOOSE: OnceLock<Arc<ClientConfig>> = OnceLock::new();
    let build = || {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let schemes = provider
            .signature_verification_algorithms
            .supported_schemes();
        let builder = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .expect("ring поддерживает TLS 1.2 и 1.3");
        let cfg = if accept_all {
            builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(AcceptAll(schemes)))
                .with_no_client_auth()
        } else {
            // сертификаты Windows (там и корпоративные центры) и общеизвестные корневые
            let mut roots = rustls::RootCertStore::empty();
            for cert in rustls_native_certs::load_native_certs().certs {
                let _ = roots.add(cert);
            }
            roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
            builder.with_root_certificates(roots).with_no_client_auth()
        };
        Arc::new(cfg)
    };
    if accept_all {
        LOOSE.get_or_init(build).clone()
    } else {
        STRICT.get_or_init(build).clone()
    }
}

enum Stream {
    Plain(TcpStream),
    Tls(Box<StreamOwned<ClientConnection, TcpStream>>),
}

impl Stream {
    fn tcp(&self) -> &TcpStream {
        match self {
            Stream::Plain(s) => s,
            Stream::Tls(s) => s.get_ref(),
        }
    }
}

impl Read for Stream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Stream::Plain(s) => s.read(buf),
            Stream::Tls(s) => s.read(buf),
        }
    }
}

impl Write for Stream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Stream::Plain(s) => s.write(buf),
            Stream::Tls(s) => s.write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            Stream::Plain(s) => s.flush(),
            Stream::Tls(s) => s.flush(),
        }
    }
}

fn is_timeout(e: &io::Error) -> bool {
    matches!(
        e.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
    )
}

/// Причина отказа в сертификате — теми же словами, что у OpenSSL в Python-версии.
fn cert_reason(c: &rustls::CertificateError) -> String {
    use rustls::CertificateError as C;
    match c {
        C::Expired | C::ExpiredContext { .. } => "certificate has expired".into(),
        C::NotValidYet | C::NotValidYetContext { .. } => "certificate is not yet valid".into(),
        C::UnknownIssuer => "unable to get local issuer certificate".into(),
        C::NotValidForName | C::NotValidForNameContext { .. } => {
            "Hostname mismatch, certificate is not valid for this server".into()
        }
        C::Revoked => "certificate revoked".into(),
        C::Other(o) if format!("{o:?}").contains("CaUsedAsEndEntity") => {
            "self-signed certificate".into()
        }
        other => other.to_string(),
    }
}

/// Ошибка рукопожатия -> сообщение как в Python-версии. Сертификат — обычная ошибка,
/// остальное — Tls (пусть попробует Python-модуль: он знает и старые версии TLS).
fn handshake_error(e: io::Error) -> Error {
    let inner = e
        .get_ref()
        .and_then(|i| i.downcast_ref::<rustls::Error>())
        .cloned();
    match inner {
        Some(rustls::Error::InvalidCertificate(c)) => Error::Imap(fmt_args(
            &t(
                "Сертификат сервера не прошёл проверку ({0}). Включите «Принимать все сертификаты».",
            ),
            &[("0", &cert_reason(&c))],
        )),
        Some(r) => Error::Tls(fmt_args(
            &t("Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт."),
            &[("e", &r)],
        )),
        None => Error::Tls(fmt_args(
            &t("Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт."),
            &[("e", &e)],
        )),
    }
}

// ---------------------------------------------------------------- соединение

/// Параметры подключения (поля Account).
#[derive(Debug, Clone, Default)]
pub struct Server {
    pub host: String,
    pub port: u16,
    /// ssl | starttls | none
    pub security: String,
    pub accept_all_certs: bool,
    pub user: String,
    pub password: String,
}

/// Письмо из ответа FETCH: (uid, тело, флаги, INTERNALDATE).
pub type OnMessage<'a> = dyn FnMut(u64, &[u8], String, Option<i64>) -> Result<()> + 'a;

/// Строка ответа с литералами: [(текст, литерал после него)].
type Resp = Vec<(Vec<u8>, Option<Vec<u8>>)>;

enum Piece<'a> {
    Text(&'a [u8]),
    /// Литерал {N}: сервер должен ответить «+», потом идут данные.
    Lit(&'a [u8]),
    /// Конец строки команды, «+» от сервера, затем данные (AUTHENTICATE).
    Cont(&'a [u8]),
}

#[derive(PartialEq, Eq, Debug)]
enum Status {
    Ok,
    No,
    Bad,
}

pub struct Imap {
    s: Stream,
    buf: Vec<u8>,
    pos: usize,
    tag: u32,
    stop: Arc<Stopper>,
    _reg: Registration,
    logging_out: bool,
}

fn literal_len(line: &[u8]) -> Option<usize> {
    let body = line.strip_suffix(b"}")?;
    let open = body.iter().rposition(|&c| c == b'{')?;
    let n = &body[open + 1..];
    let n = n.strip_suffix(b"+").unwrap_or(n);
    if n.is_empty() || !n.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(n).ok()?.parse().ok()
}

fn quoted(s: &str) -> Vec<u8> {
    let mut out = vec![b'"'];
    for &b in s.as_bytes() {
        if b == b'"' || b == b'\\' {
            out.push(b'\\');
        }
        out.push(b);
    }
    out.push(b'"');
    out
}

/// Строка-аргумент: в кавычках, а если в ней не-ASCII или перевод строки — литералом.
fn astring(s: &str, buf: &mut Vec<u8>) -> bool {
    if s.is_ascii() && !s.bytes().any(|b| b == b'\r' || b == b'\n' || b == 0) {
        *buf = quoted(s);
        false
    } else {
        *buf = s.as_bytes().to_vec();
        true
    }
}

impl Imap {
    /// Подключиться и войти (ImapConnection.connect).
    pub fn connect(srv: &Server, stop: Arc<Stopper>) -> Result<Imap> {
        let cant = |e: &dyn fmt::Display| {
            Error::Imap(fmt_args(
                &t("Не удалось подключиться к {0}:{1} — {e}"),
                &[("0", &srv.host), ("1", &srv.port), ("e", e)],
            ))
        };
        let addrs: Vec<_> = (srv.host.as_str(), srv.port)
            .to_socket_addrs()
            .map_err(|e| cant(&e))?
            .collect();
        let mut last = None;
        let mut tcp = None;
        for a in addrs {
            if stop.is_set() {
                return Err(Error::Stop);
            }
            match TcpStream::connect_timeout(&a, TIMEOUT) {
                Ok(s) => {
                    tcp = Some(s);
                    break;
                }
                Err(e) => last = Some(e),
            }
        }
        let tcp = match tcp {
            Some(s) => s,
            None => {
                let e = last.unwrap_or_else(|| io::Error::other("no address"));
                return Err(cant(&e));
            }
        };
        let _ = tcp.set_nodelay(true);
        tcp.set_read_timeout(Some(TIMEOUT)).map_err(|e| cant(&e))?;
        tcp.set_write_timeout(Some(TIMEOUT)).map_err(|e| cant(&e))?;
        let reg = stop.register(&tcp);
        let mut imap = Imap {
            s: Stream::Plain(tcp),
            buf: Vec::new(),
            pos: 0,
            tag: 0,
            _reg: reg,
            stop,
            logging_out: false,
        };
        if srv.security == "ssl" {
            imap = imap.into_tls(srv)?;
        }
        imap.greeting()?;
        if srv.security == "starttls" {
            let (st, text) = imap.command(&[Piece::Text(b"STARTTLS")], &mut |_| Ok(()))?;
            if st != Status::Ok {
                return Err(Error::Imap(fmt_args(
                    &t("Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт."),
                    &[("e", &text)],
                )));
            }
            imap = imap.into_tls(srv)?;
        }
        imap.login(&srv.user, &srv.password)?;
        Ok(imap)
    }

    /// Включить TLS поверх открытого соединения (сразу или после STARTTLS).
    fn into_tls(self, srv: &Server) -> Result<Imap> {
        let Imap {
            s,
            buf,
            pos,
            tag,
            stop,
            _reg,
            logging_out,
        } = self;
        let Stream::Plain(mut tcp) = s else {
            return Err(Error::Imap("TLS is already on".into()));
        };
        let name = ServerName::try_from(srv.host.clone()).map_err(|e| {
            Error::Imap(fmt_args(
                &t("Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт."),
                &[("e", &e)],
            ))
        })?;
        let mut conn = ClientConnection::new(tls_config(srv.accept_all_certs), name)
            .map_err(|e| handshake_error(io::Error::new(io::ErrorKind::InvalidData, e)))?;
        while conn.is_handshaking() {
            match conn.complete_io(&mut tcp) {
                Ok(_) => {}
                Err(_) if stop.is_set() => return Err(Error::Stop),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) if is_timeout(&e) => {
                    return Err(Error::Imap(fmt_args(
                        &t("Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт."),
                        &[("e", &"timed out")],
                    )));
                }
                Err(e) => return Err(handshake_error(e)),
            }
        }
        Ok(Imap {
            s: Stream::Tls(Box::new(StreamOwned::new(conn, tcp))),
            buf,
            pos,
            tag,
            stop,
            _reg,
            logging_out,
        })
    }

    fn greeting(&mut self) -> Result<()> {
        let r = self.read_resp()?;
        let line = &r[0].0;
        let up = line.to_ascii_uppercase();
        if up.starts_with(b"* OK") || up.starts_with(b"* PREAUTH") {
            return Ok(());
        }
        Err(Error::Imap(String::from_utf8_lossy(line).into_owned()))
    }

    fn login(&mut self, user: &str, pw: &str) -> Result<()> {
        let (st, text) = if user.is_ascii() && pw.is_ascii() {
            let (mut u, mut p) = (Vec::new(), Vec::new());
            let ul = astring(user, &mut u);
            let pl = astring(pw, &mut p);
            fn arg(lit: bool, b: &[u8]) -> Piece<'_> {
                if lit { Piece::Lit(b) } else { Piece::Text(b) }
            }
            self.command(
                &[
                    Piece::Text(b"LOGIN "),
                    arg(ul, &u[..]),
                    Piece::Text(b" "),
                    arg(pl, &p[..]),
                ],
                &mut |_| Ok(()),
            )?
        } else {
            // логин или пароль не в ASCII — AUTHENTICATE PLAIN в UTF-8 (как в Python-версии)
            let token = base64::engine::general_purpose::STANDARD
                .encode(format!("\0{user}\0{pw}").as_bytes());
            self.command(
                &[
                    Piece::Text(b"AUTHENTICATE PLAIN"),
                    Piece::Cont(token.as_bytes()),
                ],
                &mut |_| Ok(()),
            )?
        };
        if st != Status::Ok {
            return Err(Error::Imap(fmt_args(
                &t("Ошибка авторизации: {msg}"),
                &[("msg", &text)],
            )));
        }
        Ok(())
    }

    /// Выйти и закрыть соединение; ошибки не важны.
    pub fn logout(mut self) {
        if !self.stop.is_set() {
            self.logging_out = true;
            let _ = self.command(&[Piece::Text(b"LOGOUT")], &mut |_| Ok(()));
        }
        let _ = self.s.tcp().shutdown(Shutdown::Both);
    }

    // ---------- чтение ----------

    fn fill(&mut self) -> Result<()> {
        if self.pos == self.buf.len() {
            self.buf.clear();
            self.pos = 0;
        } else if self.pos > (4 << 20) {
            self.buf.drain(..self.pos);
            self.pos = 0;
        }
        let len = self.buf.len();
        self.buf.resize(len + 64 * 1024, 0);
        loop {
            let r = self.s.read(&mut self.buf[len..]);
            self.buf.truncate(len + *r.as_ref().unwrap_or(&0));
            return match r {
                Ok(_) | Err(_) if self.stop.is_set() => Err(Error::Stop),
                Ok(0) => Err(Error::Conn("connection closed by server".into())),
                Ok(_) => Ok(()),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) if is_timeout(&e) => Err(Error::Conn("timed out".into())),
                Err(e) => Err(conn_err(e)),
            };
        }
    }

    fn read_line(&mut self) -> Result<Vec<u8>> {
        let mut from = self.pos;
        loop {
            if let Some(i) = memchr::memchr(b'\n', &self.buf[from..]) {
                let end = from + i;
                let mut line = self.buf[self.pos..end].to_vec();
                self.pos = end + 1;
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                return Ok(line);
            }
            from = self.buf.len();
            let keep = from - self.pos;
            self.fill()?;
            from = self.pos + keep; // fill() мог сдвинуть буфер
        }
    }

    fn read_exact(&mut self, n: usize) -> Result<Vec<u8>> {
        while self.buf.len() - self.pos < n {
            self.fill()?;
        }
        let out = self.buf[self.pos..self.pos + n].to_vec();
        self.pos += n;
        Ok(out)
    }

    fn read_resp(&mut self) -> Result<Resp> {
        let mut out = Vec::new();
        loop {
            let line = self.read_line()?;
            match literal_len(&line) {
                Some(n) => {
                    let lit = self.read_exact(n)?;
                    out.push((line, Some(lit)));
                }
                None => {
                    out.push((line, None));
                    return Ok(out);
                }
            }
        }
    }

    // ---------- команды ----------

    fn send(&mut self, data: &[u8]) -> Result<()> {
        self.s.write_all(data).map_err(conn_err)?;
        self.s.flush().map_err(conn_err)
    }

    fn tagged(line: &[u8], tag: &str) -> Option<(Status, String)> {
        let rest = line.strip_prefix(tag.as_bytes())?.strip_prefix(b" ")?;
        let text = String::from_utf8_lossy(rest).into_owned();
        let (word, msg) = text.split_once(' ').unwrap_or((&text, ""));
        let st = match word.to_ascii_uppercase().as_str() {
            "OK" => Status::Ok,
            "NO" => Status::No,
            _ => Status::Bad,
        };
        Some((st, msg.trim().to_string()))
    }

    /// Читать ответы до «+» (продолжение, -> None) или до ответа с тегом.
    fn read_until(
        &mut self,
        tag: &str,
        want_cont: bool,
        on_data: &mut dyn FnMut(&Resp) -> Result<()>,
    ) -> Result<Option<(Status, String)>> {
        loop {
            let r = self.read_resp()?;
            let first = &r[0].0;
            if let Some(done) = Self::tagged(first, tag) {
                return Ok(Some(done));
            }
            if first.starts_with(b"+") {
                if want_cont {
                    return Ok(None);
                }
                continue;
            }
            if first.starts_with(b"*") {
                if !self.logging_out && first.to_ascii_uppercase().starts_with(b"* BYE") {
                    return Err(Error::Conn(String::from_utf8_lossy(first).into_owned()));
                }
                on_data(&r)?;
            }
        }
    }

    fn command(
        &mut self,
        pieces: &[Piece<'_>],
        on_data: &mut dyn FnMut(&Resp) -> Result<()>,
    ) -> Result<(Status, String)> {
        self.tag += 1;
        let tag = format!("S{:04}", self.tag);
        let mut out = format!("{tag} ").into_bytes();
        for p in pieces {
            let data = match p {
                Piece::Text(b) => {
                    out.extend_from_slice(b);
                    continue;
                }
                Piece::Lit(b) => {
                    out.extend_from_slice(format!("{{{}}}\r\n", b.len()).as_bytes());
                    b
                }
                Piece::Cont(b) => {
                    out.extend_from_slice(b"\r\n");
                    b
                }
            };
            self.send(&out)?;
            out.clear();
            if let Some(done) = self.read_until(&tag, true, on_data)? {
                return Ok(done);
            }
            out.extend_from_slice(data);
        }
        out.extend_from_slice(b"\r\n");
        self.send(&out)?;
        Ok(self
            .read_until(&tag, false, on_data)?
            .expect("ответ с тегом"))
    }

    fn refused(text: &str) -> Error {
        Error::Imap(fmt_args(
            &t("Сервер отказал в выдаче письма: {data}"),
            &[("data", &text)],
        ))
    }

    // ---------- операции ----------

    pub fn list_folders(&mut self) -> Result<Vec<Folder>> {
        let mut out = Vec::new();
        let (st, text) = self.command(&[Piece::Text(b"LIST \"\" *")], &mut |r| {
            if let Some(f) = parse_list(r) {
                out.push(f);
            }
            Ok(())
        })?;
        if st != Status::Ok {
            return Err(Error::Imap(fmt_args(
                &t("Не удалось получить список папок: {data}"),
                &[("data", &text)],
            )));
        }
        out.sort_by_cached_key(|f| {
            (
                !f.raw.eq_ignore_ascii_case("INBOX"),
                f.display.to_lowercase(),
            )
        });
        Ok(out)
    }

    /// Открыть папку только для чтения; вернуть UIDVALIDITY.
    pub fn examine(&mut self, f: &Folder) -> Result<String> {
        let mut uv = None;
        let mut name = Vec::new();
        let lit = astring(&f.raw, &mut name);
        let arg = if lit {
            Piece::Lit(&name)
        } else {
            Piece::Text(&name)
        };
        let (st, text) = self.command(&[Piece::Text(b"EXAMINE "), arg], &mut |r| {
            let up = r[0].0.to_ascii_uppercase();
            if uv.is_none()
                && let Some(i) = memchr::memmem::find(&up, b"[UIDVALIDITY ")
            {
                let digits: String = up[i + 13..]
                    .iter()
                    .take_while(|c| c.is_ascii_digit())
                    .map(|&c| c as char)
                    .collect();
                if !digits.is_empty() {
                    uv = Some(digits);
                }
            }
            Ok(())
        })?;
        if st != Status::Ok {
            return Err(Error::Imap(fmt_args(
                &t("Не удалось открыть папку «{0}»: {data}"),
                &[("0", &f.display), ("data", &text)],
            )));
        }
        Ok(uv.unwrap_or_else(|| "0".into()))
    }

    /// UID писем папки; since/before — даты в формате IMAP.
    pub fn search(&mut self, since: Option<&str>, before: Option<&str>) -> Result<Vec<u64>> {
        let mut cmd = String::from("UID SEARCH");
        if let Some(d) = since {
            cmd += &format!(" SINCE {d}");
        }
        if let Some(d) = before {
            cmd += &format!(" BEFORE {d}");
        }
        if since.is_none() && before.is_none() {
            cmd += " ALL";
        }
        let mut uids = Vec::new();
        let mut seen = HashSet::new();
        let (st, text) = self.command(&[Piece::Text(cmd.as_bytes())], &mut |r| {
            let line = String::from_utf8_lossy(&r[0].0);
            if let Some(rest) = strip_prefix_ci(&line, "* SEARCH") {
                for tok in rest.split_whitespace() {
                    match tok.parse::<u64>() {
                        Ok(u) => {
                            if seen.insert(u) {
                                uids.push(u);
                            }
                        }
                        Err(_) => break, // «(MODSEQ …)» и прочее после номеров
                    }
                }
            }
            Ok(())
        })?;
        if st != Status::Ok {
            return Err(Error::Imap(fmt_args(
                &t("Ошибка поиска писем: {data}"),
                &[("data", &text)],
            )));
        }
        Ok(uids)
    }

    /// {uid: размер} — чтобы набирать пачки.
    pub fn fetch_sizes(&mut self, uids: &[u64]) -> Result<HashMap<u64, u64>> {
        let mut out = HashMap::new();
        for chunk in uid_chunks(uids, 4000) {
            let cmd = format!("UID FETCH {chunk} (UID RFC822.SIZE)");
            let (st, text) = self.command(&[Piece::Text(cmd.as_bytes())], &mut |r| {
                if let Some(it) = fetch_item(r)
                    && let (Some(u), Some(z)) =
                        (num_of(re_uid(), &it.meta), num_of(re_size(), &it.meta))
                {
                    out.insert(u, z);
                }
                Ok(())
            })?;
            if st != Status::Ok {
                return Err(Self::refused(&text));
            }
        }
        Ok(out)
    }

    /// {uid: (размер, Message-ID)} — для пачек и поиска повторов, без тел писем.
    pub fn fetch_meta(&mut self, uids: &[u64]) -> Result<HashMap<u64, (u64, String)>> {
        let mut out = HashMap::new();
        for chunk in uid_chunks(uids, 4000) {
            let cmd = format!(
                "UID FETCH {chunk} (UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
            );
            let (st, text) = self.command(&[Piece::Text(cmd.as_bytes())], &mut |r| {
                if let Some(it) = fetch_item(r)
                    && let (Some(u), Some(z)) =
                        (num_of(re_uid(), &it.meta), num_of(re_size(), &it.meta))
                {
                    out.insert(u, (z, header_msgid(it.body.unwrap_or_default())));
                }
                Ok(())
            })?;
            if st != Status::Ok {
                return Err(Self::refused(&text));
            }
        }
        Ok(out)
    }

    /// Несколько писем одной командой. Каждое пришедшее письмо сразу отдаётся on_msg
    /// (uid, тело, флаги, INTERNALDATE) — его можно сохранить, не дожидаясь остальных.
    pub fn fetch_many(&mut self, uids: &[u64], on_msg: &mut OnMessage<'_>) -> Result<()> {
        let cmd = format!(
            "UID FETCH {} (UID INTERNALDATE FLAGS BODY.PEEK[])",
            uid_set(uids)
        );
        let (st, text) = self.command(&[Piece::Text(cmd.as_bytes())], &mut |r| {
            if let Some(it) = fetch_item(r)
                && let (Some(u), Some(body)) = (num_of(re_uid(), &it.meta), it.body)
            {
                on_msg(u, body, flags_of(&it.meta), parse_internaldate(&it.meta))?;
            }
            Ok(())
        })?;
        if st != Status::Ok {
            return Err(Self::refused(&text));
        }
        Ok(())
    }

    /// Одно письмо: (тело, флаги, INTERNALDATE).
    pub fn fetch_one(&mut self, uid: u64) -> Result<(Vec<u8>, String, Option<i64>)> {
        let mut got = None;
        let cmd = format!("UID FETCH {uid} (UID INTERNALDATE FLAGS BODY.PEEK[])");
        let (st, text) = self.command(&[Piece::Text(cmd.as_bytes())], &mut |r| {
            if let Some(it) = fetch_item(r)
                && let Some(body) = it.body
                && got.is_none()
                && num_of(re_uid(), &it.meta).is_none_or(|u| u == uid)
            {
                got = Some((
                    body.to_vec(),
                    flags_of(&it.meta),
                    parse_internaldate(&it.meta),
                ));
            }
            Ok(())
        })?;
        if st != Status::Ok {
            return Err(Self::refused(&text));
        }
        got.ok_or_else(|| {
            Error::Imap(t(
                "Сервер не вернул содержимое письма (письмо удалено, повреждено или недоступно)",
            ))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utf7() {
        assert_eq!(
            utf7_decode("&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"),
            "Отправленные"
        );
        assert_eq!(utf7_decode("A&-B"), "A&B");
        assert_eq!(utf7_decode("bad&"), "bad&");
        assert_eq!(utf7_decode("x&@@@-y"), "x&@@@-y");
    }

    #[test]
    fn sets() {
        assert_eq!(uid_set(&[5, 1, 2, 3, 7, 9, 10]), "1:3,5,7,9:10");
        assert_eq!(uid_chunks(&(1..=1000).collect::<Vec<_>>(), 4000).len(), 1);
        assert_eq!(
            uid_chunks(&(1000..=2000).collect::<Vec<_>>(), 4000).len(),
            2
        );
    }

    #[test]
    fn dates() {
        assert_eq!(imap_date("2015-01-05", 0).unwrap(), "05-Jan-2015");
        assert_eq!(imap_date("2024-02-29", 1).unwrap(), "01-Mar-2024");
        assert_eq!(imap_date("2015-12-31", 1).unwrap(), "01-Jan-2016");
        assert_eq!(
            parse_internaldate(b"INTERNALDATE \"05-Jan-2015 10:00:00 +0300\""),
            Some(1420441200)
        );
        assert_eq!(
            parse_internaldate(b"INTERNALDATE \" 5-jan-2015 07:00:00\""),
            Some(1420441200)
        );
    }

    #[test]
    fn msgid() {
        assert_eq!(header_msgid(b"Message-ID: <a@b>\r\n\r\n"), "<a@b>");
        assert_eq!(
            header_msgid(b"message-id:\r\n <long\r\n @x>\r\n"),
            "<long@x>"
        );
        assert_eq!(header_msgid(b"\r\n"), "");
    }

    #[test]
    fn list() {
        let r: Resp = vec![(
            br#"* LIST (\HasNoChildren \All) "/" "[Gmail]/All Mail""#.to_vec(),
            None,
        )];
        let f = parse_list(&r).unwrap();
        assert_eq!(
            (f.raw.as_str(), f.delimiter.as_str()),
            ("[Gmail]/All Mail", "/")
        );
        assert_eq!(f.flags, vec!["\\HasNoChildren", "\\All"]);
        let r: Resp = vec![
            (br#"* LIST () NIL {5}"#.to_vec(), Some(b"Inbox".to_vec())),
            (Vec::new(), None),
        ];
        let f = parse_list(&r).unwrap();
        assert_eq!((f.raw.as_str(), f.delimiter.as_str()), ("Inbox", ""));
        assert_eq!(literal_len(b"* 1 FETCH (BODY[] {123}"), Some(123));
        assert_eq!(literal_len(b"x {12+}"), Some(12));
        assert_eq!(literal_len(b"x {}"), None);
    }
}
