//! IMF-fixdate (RFC 9110 §5.6.7) parsing and formatting.

const DAYS: [&str; 7] = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"]; // 1970-01-01 was a Thursday
const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

pub fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

pub fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn is_leap(y: i64) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}

fn month_days(y: i64, m: i64) -> i64 {
    match m {
        2 => if is_leap(y) { 29 } else { 28 },
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Formats Unix seconds as an IMF-fixdate. `None` if the year is outside 1..=9999.
pub fn format(secs: i64) -> Option<String> {
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    if !(1..=9999).contains(&y) {
        return None;
    }
    let wd = DAYS[days.rem_euclid(7) as usize];
    Some(format!(
        "{wd}, {d:02} {} {y:04} {:02}:{:02}:{:02} GMT",
        MONTHS[(m - 1) as usize],
        rem / 3600,
        rem / 60 % 60,
        rem % 60
    ))
}

/// Parses an IMF-fixdate strictly, returning Unix seconds.
pub fn parse(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() != 29 || &b[3..5] != b", " || b[7] != b' ' || b[11] != b' ' || b[16] != b' '
        || b[19] != b':' || b[22] != b':' || b[25] != b' ' || &b[26..] != b"GMT"
    {
        return None;
    }
    let num = |r: std::ops::Range<usize>| -> Option<i64> {
        let t = &s[r];
        if t.bytes().all(|c| c.is_ascii_digit()) { t.parse().ok() } else { None }
    };
    let day = num(5..7)?;
    let m = MONTHS.iter().position(|&x| x == &s[8..11])? as i64 + 1;
    let y = num(12..16)?;
    let hh = num(17..19)?;
    let mm = num(20..22)?;
    let ss = num(23..25)?;
    if y < 1 || day < 1 || day > month_days(y, m) || hh > 23 || mm > 59 || ss > 60 {
        return None;
    }
    let days = days_from_civil(y, m, day);
    if DAYS[days.rem_euclid(7) as usize] != &s[0..3] {
        return None;
    }
    Some(days * 86400 + hh * 3600 + mm * 60 + ss)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip() {
        assert_eq!(parse("Sun, 06 Nov 1994 08:49:37 GMT"), Some(784111777));
        assert_eq!(format(784111777).unwrap(), "Sun, 06 Nov 1994 08:49:37 GMT");
        assert_eq!(format(0).unwrap(), "Thu, 01 Jan 1970 00:00:00 GMT");
        assert_eq!(format(-1).unwrap(), "Wed, 31 Dec 1969 23:59:59 GMT");
        assert!(format(-62135596801).is_none()); // year 0
        assert_eq!(format(-62135596800).unwrap(), "Mon, 01 Jan 0001 00:00:00 GMT");
        assert!(format(253402300800).is_none()); // year 10000
        assert_eq!(parse("Fri, 31 Dec 9999 23:59:59 GMT"), Some(253402300799));
    }
    #[test]
    fn rejects() {
        assert_eq!(parse("Mon, 06 Nov 1994 08:49:37 GMT"), None); // wrong day name
        assert_eq!(parse("Sunday, 06-Nov-94 08:49:37 GMT"), None);
        assert_eq!(parse("Sun Nov  6 08:49:37 1994"), None);
        assert_eq!(parse("Sun, 31 Nov 1994 08:49:37 GMT"), None);
        assert_eq!(parse("Sun, 06 Nov 1994 08:49:37 UTC"), None);
    }
}
