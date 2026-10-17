//! Загрузка одного почтового ящика — то же, что core/downloader.py, но на Rust.
//!
//! Первое соединение разбирает папки (анализ), потом готовит их частями по PREP_CHUNK
//! писем (размеры и Message-ID) и кладёт пачки в общую очередь; пачки качают все
//! соединения ящика. «Вся почта» Gmail при поиске повторов идёт последней. Письма
//! сохраняются в архив тем же форматом, что у Python-версии (core/storage.py, msgfile.py).
//!
//! Python видит только Host: статус, журнал, счётчики, вопросы пользователю.

use std::collections::{HashMap, HashSet, VecDeque};
use std::fs;
use std::io::{self, Write};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, SystemTime};

use crate::imap::{Error, Folder, Imap, Result, Server, Stopper, imap_date};
use crate::{fmt_args, t};

const MAX_AUTO_RECONNECTS: u64 = 3;
/// Писем в одной команде FETCH.
const BATCH_COUNT: usize = 100;
/// И не больше стольких байт (письма держатся в памяти).
const BATCH_BYTES: u64 = 16 * 1024 * 1024;
/// Размер, если сервер его не назвал.
const UNKNOWN_SIZE: u64 = 1024 * 1024;
/// Соединений на один ящик, не больше.
pub const MAX_CONNECTIONS: usize = 10;
/// Писем в одном запросе размеров и Message-ID при подготовке.
const PREP_CHUNK: usize = 1000;

/// (Message-ID, размер) — ключ повтора письма.
pub type Key = (String, u64);

/// Ответ пользователя на ошибку письма.
#[derive(PartialEq, Eq)]
pub enum Answer {
    Retry,
    Skip,
    SkipAll,
    Stop,
}

/// Связь с программой: всё, что загрузчик сообщает наружу и спрашивает.
pub trait Host: Sync {
    fn status(&self, text: &str);
    fn progress(&self, done: u64, total: u64);
    fn log(&self, text: &str);
    fn counters(&self, downloaded: u64, already: u64, errors: u64);
    /// Вопрос «пропустить письмо?»; ждёт ответа (или остановки).
    fn ask(&self, folder: &str, uid: u64, error: &str, details: &str) -> Answer;
    fn stopped(&self) -> bool;
    /// «Остановить ящик» из вопроса — для всех соединений.
    fn request_stop(&self);
    /// Ключи писем, уже лежащих в архиве ящика (индекс просмотрщика).
    fn archive_keys(&self) -> HashSet<Key>;
}

/// Настройки ящика (поля Account и то, что Python вычисляет за него).
#[derive(Debug, Clone, Default)]
pub struct Account {
    pub server: Server,
    pub security_text: String,
    pub date_mode: String,
    pub date_from: String,
    pub date_to: String,
    pub folder_mode: String,
    pub folder_raw: String,
    pub folder_display: String,
    pub connections: usize,
    /// auto | always | never
    pub duplicates: String,
    pub compress: bool,
    /// Папка ящика в архиве (MailboxArchive.dir).
    pub mailbox_dir: PathBuf,
    /// Разделитель разрядов для чисел в статусе (i18n.num).
    pub thousands: String,
}

#[derive(Debug, Default, Clone, Copy)]
pub struct Counts {
    pub downloaded: u64,
    pub already: u64,
    pub errors: u64,
}

// ---------------------------------------------------------------- архив

const INVALID: &str = "<>:\"/\\|?*";
const RESERVED: [&str; 22] = [
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
    "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
];

/// storage.safe_name: имя, допустимое для папки Windows.
pub fn safe_name(s: &str, maxlen: usize) -> String {
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
        out.insert(0, '_');
    }
    out.chars().take(maxlen).collect()
}

const ALL_MAIL_NAMES: [&str; 4] = [
    "[gmail]/all mail",
    "[google mail]/all mail",
    "[gmail]/вся почта",
    "[google mail]/вся почта",
];

/// storage.is_all_mail: папка с копиями писем из других папок («Вся почта» Gmail).
pub fn is_all_mail(f: &Folder) -> bool {
    f.flags.iter().any(|x| x.to_lowercase() == "\\all")
        || ALL_MAIL_NAMES.contains(&f.display.to_lowercase().as_str())
}

struct Archive {
    dir: PathBuf,
    meta_lock: Mutex<()>,
    flags_lock: Mutex<()>,
}

const META: &str = "folders.json";

