//! Minimal HTTP/1.1 range-request client (spec §6.2). Hand-written so that the
//! exact request headers are under our control (no HEAD, no automatic
//! decompression, no hidden extra requests).

use crate::uri::{self, Uri};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::time::Duration;

#[derive(Debug, Clone, Default)]
pub struct Pins {
    pub size: Option<u64>,
    pub etag: Option<String>,
    pub modified_not_after: Option<i64>,
}

const MAX_REDIRECTS: usize = 5;

/// Days since 1970-01-01 to (year, month, day). Howard Hinnant's algorithm.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Formats seconds since the epoch as an IMF-fixdate (RFC 9110 §5.6.7).
/// Returns None outside years 1..=9999.
pub fn imf_fixdate(secs: i64) -> Option<String> {
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    if !(1..=9999).contains(&y) {
        return None;
    }
    // 1970-01-01 was a Thursday.
    let wd = (days + 4).rem_euclid(7) as usize;
    const WD: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const MO: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    Some(format!(
        "{}, {:02} {} {:04} {:02}:{:02}:{:02} GMT",
        WD[wd],
        d,
        MO[(m - 1) as usize],
        y,
        rem / 3600,
        (rem / 60) % 60,
        rem % 60
    ))
}

struct Response {
    status: u16,
    headers: Vec<(String, String)>,
    body: Box<dyn Read>,
}

impl Response {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

struct ChunkedReader<R: BufRead> {
    inner: R,
    remaining: u64,
    done: bool,
}

impl<R: BufRead> Read for ChunkedReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let bad = |m: &str| std::io::Error::new(std::io::ErrorKind::InvalidData, m.to_string());
        if self.done {
            return Ok(0);
        }
        if self.remaining == 0 {
            let mut line = String::new();
            self.inner.read_line(&mut line)?;
            let hex = line.trim().split(';').next().unwrap_or("").trim();
            let n = u64::from_str_radix(hex, 16).map_err(|_| bad("bad chunk size"))?;
            if n == 0 {
                // trailers
                loop {
                    let mut l = String::new();
                    if self.inner.read_line(&mut l)? == 0 || l.trim().is_empty() {
                        break;
                    }
                }
                self.done = true;
                return Ok(0);
            }
            self.remaining = n;
        }
        let want = buf.len().min(self.remaining as usize);
        let got = self.inner.read(&mut buf[..want])?;
        if got == 0 {
            return Err(bad("truncated chunk"));
        }
        self.remaining -= got as u64;
        if self.remaining == 0 {
            let mut crlf = String::new();
            self.inner.read_line(&mut crlf)?;
        }
        Ok(got)
    }
}

