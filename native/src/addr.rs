//! email.utils.getaddresses / parseaddr (строгий режим Python 3.13) — порт AddrlistClass.

const SPECIALS: &str = "()<>@,:;.\"[]";
const LWS: &str = " \t";
const FWS: &str = " \t\r\n";

struct Parser {
    f: Vec<char>,
    pos: usize,
    comments: Vec<String>,
}

fn in_set(c: char, set: &str) -> bool {
    set.contains(c)
}

fn atomends(c: char) -> bool {
    in_set(c, SPECIALS) || in_set(c, LWS) || c == '\r' || c == '\n'
}

fn phraseends(c: char) -> bool {
    c != '.' && atomends(c)
}

fn quote(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

impl Parser {
    fn new(s: &str) -> Self {
        Parser {
            f: s.chars().collect(),
            pos: 0,
            comments: Vec::new(),
        }
    }

    fn at(&self) -> char {
        self.f[self.pos]
    }

    fn more(&self) -> bool {
        self.pos < self.f.len()
    }

    fn gotonext(&mut self) -> String {
        let mut ws = String::new();
        while self.more() {
            let c = self.at();
            if in_set(c, LWS) || c == '\n' || c == '\r' {
                if c != '\n' && c != '\r' {
                    ws.push(c);
                }
                self.pos += 1;
            } else if c == '(' {
                let c = self.getcomment();
                self.comments.push(c);
            } else {
                break;
            }
        }
        ws
    }

    fn getaddrlist(&mut self) -> Vec<(String, String)> {
        let mut result = Vec::new();
        while self.more() {
            let ad = self.getaddress();
            if ad.is_empty() {
                result.push((String::new(), String::new()));
            } else {
                result.extend(ad);
            }
        }
        result
    }

    fn getaddress(&mut self) -> Vec<(String, String)> {
        self.comments.clear();
        self.gotonext();
        let oldpos = self.pos;
        let oldcl = self.comments.clone();
        let plist = self.getphraselist();
        self.gotonext();
        let mut ret = Vec::new();
        if !self.more() {
            if !plist.is_empty() {
                ret = vec![(self.comments.join(" "), plist[0].clone())];
            }
        } else if self.at() == '.' || self.at() == '@' {
            self.pos = oldpos;
            self.comments = oldcl;
            let spec = self.getaddrspec();
            ret = vec![(self.comments.join(" "), spec)];
        } else if self.at() == ':' {
            self.pos += 1;
            while self.more() {
                self.gotonext();
                if self.more() && self.at() == ';' {
                    self.pos += 1;
                    break;
                }
                ret.extend(self.getaddress());
            }
        } else if self.at() == '<' {
            let route = self.getrouteaddr();
            if !self.comments.is_empty() {
                ret = vec![(
                    format!("{} ({})", plist.join(" "), self.comments.join(" ")),
                    route,
                )];
            } else {
                ret = vec![(plist.join(" "), route)];
            }
        } else if !plist.is_empty() {
            ret = vec![(self.comments.join(" "), plist[0].clone())];
        } else if in_set(self.at(), SPECIALS) {
            self.pos += 1;
        }
        self.gotonext();
        if self.more() && self.at() == ',' {
            self.pos += 1;
        }
        ret
    }

    fn getrouteaddr(&mut self) -> String {
        if self.at() != '<' {
            return String::new();
        }
        let mut expectroute = false;
        self.pos += 1;
        self.gotonext();
        let mut adlist = String::new();
        while self.more() {
            if expectroute {
                self.getdomain();
                expectroute = false;
            } else if self.at() == '>' {
                self.pos += 1;
                break;
            } else if self.at() == '@' {
                self.pos += 1;
                expectroute = true;
            } else if self.at() == ':' {
                self.pos += 1;
            } else {
                adlist = self.getaddrspec();
                self.pos += 1;
                break;
            }
            self.gotonext();
        }
        adlist
    }

    fn getaddrspec(&mut self) -> String {
        let mut aslist: Vec<String> = Vec::new();
        self.gotonext();
        while self.more() {
            let mut preserve_ws = true;
            let c = self.at();
            if c == '.' {
                if aslist.last().is_some_and(|l| l.trim().is_empty()) {
                    aslist.pop();
                }
                aslist.push(".".into());
                self.pos += 1;
                preserve_ws = false;
            } else if c == '"' {
                let q = self.getquote();
                aslist.push(format!("\"{}\"", quote(&q)));
            } else if atomends(c) {
                if aslist.last().is_some_and(|l| l.trim().is_empty()) {
                    aslist.pop();
                }
                break;
            } else {
                aslist.push(self.getatom(atomends));
            }
            let ws = self.gotonext();
            if preserve_ws && !ws.is_empty() {
                aslist.push(ws);
            }
        }
        if !self.more() || self.at() != '@' {
            return aslist.concat();
        }
        aslist.push("@".into());
        self.pos += 1;
        self.gotonext();
        let domain = self.getdomain();
        if domain.is_empty() {
            return String::new();
        }
        aslist.concat() + &domain
    }

    fn getdomain(&mut self) -> String {
        let mut sd: Vec<String> = Vec::new();
        while self.more() {
            let c = self.at();
            if in_set(c, LWS) {
                self.pos += 1;
            } else if c == '(' {
                let cm = self.getcomment();
                self.comments.push(cm);
            } else if c == '[' {
                sd.push(self.getdomainliteral());
            } else if c == '.' {
                self.pos += 1;
                sd.push(".".into());
            } else if c == '@' {
                return String::new();
            } else if atomends(c) {
                break;
            } else {
                sd.push(self.getatom(atomends));
            }
        }
        sd.concat()
    }

    fn getdelimited(&mut self, begin: char, ends: &str, allow_comments: bool) -> String {
        if self.at() != begin {
            return String::new();
        }
        let mut s = String::new();
        let mut q = false;
        self.pos += 1;
        while self.more() {
            let c = self.at();
            if q {
                s.push(c);
                q = false;
            } else if in_set(c, ends) {
                self.pos += 1;
                break;
            } else if allow_comments && c == '(' {
                s.push_str(&self.getcomment());
                continue;
            } else if c == '\\' {
                q = true;
            } else {
                s.push(c);
            }
            self.pos += 1;
        }
        s
    }

    fn getquote(&mut self) -> String {
        self.getdelimited('"', "\"\r", false)
    }

    fn getcomment(&mut self) -> String {
        self.getdelimited('(', ")\r", true)
    }

    fn getdomainliteral(&mut self) -> String {
        format!("[{}]", self.getdelimited('[', "]\r", false))
    }

    fn getatom(&mut self, ends: fn(char) -> bool) -> String {
        let mut s = String::new();
        while self.more() {
            if ends(self.at()) {
                break;
            }
            s.push(self.at());
            self.pos += 1;
        }
        s
    }

    fn getphraselist(&mut self) -> Vec<String> {
        let mut plist = Vec::new();
        while self.more() {
            let c = self.at();
            if in_set(c, FWS) {
                self.pos += 1;
            } else if c == '"' {
                plist.push(self.getquote());
            } else if c == '(' {
                let cm = self.getcomment();
                self.comments.push(cm);
            } else if phraseends(c) {
                break;
            } else {
                plist.push(self.getatom(phraseends));
            }
        }
        plist
    }
}

fn address_list(s: &str) -> Vec<(String, String)> {
    if s.is_empty() {
        return Vec::new();
    }
    Parser::new(s).getaddrlist()
}

/// Символы с учётом экранирования «\x» (как _iter_escaped_chars).
fn escaped_chars(s: &str) -> Vec<(usize, String)> {
    let chars: Vec<char> = s.chars().collect();
    let mut out = Vec::new();
    let mut esc = false;
    let mut pos = 0;
    for (i, &c) in chars.iter().enumerate() {
        pos = i;
        if esc {
            out.push((i, format!("\\{c}")));
            esc = false;
        } else if c == '\\' {
            esc = true;
        } else {
            out.push((i, c.to_string()));
        }
    }
    if esc {
        out.push((pos, "\\".into()));
    }
    out
}

fn strip_quoted_realnames(s: &str) -> String {
    if !s.contains('"') {
        return s.to_string();
    }
    let chars: Vec<char> = s.chars().collect();
    let mut start = 0;
    let mut open: Option<usize> = None;
    let mut out = String::new();
    for (pos, ch) in escaped_chars(s) {
        if ch == "\"" {
            match open {
                None => open = Some(pos),
                Some(o) => {
                    if start != o {
                        out.extend(&chars[start..o]);
                    }
                    start = pos + 1;
                    open = None;
                }
            }
        }
    }
    if start < chars.len() {
        out.extend(&chars[start..]);
    }
    out
}

fn check_parenthesis(s: &str) -> bool {
    let s = strip_quoted_realnames(s);
    let mut opens = 0i64;
    for (_, ch) in escaped_chars(&s) {
        if ch == "(" {
            opens += 1;
        } else if ch == ")" {
            opens -= 1;
            if opens < 0 {
                return false;
            }
        }
    }
    opens == 0
}

fn post_validate(v: Vec<(String, String)>) -> Vec<(String, String)> {
    v.into_iter()
        .map(|(n, a)| {
            if a.contains('[') {
                (String::new(), String::new())
            } else {
                (n, a)
            }
        })
        .collect()
}

/// getaddresses([value]) — один заголовок.
pub fn getaddresses(value: &str) -> Vec<(String, String)> {
    let v = if check_parenthesis(value) {
        value.to_string()
    } else {
        "('', '')".to_string()
    };
    let result = post_validate(address_list(&v));
    let n = 1 + strip_quoted_realnames(&v).matches(',').count();
    if result.len() != n {
        return vec![(String::new(), String::new())];
    }
    result
}

/// parseaddr(value)
pub fn parseaddr(value: &str) -> (String, String) {
    let v = if check_parenthesis(value) {
        value.to_string()
    } else {
        "('', '')".to_string()
    };
    let addrs = post_validate(address_list(&v));
    if addrs.len() != 1 {
        return (String::new(), String::new());
    }
    addrs.into_iter().next().unwrap()
}