impl Archive {
    /// MailboxArchive.folder_dir: папка писем на диске (и запись в folders.json).
    fn folder_dir(&self, f: &Folder) -> io::Result<PathBuf> {
        let segs: Vec<&str> = if f.delimiter.is_empty() {
            vec![f.display.as_str()]
        } else {
            f.display.split(f.delimiter.as_str()).collect()
        };
        let mut rel: Vec<String> = segs
            .iter()
            .filter(|s| !s.is_empty())
            .map(|s| safe_name(s, 60))
            .collect();
        if rel.is_empty() {
            rel.push("_".into());
        }
        let key = rel.join("/");
        let mut info = serde_json::Map::new();
        info.insert("display".into(), f.display.clone().into());
        info.insert("delimiter".into(), f.delimiter.clone().into());
        info.insert("raw".into(), f.raw.clone().into());
        info.insert("flags".into(), f.flags.clone().into());
        let info = serde_json::Value::Object(info);
        {
            let _g = lock(&self.meta_lock);
            let path = self.dir.join(META);
            let mut meta = fs::read(&path)
                .ok()
                .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
                .and_then(|v| match v {
                    serde_json::Value::Object(m) => Some(m),
                    _ => None,
                })
                .unwrap_or_default();
            if meta.get(&key) != Some(&info) {
                meta.insert(key, info);
                fs::create_dir_all(&self.dir)?;
                let tmp = self.dir.join(format!("{META}.tmp"));
                let text = serde_json::to_string_pretty(&serde_json::Value::Object(meta))
                    .map_err(io::Error::other)?;
                fs::write(&tmp, text)?;
                fs::rename(&tmp, &path)?;
            }
        }
        let mut d = self.dir.clone();
        for s in &rel {
            d.push(s);
        }
        fs::create_dir_all(&d)?;
        Ok(d)
    }

    /// MailboxArchive.save_message: письмо — в .eml/.eml.gz, флаги — в flags.txt.
    #[allow(clippy::too_many_arguments)]
    fn save(
        &self,
        fdir: &Path,
        uv: &str,
        uid: u64,
        body: &[u8],
        flags: &str,
        idate: Option<i64>,
        compress: bool,
    ) -> io::Result<()> {
        // сервер не дал INTERNALDATE: дата из заголовков, а не момент загрузки
        let ts = idate.or_else(|| crate::mail::received_from_bytes(body));
        let name = format!("{uv}_{uid}");
        write_message(&fdir.join(&name), body, compress, ts)?;
        let _g = lock(&self.flags_lock);
        let mut fh = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(fdir.join("flags.txt"))?;
        fh.write_all(format!("{name}\t{flags}\n").as_bytes())
    }
}

/// msgfile.write_message: атомарно записать письмо и поставить файлу дату получения.
fn write_message(base: &Path, body: &[u8], compress: bool, mtime: Option<i64>) -> io::Result<()> {
    let ext = if compress { ".eml.gz" } else { ".eml" };
    let mut final_name = base.as_os_str().to_owned();
    final_name.push(ext);
    let final_path = PathBuf::from(final_name);
    let mut tmp = final_path.as_os_str().to_owned();
    tmp.push(".part");
    let tmp = PathBuf::from(tmp);
    {
        let mut fh = fs::File::create(&tmp)?;
        if compress {
            let mut gz = flate2::GzBuilder::new()
                .mtime(0)
                .write(&mut fh, flate2::Compression::new(6));
            gz.write_all(body)?;
            gz.finish()?;
        } else {
            fh.write_all(body)?;
        }
        if let Some(ts) = mtime {
            let t = if ts >= 0 {
                SystemTime::UNIX_EPOCH.checked_add(Duration::from_secs(ts as u64))
            } else {
                SystemTime::UNIX_EPOCH.checked_sub(Duration::from_secs(ts.unsigned_abs()))
            };
            if let Some(t) = t {
                let _ = fh.set_times(fs::FileTimes::new().set_accessed(t).set_modified(t));
            }
        }
    }
    fs::rename(&tmp, &final_path)
}

/// msgfile.existing_keys: «uidvalidity_uid» писем, уже лежащих в папке.
fn existing_keys(fdir: &Path) -> HashSet<String> {
    let mut out = HashSet::new();
    let Ok(rd) = fs::read_dir(fdir) else {
        return out;
    };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let low = name.to_lowercase();
        let cut = if low.ends_with(".eml.gz") {
            7
        } else if low.ends_with(".eml") {
            4
        } else {
            continue;
        };
        let is_file = match e.file_type() {
            Ok(ft) if ft.is_symlink() => fs::metadata(e.path()).is_ok_and(|m| m.is_file()),
            Ok(ft) => ft.is_file(),
            Err(_) => false,
        };
        if is_file && name.is_char_boundary(name.len() - cut) {
            out.insert(name[..name.len() - cut].to_string());
        }
    }
    out
}

// ---------------------------------------------------------------- очередь

/// Режет список UID на пачки по числу писем и суммарному размеру.
fn make_batches(uids: &[u64], sizes: &HashMap<u64, u64>) -> Vec<Vec<u64>> {
    let mut out = Vec::new();
    let (mut batch, mut total) = (Vec::new(), 0u64);
    for &u in uids {
        let z = sizes.get(&u).copied().unwrap_or(UNKNOWN_SIZE);
        if !batch.is_empty() && (batch.len() >= BATCH_COUNT || total + z > BATCH_BYTES) {
            out.push(std::mem::take(&mut batch));
            total = 0;
        }
        batch.push(u);
        total += z;
    }
    if !batch.is_empty() {
        out.push(batch);
    }
    out
}

