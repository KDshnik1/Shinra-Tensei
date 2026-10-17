//! Нативное ядро Shinra Tensei: запись PST и разбор писем на Rust.
//!
//! Python-код подключает модуль, если он собран (`import shinra_core`), и
//! иначе работает на чистом Python. Тяжёлая работа идёт без GIL, поэтому
//! несколько экспортов в разных потоках действительно выполняются параллельно.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::Mutex;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyKeyError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

mod addr;
mod date;
mod mail;
mod pst;
mod reader;
mod text;

create_exception!(
    shinra_core,
    PstCheckError,
    PyException,
    "Файл PST не прошёл самопроверку."
);

use pst::{Message, Props, PstError, Val, Writer};

// ---------------------------------------------------------------- перевод сообщений

static TRANSLATOR: Mutex<Option<Py<PyAny>>> = Mutex::new(None);

/// Задать функцию перевода (core.i18n.tr): сообщения об ошибках идут через неё.
#[pyfunction]
fn set_translator(f: Py<PyAny>) {
    *TRANSLATOR.lock().unwrap() = Some(f);
}

fn tr(py: Python<'_>, text: &str) -> String {
    let f = TRANSLATOR.lock().unwrap().as_ref().map(|f| f.clone_ref(py));
    if let Some(f) = f
        && let Ok(s) = f.call1(py, (text,)).and_then(|r| r.extract::<String>(py))
    {
        return s;
    }
    text.to_string()
}

fn to_pyerr(py: Python<'_>, e: PstError) -> PyErr {
    match e {
        PstError::Io(e) => PyOSError::new_err(e.to_string()),
        PstError::Unsupported(t) => PyValueError::new_err(
            tr(py, "тип свойства 0x{ptype:04X} не поддерживается")
                .replace("{ptype:04X}", &format!("{t:04X}")),
        ),
        PstError::TooLarge => PyValueError::new_err(tr(py, "слишком большой объект для PST")),
        PstError::NoFolder(n) => PyKeyError::new_err(n),
        PstError::Internal(t) => PyRuntimeError::new_err(tr(py, t)),
    }
}

const PARSE_PANIC: &str = "не удалось разобрать письмо (внутренняя ошибка)";
const WRITE_PANIC: &str = "внутренняя ошибка при записи письма";

/// Паника в Rust не должна ронять экспорт: PyO3 превращает её в PanicException
/// (наследник BaseException, его не ловит `except Exception`), поэтому здесь она
/// становится обычной ошибкой — письмо считается ошибочным, экспорт идёт дальше.
fn guard<T>(what: &'static str, f: impl FnOnce() -> pst::Result<T>) -> pst::Result<T> {
    catch_unwind(AssertUnwindSafe(f)).unwrap_or(Err(PstError::Internal(what)))
}

/// str Python -> String; одиночные суррогаты (их нет в UTF-8) заменяются на «?».
fn py_string(v: &Bound<'_, PyAny>) -> PyResult<String> {
    let s = v.str()?;
    match s.to_str() {
        Ok(t) => Ok(t.to_string()),
        Err(_) => {
            let b: Vec<u8> = s.call_method1("encode", ("utf-8", "replace"))?.extract()?;
            Ok(String::from_utf8_lossy(&b).into_owned())
        }
    }
}

// ---------------------------------------------------------------- dict Python -> свойства

fn val_from_py(tag: u32, v: &Bound<'_, PyAny>) -> PyResult<Option<Val>> {
    if v.is_none() {
        return Ok(None);
    }
    let ptype = (tag & 0xFFFF) as u16;
    Ok(Some(match ptype {
        pst::PT_STR => Val::Str(py_string(v)?),
        pst::PT_BIN => Val::Bin(v.extract::<Vec<u8>>()?),
        pst::PT_BOOL => Val::Bool(v.is_truthy()?),
        pst::PT_I2 | pst::PT_I4 | pst::PT_I8 | pst::PT_TIME => {
            let i = v.py().get_type::<pyo3::types::PyInt>().call1((v,))?;
            Val::Int(i.extract::<i64>()?)
        }
        _ => {
            return Err(PyValueError::new_err(
                tr(v.py(), "тип свойства 0x{ptype:04X} не поддерживается")
                    .replace("{ptype:04X}", &format!("{ptype:04X}")),
            ));
        }
    }))
}

