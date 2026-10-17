//! Очистка HTML письма перед показом в просмотрщике (то же делает core/htmlclean.py).
//!
//! Просмотрщик Qt понимает только простой HTML: он не умеет display:none, поэтому
//! показывает скрытый текст рассылок, а на месте картинок из интернета (их мы не
//! загружаем) оставляет пустые места. Здесь убирается то, что показать нельзя или не
//! нужно, а остальное передаётся как есть. Сами письма в архиве не меняются.

use std::collections::HashMap;

enum Tok {
    /// текст, сущности уже раскрыты
    Text(String),
    /// содержимое <style>/<script> как есть
    Raw(String),
    Start {
        name: String,
        attrs: Vec<(String, Option<String>)>,
        closed: bool,
    },
    End(String),
}

/// Удаляются вместе с содержимым: исполняемое, встроенные окна, элементы форм.
const DROP: &[&str] = &[
    "script", "title", "iframe", "frame", "frameset", "object", "embed", "applet", "noembed",
    "template", "svg", "math", "select", "textarea", "button", "canvas", "audio", "video",
];
/// Удаляется только сам тег, содержимое остаётся.
const STRIP: &[&str] = &["meta", "link", "base", "form", "input"];
const VOID: &[&str] = &[
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
    "track", "wbr",
];
const URL_ATTRS: &[&str] = &[
    "href", "src", "background", "action", "formaction", "poster", "cite", "longdesc", "usemap",
    "xlink:href", "dynsrc", "lowsrc",
];

fn is_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r' | b'\x0c')
}

fn unescape(s: &str) -> String {
    htmlize::unescape(s).into_owned()
}

fn escape_text(s: &str, out: &mut String) {
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            _ => out.push(c),
        }
    }
}

fn escape_attr(s: &str, out: &mut String) {
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(c),
        }
    }
}

/// Атрибуты внутри тега: s — текст между именем тега и «>».
fn parse_attrs(s: &str) -> Vec<(String, Option<String>)> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        while i < b.len() && (is_space(b[i]) || b[i] == b'/') {
            i += 1;
        }
        if i >= b.len() {
            break;
        }
        let start = i;
        i += 1; // первый символ имени может быть и «=»
        while i < b.len() && !is_space(b[i]) && !matches!(b[i], b'/' | b'=' | b'>') {
            i += 1;
        }
        let name = s[start..i].to_ascii_lowercase();
        let mut j = i;
        while j < b.len() && is_space(b[j]) {
            j += 1;
        }
        if j < b.len() && b[j] == b'=' {
            while j < b.len() && b[j] == b'=' {
                j += 1;
            }
            while j < b.len() && is_space(b[j]) {
                j += 1;
            }
            let value = if j < b.len() && (b[j] == b'"' || b[j] == b'\'') {
                let q = b[j];
                let vs = j + 1;
                let ve = b[vs..].iter().position(|&c| c == q).map_or(b.len(), |p| vs + p);
                j = (ve + 1).min(b.len());
                &s[vs..ve]
            } else {
                let vs = j;
                while j < b.len() && !is_space(b[j]) && b[j] != b'>' {
                    j += 1;
                }
                &s[vs..j]
            };
            out.push((name, Some(unescape(value))));
            i = j;
        } else {
            out.push((name, None));
        }
    }
    out
}

/// Конец тега, начатого в start (указывает на «<»): позиция «>» с учётом кавычек.
fn tag_end(b: &[u8], start: usize) -> Option<usize> {
    let mut q = 0u8;
    for (k, &c) in b.iter().enumerate().skip(start + 1) {
        if q != 0 {
            if c == q {
                q = 0;
            }
        } else if c == b'"' || c == b'\'' {
            // кавычка открывает значение только после «=»
            let prev = b[..k].iter().rev().find(|&&p| !is_space(p));
            if prev == Some(&b'=') {
                q = c;
            }
        } else if c == b'>' {
            return Some(k);
        }
    }
    None
}