type Meta = HashMap<u64, (u64, String)>;

fn key_of(m: Option<&(u64, String)>) -> Option<Key> {
    m.filter(|x| !x.1.is_empty()).map(|x| (x.1.clone(), x.0))
}

struct Job {
    folder: Arc<Folder>,
    uv: String,
    fdir: Arc<PathBuf>,
    batch: Vec<u64>,
    meta: Arc<Meta>,
    done: HashSet<u64>,
    /// Пачка «Вся почта»: ждёт, пока докачаются обычные папки.
    last: bool,
    /// Ключи повторов, занятые письмами этой пачки.
    claimed: HashMap<u64, Key>,
}

struct Plan {
    folder: Arc<Folder>,
    uv: String,
    fdir: Arc<PathBuf>,
    todo: Vec<u64>,
}

#[derive(Default)]
struct State {
    queue: VecDeque<Job>,
    /// Пачек обычных папок не закончено («Вся почта» ждёт их).
    pending: usize,
    preparing: bool,
    /// Дополнительных соединений ещё работает.
    alive: usize,
    known: HashSet<Key>,
    counts: Counts,
    duplicates: u64,
    done: u64,
    total: u64,
    uv_logged: HashSet<String>,
    failures: Vec<Error>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

// ---------------------------------------------------------------- соединение

/// Одно соединение с сервером и папка, открытая в нём (_Conn в Python-версии).
struct Link<'e, 'h> {
    e: &'e Engine<'h>,
    n: usize,
    imap: Option<Imap>,
    folder: Option<Arc<Folder>>,
}

impl<'e, 'h> Link<'e, 'h> {
    fn new(e: &'e Engine<'h>, n: usize) -> Self {
        Link {
            e,
            n,
            imap: None,
            folder: None,
        }
    }

    fn connect(&mut self) -> Result<()> {
        if let Some(i) = self.imap.take() {
            i.logout();
        }
        self.imap = Some(Imap::connect(&self.e.acc.server, self.e.stop.clone())?);
        Ok(())
    }

    fn close(&mut self) {
        if let Some(i) = self.imap.take() {
            i.logout();
        }
    }

    fn imap(&mut self) -> Result<&mut Imap> {
        self.imap
            .as_mut()
            .ok_or_else(|| Error::Conn("not connected".into()))
    }

    fn log(&self, text: &str) {
        if self.n == 1 {
            self.e.host.log(text);
        } else {
            self.e.host.log(&format!("[{}] {text}", self.n));
        }
    }

    fn has_folder(&self, f: &Arc<Folder>) -> bool {
        self.folder.as_ref().is_some_and(|x| Arc::ptr_eq(x, f))
    }

    fn reconnect(&mut self) -> Result<bool> {
        for attempt in 1..=MAX_AUTO_RECONNECTS {
            self.e.check_stop()?;
            self.log(&fmt_args(
                &t("Переподключение (попытка {attempt}/{MAX_AUTO_RECONNECTS})…"),
                &[
                    ("attempt", &attempt),
                    ("MAX_AUTO_RECONNECTS", &MAX_AUTO_RECONNECTS),
                ],
            ));
            let r = self.connect().and_then(|_| match self.folder.clone() {
                Some(f) => self.imap()?.examine(&f).map(|_| ()),
                None => Ok(()),
            });
            match r {
                Ok(()) => {
                    self.log(&t("Соединение восстановлено"));
                    return Ok(true);
                }
                Err(Error::Stop) => return Err(Error::Stop),
                Err(e) => {
                    self.log(&fmt_args(
                        &t("Не удалось переподключиться: {e}"),
                        &[("e", &e)],
                    ));
                    if self.e.sleep((5 * attempt).min(15)) {
                        return Err(Error::Stop);
                    }
                }
            }
        }
        Ok(false)
    }

    /// Выполнить f; при обрыве связи переподключиться и повторить один раз.
    fn with_reconnect<T>(&mut self, mut f: impl FnMut(&mut Imap) -> Result<T>) -> Result<T> {
        match self.imap().and_then(&mut f) {
            Err(e) if e.is_connection() => {
                self.log(&fmt_args(&t("Обрыв соединения: {e}"), &[("e", &e)]));
                if !self.reconnect()? {
                    return Err(e);
                }
                self.imap().and_then(f)
            }
            r => r,
        }
    }

    /// Открыть папку (с переподключением); вернуть UIDVALIDITY.
    fn open(&mut self, f: &Arc<Folder>) -> Result<String> {
        self.folder = Some(f.clone());
        self.with_reconnect(|i| i.examine(f))
    }
}

// ---------------------------------------------------------------- загрузчик

struct Engine<'h> {
    host: &'h dyn Host,
    acc: &'h Account,
    stop: Arc<Stopper>,
    archive: Archive,
    st: Mutex<State>,
    cv: Condvar,
    /// Счётчики и прогресс уходят наружу по порядку.
    emit: Mutex<()>,
    skip_all: AtomicBool,
    /// Качать пачками, пока сервер не откажет.
    batches: AtomicBool,
    dedup: AtomicBool,
    archive_known: Mutex<Arc<HashSet<Key>>>,
}

