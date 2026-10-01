//! HTTP-date (RFC 9110 §5.6.7): formatting as IMF-fixdate, parsing all three
//! formats.

const DAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const LONG_DAYS: [&str; 7] = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/// Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant).
pub fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let m = m as i64;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

pub fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn days_in_month(y: i64, m: u32) -> u32 {
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

/// Format seconds since the epoch as an IMF-fixdate. `None` if the year is
/// outside 1..=9999.
pub fn format_imf_fixdate(secs: i64) -> Option<String> {
    let days = secs.div_euclid(86400);
    let sod = secs.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    if !(1..=9999).contains(&y) {
        return None;
    }
    // 1970-01-01 was a Thursday (index 3).
    let wd = (days + 3).rem_euclid(7) as usize;
    Some(format!(
        "{}, {:02} {} {:04} {:02}:{:02}:{:02} GMT",
        DAYS[wd],
        d,
        MONTHS[(m - 1) as usize],
        y,
        sod / 3600,
        (sod / 60) % 60,
        sod % 60
    ))
}

fn two_digits(s: &str) -> Option<u32> {
    if s.len() == 2 && s.bytes().all(|c| c.is_ascii_digit()) {
        s.parse().ok()
    } else {
        None
    }
}

fn parse_time(s: &str) -> Option<i64> {
    let p: Vec<&str> = s.split(':').collect();
    if p.len() != 3 {
        return None;
    }
    let (h, m, sec) = (two_digits(p[0])?, two_digits(p[1])?, two_digits(p[2])?);
    if h > 23 || m > 59 || sec > 60 {
        return None;
    }
    Some(h as i64 * 3600 + m as i64 * 60 + sec as i64)
}

fn month(s: &str) -> Option<u32> {
    MONTHS.iter().position(|m| *m == s).map(|i| i as u32 + 1)
}

fn build(y: i64, m: u32, d: u32, t: i64) -> Option<i64> {
    if d == 0 || d > days_in_month(y, m) {
        return None;
    }
    Some(days_from_civil(y, m, d) * 86400 + t)
}

/// Parse an HTTP-date (case-sensitive) into seconds since the epoch.
pub fn parse_http_date(s: &str) -> Option<i64> {
    // IMF-fixdate: "Sun, 06 Nov 1994 08:49:37 GMT"
    if s.len() == 29 && s.as_bytes()[3] == b',' {
        let p: Vec<&str> = s.split(' ').collect();
        if p.len() != 6 || !DAYS.contains(&&p[0][..3]) || p[0].len() != 4 || p[5] != "GMT" {
            return None;
        }
        let d = two_digits(p[1])?;
        let m = month(p[2])?;
        if p[3].len() != 4 || !p[3].bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        let y: i64 = p[3].parse().ok()?;
        return build(y, m, d, parse_time(p[4])?);
    }
    // rfc850-date: "Sunday, 06-Nov-94 08:49:37 GMT"
    if let Some(comma) = s.find(", ") {
        let day = &s[..comma];
        if LONG_DAYS.contains(&day) {
            let p: Vec<&str> = s[comma + 2..].split(' ').collect();
            if p.len() != 3 || p[2] != "GMT" {
                return None;
            }
            let dp: Vec<&str> = p[0].split('-').collect();
            if dp.len() != 3 {
                return None;
            }
            let d = two_digits(dp[0])?;
            let m = month(dp[1])?;
            let yy = two_digits(dp[2])? as i64;
            // RFC 9110: a year more than 50 years in the future is in the past century.
            let now_year = civil_from_days(
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs() as i64)
                    .unwrap_or(0)
                    .div_euclid(86400),
            )
            .0;
            let century = now_year - now_year.rem_euclid(100);
            let mut y = century + yy;
            if y > now_year + 50 {
                y -= 100;
            }
            return build(y, m, d, parse_time(p[1])?);
        }
        return None;
    }
    // asctime-date: "Sun Nov  6 08:49:37 1994"
    if s.len() == 24 {
        let b = s.as_bytes();
        if !DAYS.contains(&&s[..3]) || b[3] != b' ' || b[7] != b' ' || b[10] != b' ' || b[19] != b' ' {
            return None;
        }
        let m = month(&s[4..7])?;
        let dstr = &s[8..10];
        let d = if dstr.as_bytes()[0] == b' ' {
            let c = dstr.as_bytes()[1];
            if !c.is_ascii_digit() {
                return None;
            }
            (c - b'0') as u32
        } else {
            two_digits(dstr)?
        };
        let t = parse_time(&s[11..19])?;
        let ys = &s[20..24];
        if !ys.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        return build(ys.parse().ok()?, m, d, t);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip() {
        assert_eq!(format_imf_fixdate(784111777).unwrap(), "Sun, 06 Nov 1994 08:49:37 GMT");
        assert_eq!(parse_http_date("Sun, 06 Nov 1994 08:49:37 GMT"), Some(784111777));
        assert_eq!(parse_http_date("Sunday, 06-Nov-94 08:49:37 GMT"), Some(784111777));
        assert_eq!(parse_http_date("Sun Nov  6 08:49:37 1994"), Some(784111777));
        assert_eq!(parse_http_date("sun, 06 Nov 1994 08:49:37 GMT"), None);
        assert_eq!(parse_http_date("Sun, 31 Feb 1994 08:49:37 GMT"), None);
        assert_eq!(format_imf_fixdate(-62135596800).unwrap(), "Mon, 01 Jan 0001 00:00:00 GMT");
        assert!(format_imf_fixdate(-62135596801).is_none());
        assert!(format_imf_fixdate(253402300799).is_some());
        assert!(format_imf_fixdate(253402300800).is_none());
        assert_eq!(format_imf_fixdate(-1).unwrap(), "Wed, 31 Dec 1969 23:59:59 GMT");
    }
}