fn props_from_py(d: &Bound<'_, PyAny>) -> PyResult<Props> {
    let d = d.downcast::<PyDict>()?;
    let mut out = Vec::with_capacity(d.len());
    for (k, v) in d.iter() {
        let tag: u32 = k.extract()?;
        out.push((tag, val_from_py(tag, &v)?));
    }
    Ok(out)
}

fn message_from_py(msg: &Bound<'_, PyDict>) -> PyResult<Message> {
    let props = props_from_py(&msg.as_any().get_item("props")?)?;
    let mut recipients = Vec::new();
    if let Some(r) = msg.get_item("recipients")?
        && r.is_truthy()?
    {
        for it in r.try_iter()? {
            recipients.push(props_from_py(&it?)?);
        }
    }
    let mut attachments = Vec::new();
    if let Some(a) = msg.get_item("attachments")?
        && a.is_truthy()?
    {
        for it in a.try_iter()? {
            let it = it?;
            attachments.push((
                props_from_py(&it.get_item("props")?)?,
                props_from_py(&it.get_item("row")?)?,
            ));
        }
    }
    let read = match msg.get_item("read")? {
        Some(r) => r.is_truthy()?,
        None => false,
    };
    Ok(Message {
        props,
        recipients,
        attachments,
        read,
        received: 0,
    })
}

fn val_to_py(py: Python<'_>, v: &Option<Val>) -> PyObject {
    match v {
        None => py.None(),
        Some(Val::Int(i)) => i.into_pyobject(py).unwrap().into_any().unbind(),
        Some(Val::Bool(b)) => b.into_pyobject(py).unwrap().to_owned().into_any().unbind(),
        Some(Val::Str(s)) => s.into_pyobject(py).unwrap().into_any().unbind(),
        Some(Val::Bin(b)) => PyBytes::new(py, b).into_any().unbind(),
    }
}

fn props_to_py<'py>(py: Python<'py>, p: &Props) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (t, v) in p {
        d.set_item(t, val_to_py(py, v))?;
    }
    Ok(d)
}

// ---------------------------------------------------------------- PstFile

/// Тот же интерфейс, что у core.pst_writer.PstFile, плюс add_eml().
#[pyclass(module = "shinra_core")]
struct PstFile {
    w: Option<Writer>,
    path: String,
    /// close() прошёл успешно: abort() готовый файл не удаляет.
    closed: bool,
}

impl PstFile {
    fn writer(&mut self) -> PyResult<&mut Writer> {
        self.w
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("PST file is closed"))
    }
}

#[pymethods]
impl PstFile {
    #[new]
    #[pyo3(signature = (path, display_name, password = "", uid = None))]
    fn new(
        py: Python<'_>,
        path: PathBuf,
        display_name: &Bound<'_, PyAny>,
        password: &str,
        uid: Option<Vec<u8>>,
    ) -> PyResult<Self> {
        let display_name = py_string(display_name)?;
        let uid: [u8; 16] = match uid {
            Some(u) => u
                .try_into()
                .map_err(|_| PyValueError::new_err("uid must be 16 bytes"))?,
            None => mail::uuid4(),
        };
        let deleted = tr(py, "Удаленные");
        let path = path.to_string_lossy().into_owned();
        let w = Writer::create(&path, &display_name, password, uid, deleted)
            .map_err(|e| to_pyerr(py, e))?;
        Ok(PstFile {
            w: Some(w),
            path,
            closed: false,
        })
    }

    fn size(&mut self) -> PyResult<u64> {
        Ok(self.writer()?.size())
    }

    fn close_overhead(&mut self) -> PyResult<i64> {
        Ok(self.writer()?.close_overhead())
    }

    fn add_folder(&mut self, parent_nid: u32, name: &Bound<'_, PyAny>) -> PyResult<u32> {
        let name = py_string(name)?;
        Ok(self.writer()?.add_folder(parent_nid, &name))
    }