impl<'h> Engine<'h> {
    fn stopped(&self) -> bool {
        self.stop.is_set()
    }

    fn check_stop(&self) -> Result<()> {
        if self.stopped() {
            Err(Error::Stop)
        } else {
            Ok(())
        }
    }

    /// Пауза; true — если за это время нажали «Стоп».
    fn sleep(&self, secs: u64) -> bool {
        for _ in 0..secs * 10 {
            if self.stopped() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        self.stopped()
    }

    fn state(&self) -> MutexGuard<'_, State> {
        lock(&self.st)
    }

    fn num(&self, n: usize) -> String {
        let s = n.to_string();
        let mut out = String::new();
        for (i, c) in s.chars().enumerate() {
            if i > 0 && (s.len() - i).is_multiple_of(3) {
                out.push_str(&self.acc.thousands);
            }
            out.push(c);
        }
        out
    }

    fn add(&self, downloaded: u64, errors: u64, done: u64) {
        let _g = lock(&self.emit);
        let (c, d, total) = {
            let mut st = self.state();
            st.counts.downloaded += downloaded;
            st.counts.errors += errors;
            st.done += done;
            (st.counts, st.done, st.total)
        };
        if downloaded > 0 || errors > 0 {
            self.host.counters(c.downloaded, c.already, c.errors);
        }
        if done > 0 {
            self.host.progress(d, total);
        }
    }

    fn counters(&self) {
        let _g = lock(&self.emit);
        let c = self.state().counts;
        self.host.counters(c.downloaded, c.already, c.errors);
    }

    fn run(&self, main: &mut Link<'_, 'h>) -> Result<()> {
        let a = self.acc;
        self.host.status(&t("Подключение"));
        self.host.log(&fmt_args(
            &t("Подключение к {0}:{1} ({2})"),
            &[
                ("0", &a.server.host),
                ("1", &a.server.port),
                ("2", &a.security_text),
            ],
        ));
        main.connect()?;
        self.host.log(&t("Вход выполнен"));

        let mut folders: Vec<Folder> = main
            .imap()?
            .list_folders()?
            .into_iter()
            .filter(Folder::selectable)
            .collect();
        // Повторы между папками бывают там, где письмо лежит в нескольких папках сразу —
        // в Gmail («Вся почта» и ярлыки). «Автоматически» ищет их только в таких ящиках.
        let dedup = a.duplicates == "always"
            || (a.duplicates != "never" && folders.iter().any(is_all_mail));
        self.dedup.store(dedup, Ordering::Relaxed);
        if a.duplicates != "always" && a.duplicates != "never" && !dedup {
            self.host.log(&t(
                "Повторы писем не ищутся: в ящике нет папки «Вся почта», письмо лежит только в одной папке",
            ));
        }
        if a.folder_mode == "one" {
            folders.retain(|f| f.raw == a.folder_raw);
            if folders.is_empty() {
                let name = if a.folder_display.is_empty() {
                    &a.folder_raw
                } else {
                    &a.folder_display
                };
                return Err(Error::Imap(fmt_args(
                    &t("Папка «{0}» не найдена на сервере"),
                    &[("0", name)],
                )));
            }
        }
        let range = a.date_mode == "range";
        let since = range.then(|| imap_date(&a.date_from, 0)).flatten();
        let before = range.then(|| imap_date(&a.date_to, 1)).flatten(); // BEFORE не включает день

        // 1. Анализ: сколько писем и какие ещё не скачаны
        self.host.status(&t("Анализ папок"));
        let mut plan = Vec::new();
        for f in folders {
            self.check_stop()?;
            let f = Arc::new(f);
            let found = main.open(&f).and_then(|uv| {
                let uids =
                    main.with_reconnect(|i| i.search(since.as_deref(), before.as_deref()))?;
                Ok((uv, uids))
            });
            let (uv, uids) = match found {
                Ok(x) => x,
                Err(Error::Stop) => return Err(Error::Stop),
                Err(e) => {
                    self.add(0, 1, 0);
                    self.host.log(&fmt_args(
                        &t("Папка «{0}» пропущена: {e}"),
                        &[("0", &f.display), ("e", &e)],
                    ));
                    continue;
                }
            };
            let fdir = self
                .archive
                .folder_dir(&f)
                .map_err(|e| Error::Io(e.to_string()))?;
            let have = existing_keys(&fdir);
            let todo: Vec<u64> = uids
                .iter()
                .copied()
                .filter(|u| !have.contains(&format!("{uv}_{u}")))
                .collect();
            self.state().counts.already += (uids.len() - todo.len()) as u64;
            self.host.log(&fmt_args(
                &t("«{0}»: писем {1}, новых {2}"),
                &[("0", &f.display), ("1", &uids.len()), ("2", &todo.len())],
            ));
            if !todo.is_empty() {
                plan.push(Plan {
                    folder: f,
                    uv,
                    fdir: Arc::new(fdir),
                    todo,
                });
            }
        }
        self.counters();

        // 2. Подготовка и загрузка идут вместе. «Вся почта» Gmail — последней: её письма почти
        // все есть в других папках и с пропуском повторов остаются там, где лежат по ярлыкам.
        plan.sort_by_key(|p| is_all_mail(&p.folder));
        if dedup && !plan.is_empty() {
            self.host.status(&t("Поиск повторов в архиве"));
            let keys = Arc::new(self.host.archive_keys());
            self.check_stop()?;
            self.state().known = (*keys).clone();
            *lock(&self.archive_known) = keys;
        }
        let total = plan.iter().map(|p| p.todo.len() as u64).sum();
        self.state().total = total;
        self.host.progress(0, total);
        if !plan.is_empty() {
            self.download(main, &plan)?;
        }
        let dups = self.state().duplicates;
        if dups > 0 {
            self.host.log(&fmt_args(
                &t("Повторов писем не загружено: {0}"),
                &[("0", &dups)],
            ));
        }
        Ok(())
    }