fn tokenize(h: &str) -> Vec<Tok> {
    let b = h.as_bytes();
    let mut toks = Vec::new();
    let mut text = String::new();
    let flush = |text: &mut String, toks: &mut Vec<Tok>| {
        if !text.is_empty() {
            toks.push(Tok::Text(unescape(text)));
            text.clear();
        }
    };
    let mut i = 0;
    while i < b.len() {
        let Some(p) = memchr::memchr(b'<', &b[i..]) else {
            text.push_str(&h[i..]);
            break;
        };
        let lt = i + p;
        text.push_str(&h[i..lt]);
        let next = b.get(lt + 1).copied().unwrap_or(0);
        if h[lt..].starts_with("<!--") {
            // комментарий (в том числе условные <!--[if mso]> Outlook) — не показываем
            flush(&mut text, &mut toks);
            i = memchr::memmem::find(&b[lt + 4..], b"-->").map_or(b.len(), |e| lt + 4 + e + 3);
        } else if next == b'!' || next == b'?' || (next == b'/' && !b.get(lt + 2).is_some_and(|c| c.is_ascii_alphabetic())) {
            // <!DOCTYPE>, <?xml?>, </ мусор> — пропускаем до «>»
            flush(&mut text, &mut toks);
            i = memchr::memchr(b'>', &b[lt..]).map_or(b.len(), |e| lt + e + 1);
        } else if next == b'/' || next.is_ascii_alphabetic() {
            let end_tag = next == b'/';
            let ns = lt + if end_tag { 2 } else { 1 };
            let Some(gt) = tag_end(b, lt) else {
                // тег не закрыт до конца текста — это просто текст
                text.push_str(&h[lt..]);
                break;
            };
            flush(&mut text, &mut toks);
            let mut ne = ns;
            while ne < gt && !is_space(b[ne]) && b[ne] != b'/' {
                ne += 1;
            }
            let name = h[ns..ne].to_ascii_lowercase();
            i = gt + 1;
            if end_tag {
                toks.push(Tok::End(name));
                continue;
            }
            let inner = &h[ne..gt];
            let closed = inner.trim_end().ends_with('/');
            toks.push(Tok::Start {
                name: name.clone(),
                attrs: parse_attrs(inner),
                closed,
            });
            if !closed && (name == "script" || name == "style") {
                // содержимое — как есть, до закрывающего тега
                let close = format!("</{name}");
                let lower = h[i..].to_ascii_lowercase();
                let e = lower.find(&close).map_or(b.len(), |e| i + e);
                if e > i {
                    toks.push(Tok::Raw(h[i..e].to_string()));
                }
                i = e;
            }
        } else {
            // «<» без тега — обычный символ
            text.push('<');
            i = lt + 1;
        }
    }
    flush(&mut text, &mut toks);
    toks
}

fn attr<'a>(attrs: &'a [(String, Option<String>)], name: &str) -> Option<&'a str> {
    attrs
        .iter()
        .find(|(n, _)| n == name)
        .map(|(_, v)| v.as_deref().unwrap_or(""))
}

/// Элемент скрыт стилем или атрибутом hidden (так прячут «прехедеры» рассылок).
fn hidden(attrs: &[(String, Option<String>)]) -> bool {
    if attr(attrs, "hidden").is_some() {
        return true;
    }
    let Some(style) = attr(attrs, "style") else {
        return false;
    };
    let st: String = style
        .chars()
        .filter(|c| !c.is_whitespace())
        .collect::<String>()
        .to_ascii_lowercase();
    if st.contains("display:none") || st.contains("visibility:hidden") {
        return true;
    }
    if st.contains("overflow:hidden")
        && let Some(p) = st.find("max-height:0")
    {
        let rest = &st[p + "max-height:0".len()..];
        return !rest.starts_with(|c: char| c.is_ascii_digit() || c == '.');
    }
    false
}

/// Ссылка, которую нельзя оставлять: скрипт или встроенный документ.
fn bad_url(attr: &str, value: &str) -> bool {
    let v: String = value
        .chars()
        .filter(|c| !c.is_ascii_whitespace() && !c.is_control())
        .collect::<String>()
        .to_ascii_lowercase();
    if v.starts_with("javascript:") || v.starts_with("vbscript:") {
        return true;
    }
    v.starts_with("data:") && !(attr == "src" && v.starts_with("data:image/"))
}

fn tiny(attrs: &[(String, Option<String>)]) -> bool {
    ["width", "height"].iter().any(|n| {
        attr(attrs, n)
            .map(|v| v.trim().trim_end_matches("px").trim())
            .and_then(|v| v.parse::<f64>().ok())
            .is_some_and(|x| x <= 2.0)
    })
}