fn send(url: &Uri, a: u64, b: u64, pins: &Pins, ims: &Option<String>) -> Result<Response, String> {
    let authority = url
        .authority
        .as_deref()
        .ok_or_else(|| "http URL has no authority".to_string())?;
    let (_userinfo, host, port) =
        uri::split_authority(authority).ok_or_else(|| "bad authority".to_string())?;
    if host.is_empty() {
        return Err("http URL has an empty host".into());
    }
    let port: u16 = match port {
        None | Some("") => 80,
        Some(p) => p.parse().map_err(|_| format!("bad port {:?}", p))?,
    };
    let connect_host = host.trim_start_matches('[').trim_end_matches(']');
    let connect_host = String::from_utf8_lossy(&uri::pct_decode(connect_host)).into_owned();
    let host_header = match authority.rfind('@') {
        Some(i) => &authority[i + 1..],
        None => authority,
    };
    let mut target = if url.path.is_empty() { "/".to_string() } else { url.path.clone() };
    if let Some(q) = &url.query {
        target.push('?');
        target.push_str(q);
    }
    let mut req = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nRange: bytes={}-{}\r\nAccept-Encoding: identity\r\n",
        target,
        host_header,
        a,
        b - 1
    );
    if let Some(e) = &pins.etag {
        req.push_str(&format!("If-Match: {}\r\n", e));
    }
    if let Some(d) = ims {
        req.push_str(&format!("If-Unmodified-Since: {}\r\n", d));
    }
    req.push_str("Connection: close\r\nUser-Agent: vzip-rust/0\r\n\r\n");

    let mut stream = TcpStream::connect((connect_host.as_str(), port))
        .map_err(|e| format!("cannot connect to {}:{}: {}", connect_host, port, e))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(60)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(60)));
    stream.write_all(req.as_bytes()).map_err(|e| format!("send failed: {}", e))?;
    let mut rd = BufReader::new(stream);

    loop {
        let mut status_line = String::new();
        rd.read_line(&mut status_line).map_err(|e| format!("read failed: {}", e))?;
        let mut parts = status_line.split_whitespace();
        let ver = parts.next().unwrap_or("");
        if !ver.starts_with("HTTP/") {
            return Err(format!("bad status line {:?}", status_line));
        }
        let status: u16 = parts
            .next()
            .and_then(|s| s.parse().ok())
            .ok_or_else(|| format!("bad status line {:?}", status_line))?;
        let mut headers = Vec::new();
        loop {
            let mut l = String::new();
            let n = rd.read_line(&mut l).map_err(|e| format!("read failed: {}", e))?;
            if n == 0 {
                return Err("connection closed in headers".into());
            }
            let l = l.trim_end_matches(['\r', '\n']);
            if l.is_empty() {
                break;
            }
            if let Some(i) = l.find(':') {
                headers.push((l[..i].trim().to_string(), l[i + 1..].trim().to_string()));
            }
        }
        if (100..200).contains(&status) && status != 101 {
            continue; // interim response
        }
        let chunked = headers.iter().any(|(k, v)| {
            k.eq_ignore_ascii_case("transfer-encoding") && v.to_ascii_lowercase().contains("chunked")
        });
        let clen = headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
            .and_then(|(_, v)| v.parse::<u64>().ok());
        let body: Box<dyn Read> = if chunked {
            Box::new(ChunkedReader { inner: rd, remaining: 0, done: false })
        } else if let Some(n) = clen {
            Box::new(rd.take(n))
        } else {
            Box::new(rd)
        };
        return Ok(Response { status, headers, body });
    }
}

fn parse_content_range(v: &str) -> Option<(u64, u64, Option<u64>)> {
    let v = v.trim();
    let rest = v.strip_prefix("bytes ").or_else(|| {
        if v.len() > 6 && v[..6].eq_ignore_ascii_case("bytes ") {
            Some(&v[6..])
        } else {
            None
        }
    })?;
    let (range, total) = rest.split_once('/')?;
    let (s, e) = range.trim().split_once('-')?;
    let s: u64 = s.trim().parse().ok()?;
    let e: u64 = e.trim().parse().ok()?;
    let total = match total.trim() {
        "*" => None,
        t => Some(t.parse().ok()?),
    };
    Some((s, e, total))
}

fn read_exact_body(body: &mut dyn Read, n: u64) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(n.min(1 << 24) as usize);
    let mut lim = body.take(n + 1);
    lim.read_to_end(&mut out).map_err(|e| format!("body read failed: {}", e))?;
    if out.len() as u64 != n {
        return Err(format!("response body has {} bytes, expected {}", out.len(), n));
    }
    Ok(out)
}