    /// Готовит папки частями по PREP_CHUNK писем и кладёт пачки в очередь. После каждой
    /// части зовёт after — при одном соединении оно тут же качает эти пачки.
    fn prepare(
        &self,
        m: &mut Link<'_, 'h>,
        plan: &[Plan],
        after: &mut dyn FnMut(&mut Link<'_, 'h>),
    ) -> Result<()> {
        let dedup = self.dedup.load(Ordering::Relaxed);
        let archive_known = lock(&self.archive_known).clone();
        let mut no_meta = HashSet::new();
        let mut no_sizes = HashSet::new();
        for p in plan {
            let f = &p.folder;
            let last = dedup && is_all_mail(f);
            let len = p.todo.len();
            let mut i = 0;
            while i < len {
                self.check_stop()?;
                let end = (i + PREP_CHUNK).min(len);
                self.host.status(&fmt_args(
                    &t("Подготовка: {0} ({1} из {2})"),
                    &[
                        ("0", &f.display),
                        ("1", &self.num(end)),
                        ("2", &self.num(len)),
                    ],
                ));
                if !m.has_folder(f) {
                    match m.open(f) {
                        Ok(uv) if uv == p.uv => {}
                        Ok(_) => {
                            self.host.log(&fmt_args(
                                &t("«{0}»: сервер сменил UIDVALIDITY, папка будет перекачана при следующем запуске"),
                                &[("0", &f.display)],
                            ));
                            self.add(0, 0, (len - i) as u64);
                            break;
                        }
                        Err(Error::Stop) => return Err(Error::Stop),
                        Err(e) => {
                            self.host.log(&fmt_args(
                                &t("Папка «{0}» пропущена: {e}"),
                                &[("0", &f.display), ("e", &e)],
                            ));
                            self.add(0, 1, (len - i) as u64);
                            break;
                        }
                    }
                }
                let mut part = p.todo[i..end].to_vec();
                let (meta, sizes) =
                    self.meta_and_sizes(m, f, &part, dedup, &mut no_meta, &mut no_sizes)?;
                if !meta.is_empty() {
                    // с архивом сверяемся здесь; повторы среди писем этого запуска ловит загрузка
                    let before = part.len();
                    part.retain(|u| {
                        !key_of(meta.get(u)).is_some_and(|k| archive_known.contains(&k))
                    });
                    let dups = (before - part.len()) as u64;
                    if dups > 0 {
                        self.state().duplicates += dups;
                        self.add(0, 0, dups);
                        self.host.log(&fmt_args(
                            &t("«{0}»: {1} писем уже есть в других папках — не загружаются"),
                            &[("0", &f.display), ("1", &dups)],
                        ));
                    }
                }
                let job_last = last && !meta.is_empty();
                let meta = Arc::new(meta);
                let jobs: Vec<Job> = make_batches(&part, &sizes)
                    .into_iter()
                    .map(|batch| Job {
                        folder: f.clone(),
                        uv: p.uv.clone(),
                        fdir: p.fdir.clone(),
                        batch,
                        meta: meta.clone(),
                        done: HashSet::new(),
                        last: job_last,
                        claimed: HashMap::new(),
                    })
                    .collect();
                {
                    let mut st = self.state();
                    if !job_last {
                        st.pending += jobs.len();
                    }
                    st.queue.extend(jobs);
                    self.cv.notify_all();
                }
                after(m);
                i = end;
            }
        }
        Ok(())
    }