fn write_start(name: &str, attrs: &[(String, Option<String>)], closed: bool, out: &mut String) {
    out.push('<');
    out.push_str(name);
    for (n, v) in attrs {
        if n.starts_with("on") {
            continue;
        }
        if let Some(v) = v
            && URL_ATTRS.contains(&n.as_str())
            && bad_url(n, v)
        {
            continue;
        }
        out.push(' ');
        out.push_str(n);
        if let Some(v) = v {
            out.push_str("=\"");
            escape_attr(v, out);
            out.push('"');
        }
    }
    out.push_str(if closed { " />" } else { ">" });
}

/// HTML письма, пригодный для показа: без скриптов, скрытого текста и картинок из сети.
pub fn clean_html(h: &str) -> String {
    let toks = tokenize(h);
    // пара для каждого открывающего тега: ближайший закрывающий с тем же именем
    let mut close_of: HashMap<usize, usize> = HashMap::new();
    let mut open: HashMap<&str, Vec<usize>> = HashMap::new();
    for (k, t) in toks.iter().enumerate() {
        match t {
            Tok::Start { name, closed: false, .. } if !VOID.contains(&name.as_str()) => {
                open.entry(name.as_str()).or_default().push(k);
            }
            Tok::End(name) => {
                if let Some(s) = open.get_mut(name.as_str()).and_then(|v| v.pop()) {
                    close_of.insert(s, k);
                }
            }
            _ => {}
        }
    }
    let mut out = String::with_capacity(h.len());
    let mut k = 0;
    while k < toks.len() {
        match &toks[k] {
            Tok::Text(t) => escape_text(t, &mut out),
            Tok::Raw(t) => out.push_str(t),
            Tok::End(name) => {
                if !DROP.contains(&name.as_str()) && !STRIP.contains(&name.as_str()) {
                    out.push_str("</");
                    out.push_str(name);
                    out.push('>');
                }
            }
            Tok::Start { name, attrs, closed } => {
                let void = *closed || VOID.contains(&name.as_str());
                if DROP.contains(&name.as_str()) {
                    // без закрывающего тега — до конца текста
                    k = if void { k + 1 } else { close_of.get(&k).map_or(toks.len(), |e| e + 1) };
                    continue;
                }
                if hidden(attrs) {
                    if void {
                        k += 1;
                        continue;
                    }
                    if let Some(e) = close_of.get(&k) {
                        k = e + 1;
                        continue;
                    }
                    // не закрыт: прятать нечего, иначе пропало бы всё письмо до конца
                }
                if STRIP.contains(&name.as_str()) {
                    k += 1;
                    continue;
                }
                if name == "img" {
                    let src = attr(attrs, "src").unwrap_or("").trim().to_ascii_lowercase();
                    if !(src.starts_with("cid:") || src.starts_with("data:image/")) {
                        // картинки из интернета не загружаются: вместо пустого места — подпись
                        if !tiny(attrs) {
                            escape_text(attr(attrs, "alt").unwrap_or("").trim(), &mut out);
                        }
                        k += 1;
                        continue;
                    }
                }
                write_start(name, attrs, *closed, &mut out);
            }
        }
        k += 1;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::clean_html;

    #[test]
    fn basics() {
        let h = r#"<html><head><title>T</title><style>p{color:red}</style></head><body>
<div style="display: none; max-height:0">прехедер</div>
<p onclick="x()">Привет &amp; пока</p><script>alert(1)</script>
<a href="javascript:alert(1)">a</a><a href="https://e.com/?a=1&amp;b=2">b</a>
<img src="https://t.co/p.gif" width="1" height="1"><img src="http://x/logo.png" alt="Логотип">
<img src="cid:abc"></body></html>"#;
        let c = clean_html(h);
        assert!(!c.contains("прехедер") && !c.contains("alert") && !c.contains("onclick"));
        assert!(!c.contains("<title>") && c.contains("<style>p{color:red}</style>"));
        assert!(c.contains("Привет &amp; пока"));
        assert!(c.contains(r#"<a>a</a>"#));
        assert!(c.contains(r#"href="https://e.com/?a=1&amp;b=2""#));
        assert!(c.contains("Логотип") && !c.contains("http://x/logo.png"));
        assert!(c.contains(r#"<img src="cid:abc">"#));
    }

    #[test]
    fn unclosed_hidden_keeps_rest() {
        let c = clean_html(r#"<div style="display:none">a<p>b</p>"#);
        assert!(c.contains("<p>b</p>"));
    }

    #[test]
    fn garbage_does_not_panic() {
        for h in ["<", "<a", "</", "<!--", "<p a='", "<script>", "a < b > c", "<\u{442}>", "<p =x>"] {
            clean_html(h);
        }
    }
}