/// Reads bytes `[a, b)` (b > a) of the object at `url`, checking `pins`.
pub fn fetch_range(start_url: &Uri, a: u64, b: u64, pins: &Pins) -> Result<Vec<u8>, String> {
    let ims = match pins.modified_not_after {
        Some(t) => Some(imf_fixdate(t).ok_or_else(|| {
            "modified_not_after pin is outside years 1-9999 and cannot be sent".to_string()
        })?),
        None => None,
    };
    let mut url = start_url.clone();
    let mut redirects = 0;
    loop {
        let scheme = url.scheme.as_deref().unwrap_or("").to_ascii_lowercase();
        if scheme != "http" {
            return Err(format!("unsupported URL scheme {:?}", scheme));
        }
        let mut resp = send(&url, a, b, pins, &ims)?;
        match resp.status {
            301 | 302 | 303 | 307 | 308 => {
                redirects += 1;
                if redirects > MAX_REDIRECTS {
                    return Err("too many redirects".into());
                }
                let loc = resp
                    .header("location")
                    .ok_or_else(|| "redirect without Location".to_string())?;
                let r = uri::parse_uri_reference(loc)
                    .ok_or_else(|| format!("invalid redirect Location {:?}", loc))?;
                url = uri::resolve(&url, &r);
                continue;
            }
            _ => {}
        }
        if let Some(ce) = resp.header("content-encoding") {
            if !ce.trim().eq_ignore_ascii_case("identity") && !ce.trim().is_empty() {
                return Err(format!("response has Content-Encoding {:?}", ce));
            }
        }
        match resp.status {
            206 => {
                let cr = resp
                    .header("content-range")
                    .ok_or_else(|| "206 response without Content-Range".to_string())?;
                let (s, e, total) = parse_content_range(cr)
                    .ok_or_else(|| format!("unsupported Content-Range {:?}", cr))?;
                if s != a || e != b - 1 {
                    return Err(format!(
                        "server returned range {}-{}, requested {}-{}",
                        s,
                        e,
                        a,
                        b - 1
                    ));
                }
                if let Some(p) = pins.size {
                    match total {
                        None => return Err("size pin cannot be checked: object size unknown (*)".into()),
                        Some(t) if t != p => {
                            return Err(format!("size pin failed: object has {} bytes, pinned {}", t, p))
                        }
                        _ => {}
                    }
                }
                return read_exact_body(&mut resp.body, b - a);
            }
            200 => {
                // Whole object: keep [a, b), count the total.
                let mut out = Vec::new();
                let mut pos: u64 = 0;
                let mut buf = vec![0u8; 64 * 1024];
                loop {
                    let n = resp.body.read(&mut buf).map_err(|e| format!("body read failed: {}", e))?;
                    if n == 0 {
                        break;
                    }
                    let cs = pos;
                    let ce = pos + n as u64;
                    let lo = cs.max(a);
                    let hi = ce.min(b);
                    if lo < hi {
                        out.extend_from_slice(&buf[(lo - cs) as usize..(hi - cs) as usize]);
                    }
                    pos = ce;
                }
                if let Some(p) = pins.size {
                    if pos != p {
                        return Err(format!("size pin failed: object has {} bytes, pinned {}", pos, p));
                    }
                }
                if pos < b {
                    return Err(format!("object has {} bytes, shorter than {}", pos, b));
                }
                return Ok(out);
            }
            412 => return Err("precondition failed (412): pin failed".into()),
            416 => return Err("range not satisfiable (416): object shorter than requested".into()),
            s => return Err(format!("unexpected HTTP status {}", s)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dates() {
        assert_eq!(imf_fixdate(784111777).unwrap(), "Sun, 06 Nov 1994 08:49:37 GMT");
        assert_eq!(imf_fixdate(0).unwrap(), "Thu, 01 Jan 1970 00:00:00 GMT");
        assert_eq!(imf_fixdate(-1).unwrap(), "Wed, 31 Dec 1969 23:59:59 GMT");
        assert_eq!(imf_fixdate(-62135596800).unwrap(), "Mon, 01 Jan 0001 00:00:00 GMT");
        assert!(imf_fixdate(-62135596801).is_none());
        assert_eq!(imf_fixdate(253402300799).unwrap(), "Fri, 31 Dec 9999 23:59:59 GMT");
        assert!(imf_fixdate(253402300800).is_none());
    }
}