    fn entry_id<'py>(&mut self, py: Python<'py>, nid: u32) -> PyResult<Bound<'py, PyBytes>> {
        Ok(PyBytes::new(py, &self.writer()?.entry_id(nid)))
    }

    /// msg: props (dict tag->value), recipients [dict], attachments [dict], read (bool).
    fn add_message(
        &mut self,
        py: Python<'_>,
        folder_nid: u32,
        msg: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        let m = message_from_py(msg)?;
        let w = self.writer()?;
        py.allow_threads(|| guard(WRITE_PANIC, || w.add_message(folder_nid, m)))
            .map_err(|e| to_pyerr(py, e))
    }

    /// Разобрать .eml / .eml.gz и записать письмо — целиком без GIL.
    fn add_eml(
        &mut self,
        py: Python<'_>,
        folder_nid: u32,
        path: PathBuf,
        read: bool,
    ) -> PyResult<i64> {
        let fwd = tr(py, "Пересланное письмо");
        let w = self.writer()?;
        py.allow_threads(|| {
            let m = guard(PARSE_PANIC, || {
                mail::eml_to_message(&path, read, &fwd).map_err(PstError::from)
            })?;
            let received = m.received;
            guard(WRITE_PANIC, || w.add_message(folder_nid, m)).map(|_| received)
        })
        .map_err(|e| to_pyerr(py, e))
    }

    /// Записать письмо, разобранное parse_eml() (в другом потоке). Возвращает дату получения.
    fn add_parsed(
        &mut self,
        py: Python<'_>,
        folder_nid: u32,
        mut msg: PyRefMut<'_, ParsedEml>,
    ) -> PyResult<i64> {
        let m = msg
            .m
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("message is already written"))?;
        let w = self.writer()?;
        py.allow_threads(|| {
            let received = m.received;
            guard(WRITE_PANIC, || w.add_message(folder_nid, m)).map(|_| received)
        })
        .map_err(|e| to_pyerr(py, e))
    }

    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        let w = self
            .w
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("PST file is closed"))?;
        py.allow_threads(|| guard(WRITE_PANIC, || w.close()))
            .map_err(|e| to_pyerr(py, e))?;
        self.closed = true;
        Ok(())
    }

    /// Прервать запись и удалить недописанный файл (готовый, после close(), не трогается).
    fn abort(&mut self) {
        if let Some(w) = self.w.take() {
            w.abort();
        } else if !self.closed {
            let _ = std::fs::remove_file(&self.path); // close() упал на полпути
        }
    }
}

use std::path::PathBuf;

impl From<mail::MailError> for PstError {
    fn from(e: mail::MailError) -> Self {
        match e {
            mail::MailError::Io(e) => PstError::Io(e),
        }
    }
}

// ---------------------------------------------------------------- функции модуля

#[pyfunction]
fn pst_crc(data: &[u8]) -> u32 {
    pst::pst_crc(data)
}

/// То же, что core.pst_export.eml_to_message(), но в Rust (для проверки и отладки).
#[pyfunction]
fn eml_to_message<'py>(py: Python<'py>, path: PathBuf, read: bool) -> PyResult<Bound<'py, PyDict>> {
    let fwd = tr(py, "Пересланное письмо");
    let m = py
        .allow_threads(|| {
            guard(PARSE_PANIC, || {
                mail::eml_to_message(&path, read, &fwd).map_err(PstError::from)
            })
        })
        .map_err(|e| to_pyerr(py, e))?;
    let d = PyDict::new(py);
    d.set_item("props", props_to_py(py, &m.props)?)?;
    let recs = PyList::empty(py);
    for r in &m.recipients {
        recs.append(props_to_py(py, r)?)?;
    }
    d.set_item("recipients", recs)?;
    let atts = PyList::empty(py);
    for (p, row) in &m.attachments {
        let a = PyDict::new(py);
        a.set_item("props", props_to_py(py, p)?)?;
        a.set_item("row", props_to_py(py, row)?)?;
        atts.append(a)?;
    }
    d.set_item("attachments", atts)?;
    d.set_item("read", m.read)?;
    d.set_item("received", m.received)?;
    Ok(d)
}

/// То же, что core.mailparse.summarize(): сведения о письме для просмотрщика (без GIL).
/// Разобранное письмо: parse_eml() готовит его в рабочем потоке, PstFile.add_parsed()
/// записывает. Так письма разбираются на нескольких ядрах, а пишутся по порядку.
#[pyclass(module = "shinra_core")]
struct ParsedEml {
    m: Option<Message>,
}