    /// (Message-ID с размерами — для поиска повторов; размеры — для пачек). Если сервер
    /// отказал, в этой папке больше не спрашиваем (и пишем в журнал один раз).
    fn meta_and_sizes(
        &self,
        m: &mut Link<'_, 'h>,
        f: &Folder,
        part: &[u64],
        dedup: bool,
        no_meta: &mut HashSet<String>,
        no_sizes: &mut HashSet<String>,
    ) -> Result<(Meta, HashMap<u64, u64>)> {
        let mut meta = Meta::new();
        let mut sizes = None;
        if dedup && !no_meta.contains(&f.raw) {
            match m.with_reconnect(|i| i.fetch_meta(part)) {
                Ok(x) => {
                    sizes = Some(x.iter().map(|(&u, v)| (u, v.0)).collect());
                    meta = x;
                }
                Err(Error::Stop) => return Err(Error::Stop),
                Err(e) => {
                    no_meta.insert(f.raw.clone());
                    self.host.log(&fmt_args(
                        &t("«{0}»: сервер не выдал Message-ID писем ({e}), повторы в этой папке не ищутся"),
                        &[("0", &f.display), ("e", &e)],
                    ));
                }
            }
        }
        if sizes.is_none() && !no_sizes.contains(&f.raw) {
            match m.with_reconnect(|i| i.fetch_sizes(part)) {
                Ok(x) => sizes = Some(x),
                Err(Error::Stop) => return Err(Error::Stop),
                Err(e) => {
                    no_sizes.insert(f.raw.clone());
                    self.host.log(&fmt_args(
                        &t("«{0}»: сервер не сообщил размеры писем ({e}), качаю небольшими пачками"),
                        &[("0", &f.display), ("e", &e)],
                    ));
                }
            }
        }
        Ok((meta, sizes.unwrap_or_default()))
    }

    // ---------- раздача пачек соединениям ----------

    fn download(&self, main: &mut Link<'_, 'h>, plan: &[Plan]) -> Result<()> {
        let total = self.state().total as usize;
        let batches = total.div_ceil(BATCH_COUNT); // примерно, пачек не меньше
        let n = self
            .acc
            .connections
            .clamp(1, MAX_CONNECTIONS)
            .min(batches)
            .max(1);
        {
            let mut st = self.state();
            st.pending = 0;
            st.preparing = true;
            st.alive = n - 1;
        }
        if n > 1 {
            self.host
                .log(&fmt_args(&t("Загрузка в {0} соединениях"), &[("0", &n)]));
        }
        let prepared = std::thread::scope(|s| {
            for i in 2..=n {
                s.spawn(move || {
                    let mut c = Link::new(self, i);
                    match c.connect() {
                        Ok(()) => {
                            self.worker(&mut c, true, n);
                        }
                        Err(e) => {
                            if !self.stopped() {
                                self.host.log(&fmt_args(
                                    &t("Соединение {0} не открылось ({e}) — работают остальные"),
                                    &[("0", &i), ("e", &e)],
                                ));
                            }
                        }
                    }
                    c.close();
                    let mut st = self.state();
                    st.alive -= 1;
                    self.cv.notify_all();
                });
            }
            // первое соединение готовит папки; пока других нет (или все выбыли), оно же и качает
            let mut main_ok = true;
            let prepared = self.prepare(main, plan, &mut |m| {
                let drain = self.state().alive == 0;
                if drain && main_ok {
                    main_ok = self.worker(m, false, n);
                }
            });
            {
                let mut st = self.state();
                st.preparing = false;
                self.cv.notify_all();
            }
            if main_ok && prepared.is_ok() {
                self.worker(main, true, n);
            }
            prepared
        });
        prepared?;
        self.check_stop()?;
        let mut st = self.state();
        if !st.queue.is_empty() {
            // пачки остались: все соединения выбыли с ошибкой
            return Err(st
                .failures
                .pop()
                .unwrap_or_else(|| Error::Imap(t("Нет соединения с сервером"))));
        }
        Ok(())
    }

    /// Качать пачки из очереди; false — соединение выбыло с ошибкой.
    fn worker(&self, c: &mut Link<'_, 'h>, wait: bool, n: usize) -> bool {
        match self.work(c, wait) {
            Ok(()) | Err(Error::Stop) => true,
            Err(e) => {
                if n > 1 {
                    self.host.log(&fmt_args(
                        &t("Соединение {0} закрыто из-за ошибки: {e}"),
                        &[("0", &c.n), ("e", &e)],
                    ));
                }
                self.state().failures.push(e);
                false
            }
        }
    }

