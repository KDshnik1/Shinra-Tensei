//! email.utils.parsedate_to_datetime(...).timestamp() — порт _parsedate_tz из Python.

const MONTHS: [&str; 24] = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
];
const DAYS: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

fn tz_named(tz: &str) -> Option<i64> {
    Some(match tz {
        "UT" | "UTC" | "GMT" | "Z" => 0,
        "AST" => -400,
        "ADT" => -300,
        "EST" => -500,
        "EDT" => -400,
        "CST" => -600,
        "CDT" => -500,
        "MST" => -700,
        "MDT" => -600,
        "PST" => -800,
        "PDT" => -700,
        _ => return None,
    })
}

/// int() Python для строки: пробелы по краям, знак, цифры с одиночными «_».
fn py_int(s: &str) -> Option<i64> {
    let s = s.trim_matches(crate::text::py_space);
    let (neg, digits) = match s.as_bytes().first()? {
        b'+' => (false, &s[1..]),
        b'-' => (true, &s[1..]),
        _ => (false, s),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return None;
    }
    let mut v: i64 = 0;
    for c in digits.chars() {
        if c == '_' {
            continue;
        }
        let d = c.to_digit(10)? as i64; // Python принимает и не-ASCII цифры; здесь — только ASCII
        v = v.checked_mul(10)?.checked_add(d)?;
    }
    Some(if neg { -v } else { v })
}

/// Как py_int, но число вне i64 не ошибка, а «очень большое» (для часового пояса:
/// Python такое смещение примет, а дату потом отвергнет).
fn py_int_sat(s: &str) -> Option<i64> {
    py_int(s).or_else(|| {
        let t = s.trim_matches(crate::text::py_space);
        let (neg, digits) = match t.as_bytes().first()? {
            b'-' => (true, &t[1..]),
            b'+' => (false, &t[1..]),
            _ => (false, t),
        };
        let ok = !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit() || c == '_');
        ok.then_some(if neg { -i64::MAX } else { i64::MAX })
    })
}

/// Разбор даты. Возвращает (год, мес, день, ч, мин, сек, смещение_сек | None).
fn parsedate_tz(data: &str) -> Option<(i64, i64, i64, i64, i64, i64, Option<i64>)> {
    let mut data: Vec<String> = data
        .split(crate::text::py_space)
        .filter(|w| !w.is_empty())
        .map(String::from)
        .collect();
    if data.is_empty() {
        return None;
    }
    if data[0].ends_with(',') || DAYS.contains(&data[0].to_lowercase().as_str()) {
        data.remove(0);
    } else if let Some(i) = data[0].rfind(',') {
        data[0] = data[0][i + 1..].to_string();
    }
    if data.len() == 3 {
        let stuff: Vec<String> = data[0].split('-').map(String::from).collect();
        if stuff.len() == 3 {
            let mut d = stuff;
            d.extend_from_slice(&data[1..]);
            data = d;
        }
    }
    if data.len() == 4 {
        let s = data[3].clone();
        let i = s.find('+').or_else(|| s.find('-'));
        match i {
            Some(i) if i > 0 => {
                data.truncate(3);
                data.push(s[..i].to_string());
                data.push(s[i..].to_string());
            }
            _ => data.push(String::new()),
        }
    }
    if data.len() < 5 {
        return None;
    }
    data.truncate(5);
    let (mut dd, mut mm, mut yy, mut tm, mut tz) = (
        data[0].clone(),
        data[1].clone(),
        data[2].clone(),
        data[3].clone(),
        data[4].clone(),
    );
    if dd.is_empty() || mm.is_empty() || yy.is_empty() {
        return None;
    }
    mm = mm.to_lowercase();
    if !MONTHS.contains(&mm.as_str()) {
        let t = dd.to_lowercase();
        dd = mm;
        mm = t;
        if !MONTHS.contains(&mm.as_str()) {
            return None;
        }
    }
    let mut mon = MONTHS.iter().position(|m| *m == mm).unwrap() as i64 + 1;
    if mon > 12 {
        mon -= 12;
    }
    if dd.ends_with(',') {
        dd.pop();
    }
    if let Some(i) = yy.find(':')
        && i > 0
    {
        std::mem::swap(&mut yy, &mut tm);
    }
    if yy.ends_with(',') {
        yy.pop();
        if yy.is_empty() {
            return None;
        }
    }
    if !yy.chars().next().is_some_and(|c| c.is_ascii_digit()) {
        std::mem::swap(&mut yy, &mut tz);
    }
    if tm.ends_with(',') {
        tm.pop();
    }
    let parts: Vec<&str> = tm.split(':').collect();
    let (thh, tmm, tss): (String, String, String) = match parts.len() {
        2 => (parts[0].into(), parts[1].into(), "0".into()),
        3 => (parts[0].into(), parts[1].into(), parts[2].into()),
        1 if tm.contains('.') => {
            let p: Vec<&str> = tm.split('.').collect();
            match p.len() {
                2 => (p[0].into(), p[1].into(), "0".into()),
                3 => (p[0].into(), p[1].into(), p[2].into()),
                _ => return None,
            }
        }
        _ => return None,
    };
    let mut y = py_int(&yy)?;
    let d = py_int(&dd)?;
    let h = py_int(&thh)?;
    let mi = py_int(&tmm)?;
    let s = py_int(&tss)?;
    if y < 100 {
        y += if y > 68 { 1900 } else { 2000 };
    }
    let tzu = tz.to_uppercase();
    let mut off = tz_named(&tzu).or_else(|| py_int_sat(&tzu));
    if off == Some(0) && tzu.starts_with('-') {
        off = None;
    }
    if let Some(o) = off
        && o != 0
    {
        let (sign, a) = if o < 0 { (-1, -o) } else { (1, o) };
        off = Some(
            sign * (a / 100)
                .saturating_mul(3600)
                .saturating_add((a % 100) * 60),
        );
    }
    Some((y, mon, d, h, mi, s, off))
}

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn month_days(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ => {
            if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 {
                29
            } else {
                28
            }
        }
    }
}

/// Проверки datetime(...) и timezone(...) Python: (год, мес, день, ч, мин, сек, смещение).
fn checked(value: &str) -> Option<(i64, i64, i64, i64, i64, i64, i64)> {
    let (y, mo, d, h, mi, s, off) = parsedate_tz(value)?;
    if !(1..=9999).contains(&y) || !(1..=12).contains(&mo) || d < 1 || d > month_days(y, mo) {
        return None;
    }
    if !(0..24).contains(&h) || !(0..60).contains(&mi) || !(0..60).contains(&s) {
        return None;
    }
    let off = off.unwrap_or(0);
    if off.abs() >= 86400 {
        return None; // datetime.timezone: смещение строго меньше суток
    }
    Some((y, mo, d, h, mi, s, off))
}

/// parsedate_to_datetime(value).isoformat() (без пояса — UTC), как mailparse.summarize.
pub fn sent_iso(value: &str) -> Option<String> {
    let (y, mo, d, h, mi, s, off) = checked(value)?;
    let (sign, a) = if off < 0 { ('-', -off) } else { ('+', off) };
    Some(format!(
        "{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}{sign}{:02}:{:02}",
        a / 3600,
        a % 3600 / 60
    ))
}

/// Время из заголовка Date в секундах эпохи (UTC, если пояс не указан).
/// None — Python бросил бы исключение (дата не разобрана или некорректна).
pub fn sent_timestamp(value: &str) -> Option<i64> {
    let (y, mo, d, h, mi, s, off) = checked(value)?;
    Some(days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s - off)
}