/// Разобрать .eml / .eml.gz для записи в PST — целиком без GIL.
#[pyfunction]
fn parse_eml(py: Python<'_>, path: PathBuf, read: bool) -> PyResult<ParsedEml> {
    let fwd = tr(py, "Пересланное письмо");
    py.allow_threads(|| {
        guard(PARSE_PANIC, || {
            mail::eml_to_message(&path, read, &fwd).map_err(PstError::from)
        })
    })
    .map(|m| ParsedEml { m: Some(m) })
    .map_err(|e| to_pyerr(py, e))
}

/// Текст письма для поиска (обычный текст или HTML без разметки) — без GIL.
#[pyfunction]
fn search_text(py: Python<'_>, path: PathBuf) -> PyResult<String> {
    let fwd = tr(py, "Пересланное письмо");
    py.allow_threads(|| {
        guard(PARSE_PANIC, || {
            mail::search_text(&path, &fwd).map_err(PstError::from)
        })
    })
    .map_err(|e| to_pyerr(py, e))
}

#[pyfunction]
fn summarize<'py>(py: Python<'py>, path: PathBuf) -> PyResult<Bound<'py, PyDict>> {
    let fwd = tr(py, "Пересланное письмо");
    let s = py
        .allow_threads(|| {
            guard(PARSE_PANIC, || {
                mail::summarize(&path, &fwd).map_err(PstError::from)
            })
        })
        .map_err(|e| to_pyerr(py, e))?;
    let d = PyDict::new(py);
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    d.set_item("file", name)?;
    d.set_item("from", s.from)?;
    d.set_item("from_name", s.from_name)?;
    d.set_item("from_addr", s.from_addr)?;
    d.set_item("to", s.to)?;
    d.set_item("cc", s.cc)?;
    d.set_item("subject", s.subject)?;
    d.set_item("msgid", s.msgid)?;
    d.set_item("sent", s.sent)?;
    d.set_item("received", s.received)?;
    d.set_item("size", s.size)?;
    d.set_item("fsize", s.fsize)?;
    d.set_item("mtime", s.mtime)?;
    d.set_item("attach", s.att_names.len())?;
    d.set_item(
        "att_names",
        s.att_names.into_iter().take(10).collect::<Vec<_>>(),
    )?;
    Ok(d)
}

/// То же, что core.pst_reader.verify(): полная проверка файла, сводка {папка: писем}.
#[pyfunction]
fn verify<'py>(py: Python<'py>, path: PathBuf) -> PyResult<Bound<'py, PyDict>> {
    std::fs::File::open(&path)
        .map_err(|e| PyOSError::new_err(format!("{e}: {}", path.display())))?;
    let path = path.to_string_lossy().into_owned();
    let res = py.allow_threads(|| {
        catch_unwind(AssertUnwindSafe(|| reader::verify(&path))).unwrap_or(Err(
            reader::CheckError {
                template: reader::BROKEN,
                args: vec![],
            },
        ))
    });
    match res {
        Ok(items) => {
            let d = PyDict::new(py);
            for (k, v) in items {
                match v {
                    reader::Summary::Str(s) => d.set_item(k, s)?,
                    reader::Summary::Bool(b) => d.set_item(k, b)?,
                    reader::Summary::Count(n) => d.set_item(k, n)?,
                }
            }
            Ok(d)
        }
        Err(e) => {
            let mut msg = tr(py, e.template);
            for (k, v) in &e.args {
                msg = msg
                    .replace(&format!("{{{k}:#x}}"), v)
                    .replace(&format!("{{{k}}}"), v);
            }
            Err(PstCheckError::new_err(msg))
        }
    }
}

#[pymodule]
fn shinra_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("PstCheckError", m.py().get_type::<PstCheckError>())?;
    m.add_function(wrap_pyfunction!(verify, m)?)?;
    m.add_class::<PstFile>()?;
    m.add_function(wrap_pyfunction!(pst_crc, m)?)?;
    m.add_function(wrap_pyfunction!(eml_to_message, m)?)?;
    m.add_function(wrap_pyfunction!(summarize, m)?)?;
    m.add_class::<ParsedEml>()?;
    m.add_function(wrap_pyfunction!(parse_eml, m)?)?;
    m.add_function(wrap_pyfunction!(search_text, m)?)?;
    m.add_function(wrap_pyfunction!(set_translator, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