    /// Следующая пачка. Пачки «Вся почта» ждут, пока докачаются обычные папки.
    /// wait=false — не ждать новых пачек (первое соединение, пока оно готовит папки).
    fn next_job(&self, wait: bool) -> Result<Option<Job>> {
        let mut st = self.state();
        loop {
            self.check_stop()?;
            if let Some(j) = st.queue.front()
                && !(j.last && st.pending > 0)
            {
                return Ok(st.queue.pop_front());
            }
            if !wait || !(!st.queue.is_empty() || st.preparing) {
                return Ok(None);
            }
            st = self
                .cv
                .wait_timeout(st, Duration::from_millis(300))
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }

    fn work(&self, c: &mut Link<'_, 'h>, wait: bool) -> Result<()> {
        while let Some(mut job) = self.next_job(wait)? {
            let r = catch_unwind(AssertUnwindSafe(|| self.run_job(c, &mut job)))
                .unwrap_or_else(|_| Err(Error::Imap(t("внутренняя ошибка загрузки"))));
            match r {
                Ok(()) => {
                    if !job.last {
                        let mut st = self.state();
                        st.pending -= 1;
                        self.cv.notify_all();
                    }
                }
                Err(Error::Stop) => return Err(Error::Stop),
                Err(e) => {
                    // соединение выбывает, пачку доделают другие (уже скачанное не повторяется)
                    let mut st = self.state();
                    let rest: Vec<u64> = job
                        .batch
                        .iter()
                        .copied()
                        .filter(|u| !job.done.contains(u))
                        .collect();
                    for u in &rest {
                        if let Some(k) = job.claimed.remove(u) {
                            st.known.remove(&k);
                        }
                    }
                    job.batch = rest;
                    st.queue.push_front(job);
                    self.cv.notify_all();
                    return Err(e);
                }
            }
        }
        Ok(())
    }

    /// Письмо обработано (сохранено или пропущено).
    fn finish(&self, job: &mut Job, uid: u64, saved: bool) {
        job.done.insert(uid);
        if !saved && let Some(k) = job.claimed.remove(&uid) {
            // письмо не скачалось — его копию в другой папке можно качать
            self.state().known.remove(&k);
        }
        self.add(0, 0, 1);
    }

    fn run_job(&self, c: &mut Link<'_, 'h>, job: &mut Job) -> Result<()> {
        let f = job.folder.clone();
        if !c.has_folder(&f) {
            self.host
                .status(&fmt_args(&t("Загрузка: {0}"), &[("0", &f.display)]));
            let uv = c.open(&f)?;
            if uv != job.uv {
                let first = self.state().uv_logged.insert(f.raw.clone());
                if first {
                    c.log(&fmt_args(
                        &t("«{0}»: сервер сменил UIDVALIDITY, папка будет перекачана при следующем запуске"),
                        &[("0", &f.display)],
                    ));
                }
                let rest: Vec<u64> = job
                    .batch
                    .iter()
                    .copied()
                    .filter(|u| !job.done.contains(u))
                    .collect();
                job.done.extend(&rest);
                self.add(0, 0, rest.len() as u64);
                return Ok(());
            }
        }
        // повторы среди писем этого запуска: ключ занимает первое взявшееся соединение
        let mut batch = Vec::new();
        let mut dups = 0;
        {
            let mut st = self.state();
            for &u in &job.batch {
                if job.done.contains(&u) {
                    continue;
                }
                let k = key_of(job.meta.get(&u));
                if let Some(k) = k {
                    if st.known.contains(&k) {
                        job.done.insert(u);
                        dups += 1;
                        continue;
                    }
                    st.known.insert(k.clone());
                    job.claimed.insert(u, k);
                }
                batch.push(u);
            }
            st.duplicates += dups;
        }
        if dups > 0 {
            self.add(0, 0, dups);
        }
        self.download_batch(c, job, &batch)
    }

    /// Качает пачку одной командой, сохраняя письма по мере прихода. Письма, которые не
    /// пришли или не сохранились, докачиваются по одному — с переподключением и вопросом.
    fn download_batch(&self, c: &mut Link<'_, 'h>, job: &mut Job, batch: &[u64]) -> Result<()> {
        let mut got: HashSet<u64> = HashSet::new();
        if batch.len() > 1 && self.batches.load(Ordering::Relaxed) {
            let want: HashSet<u64> = batch.iter().copied().collect();
            let r = c.with_reconnect(|imap| {
                let left: Vec<u64> = batch.iter().copied().filter(|u| !got.contains(u)).collect();
                if left.is_empty() {
                    return Ok(());
                }
                imap.fetch_many(&left, &mut |uid, body, flags, idate| {
                    if !want.contains(&uid) || got.contains(&uid) {
                        return Ok(());
                    }
                    let saved = self.archive.save(
                        &job.fdir,
                        &job.uv,
                        uid,
                        body,
                        &flags,
                        idate,
                        self.acc.compress,
                    );
                    if saved.is_ok() {
                        got.insert(uid);
                        self.add(1, 0, 0);
                        self.finish(job, uid, true);
                    }
                    self.check_stop()
                })
            });
            match r {
                Ok(()) => {}
                Err(Error::Stop) => return Err(Error::Stop),
                Err(e) if !e.is_connection() => {
                    // сервер не понимает FETCH нескольких писем — дальше по одному
                    if self.batches.swap(false, Ordering::Relaxed) {
                        c.log(&fmt_args(
                            &t("Сервер не выдаёт письма пачками ({e}), качаю по одному"),
                            &[("e", &e)],
                        ));
                    }
                }
                Err(_) => {} // связь не восстановилась — по одному, там свои переподключения
            }
        }
        for &uid in batch {
            if got.contains(&uid) {
                continue;
            }
            let saved = self.download_one(c, job, uid)?;
            self.finish(job, uid, saved);
        }
        Ok(())
    }

    fn download_one(&self, c: &mut Link<'_, 'h>, job: &Job, uid: u64) -> Result<bool> {
        let mut auto = 0;
        loop {
            self.check_stop()?;
            let r = c
                .imap()
                .and_then(|i| i.fetch_one(uid))
                .and_then(|(body, flags, idate)| {
                    self.archive
                        .save(
                            &job.fdir,
                            &job.uv,
                            uid,
                            &body,
                            &flags,
                            idate,
                            self.acc.compress,
                        )
                        .map_err(|e| Error::Io(e.to_string()))
                });
            let e = match r {
                Ok(()) => {
                    self.add(1, 0, 0);
                    return Ok(true);
                }
                Err(Error::Stop) => return Err(Error::Stop),
                Err(e) => e,
            };
            let conn_err = e.is_connection();
            if conn_err && auto < MAX_AUTO_RECONNECTS {
                auto += 1;
                c.log(&fmt_args(
                    &t("UID {uid}: обрыв соединения ({e})"),
                    &[("uid", &uid), ("e", &e)],
                ));
                if c.reconnect()? {
                    continue;
                }
            }
            let answer = if self.skip_all.load(Ordering::Relaxed) {
                Answer::Skip
            } else {
                let error = format!("{}: {e}", e.kind());
                let details = format!("{error}\nUID {uid}, {}", job.folder.raw);
                self.host.ask(&job.folder.display, uid, &error, &details)
            };
            match answer {
                Answer::Stop => {
                    // «Остановить ящик» — для всех его соединений
                    self.host.request_stop();
                    self.stop.set();
                    return Err(Error::Stop);
                }
                Answer::Retry => {
                    auto = 0;
                    if conn_err {
                        c.reconnect()?;
                    }
                    continue;
                }
                Answer::SkipAll => self.skip_all.store(true, Ordering::Relaxed),
                Answer::Skip => {}
            }
            self.add(0, 1, 0);
            c.log(&fmt_args(
                &t("Письмо UID {uid} в «{0}» пропущено: {e}"),
                &[("0", &job.folder.display), ("uid", &uid), ("e", &e)],
            ));
            return Ok(false);
        }
    }
}

/// Загрузить ящик. Возвращает счётчики и итог: Ok, или ошибку (Stop — остановлено).
pub fn download(host: &dyn Host, acc: &Account) -> (Counts, Result<()>) {
    let stop = Arc::new(Stopper::default());
    if host.stopped() {
        stop.set();
    }
    let engine = Engine {
        host,
        acc,
        stop: stop.clone(),
        archive: Archive {
            dir: acc.mailbox_dir.clone(),
            meta_lock: Mutex::new(()),
            flags_lock: Mutex::new(()),
        },
        st: Mutex::new(State::default()),
        cv: Condvar::new(),
        emit: Mutex::new(()),
        skip_all: AtomicBool::new(false),
        batches: AtomicBool::new(true),
        dedup: AtomicBool::new(false),
        archive_known: Mutex::new(Arc::new(HashSet::new())),
    };
    let finished = AtomicBool::new(false);
    let result = std::thread::scope(|s| {
        // «Стоп» нажимают в Python: флаг события переносится сюда, его видят все соединения
        s.spawn(|| {
            while !finished.load(Ordering::Relaxed) {
                if host.stopped() {
                    stop.set();
                    break;
                }
                std::thread::sleep(Duration::from_millis(150));
            }
        });
        let mut main = Link::new(&engine, 1);
        let r = catch_unwind(AssertUnwindSafe(|| engine.run(&mut main)))
            .unwrap_or_else(|_| Err(Error::Imap(t("внутренняя ошибка загрузки"))));
        main.close();
        finished.store(true, Ordering::Relaxed);
        r
    });
    let counts = engine.state().counts;
    let result = match result {
        Err(_) if engine.stopped() => Err(Error::Stop),
        r => r,
    };
    (counts, result)
}

/// Список папок сервера (для окна ящика).
pub fn list_folders(srv: &Server) -> Result<Vec<Folder>> {
    let mut imap = Imap::connect(srv, Arc::new(Stopper::default()))?;
    let r = imap.list_folders();
    imap.logout();
    r
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names() {
        assert_eq!(safe_name(" a:b. ", 60), "a_b");
        assert_eq!(safe_name("con", 60), "_con");
        assert_eq!(safe_name("..", 60), "_");
        assert_eq!(safe_name("абвгд", 3), "абв");
    }

    #[test]
    fn batches() {
        let sizes: HashMap<u64, u64> = [(1, 10 << 20), (2, 10 << 20), (3, 1)].into();
        assert_eq!(make_batches(&[1, 2, 3], &sizes), vec![vec![1], vec![2, 3]]);
        let many: Vec<u64> = (1..=250).collect();
        assert_eq!(make_batches(&many, &HashMap::new()).len(), 250 / 16 + 1);
    }
}
