//! Minimal HTTP/1.1 client for byte-range reads (§6.2).
//!
//! Hand-written so that every header sent and every response header checked is
//! under our control (no automatic decompression, no hidden redirects).

use crate::uri::{self, Uri};
use crate::{Error, ErrorClass};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::time::Duration;

pub struct Pins<'a> {
    pub size: Option<u64>,
    pub etag: Option<&'a str>,
    pub modified_not_after: Option<i64>,
}

fn res(msg: impl Into<String>) -> Error {
    Error::new(ErrorClass::Resolution, msg)
}

struct Response {
    status: u16,
    headers: Vec<(String, String)>,
    reader: BufReader<TcpStream>,
}

impl Response {
    fn all(&self, name: &str) -> Vec<&str> {
        self.headers.iter().filter(|(n, _)| n.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str()).collect()
    }
    fn one(&self, name: &str) -> Result<Option<&str>, Error> {
        let v = self.all(name);
        match v.len() {
            0 => Ok(None),
            1 => Ok(Some(v[0])),
            _ => Err(res(format!("several {name} headers in response"))),
        }
    }

    /// Reads the body, refusing more than `limit` bytes (a request error).
    fn body(mut self, limit: u64) -> Result<Vec<u8>, Error> {
        let te = self.all("transfer-encoding").join(",");
        let chunked = te
            .rsplit(',')
            .next()
            .map(|s| s.trim().eq_ignore_ascii_case("chunked"))
            .unwrap_or(false);
        let io = |e: std::io::Error| res(format!("HTTP read failed: {e}"));
        let too_big = || Error::new(ErrorClass::Request, format!("HTTP body exceeds the {limit}-byte limit"));
        let mut out = Vec::new();
        if chunked {
            loop {
                let mut line = String::new();
                self.reader.read_line(&mut line).map_err(io)?;
                let sz = line.trim_end().split(';').next().unwrap_or("").trim();
                let n = u64::from_str_radix(sz, 16).map_err(|_| res("bad chunk size"))?;
                if n == 0 {
                    // trailers
                    loop {
                        let mut l = String::new();
                        let k = self.reader.read_line(&mut l).map_err(io)?;
                        if k == 0 || l == "\r\n" || l == "\n" {
                            break;
                        }
                    }
                    break;
                }
                if out.len() as u64 + n > limit {
                    return Err(too_big());
                }
                let start = out.len();
                out.resize(start + n as usize, 0);
                self.reader.read_exact(&mut out[start..]).map_err(io)?;
                let mut crlf = String::new();
                self.reader.read_line(&mut crlf).map_err(io)?;
            }
        } else if !te.is_empty() {
            (&mut self.reader).take(limit + 1).read_to_end(&mut out).map_err(io)?;
            if out.len() as u64 > limit {
                return Err(too_big());
            }
        } else {
            let cls = self.all("content-length");
            let mut cl: Option<u64> = None;
            for v in cls {
                for p in v.split(',') {
                    let n: u64 = p.trim().parse().map_err(|_| res("bad Content-Length"))?;
                    if cl.is_some_and(|c| c != n) {
                        return Err(res("conflicting Content-Length"));
                    }
                    cl = Some(n);
                }
            }
            match cl {
                Some(n) => {
                    if n > limit {
                        return Err(too_big());
                    }
                    out.resize(n as usize, 0);
                    self.reader.read_exact(&mut out).map_err(io)?;
                }
                None => {
                    (&mut self.reader).take(limit + 1).read_to_end(&mut out).map_err(io)?;
                    if out.len() as u64 > limit {
                        return Err(too_big());
                    }
                }
            }
        }
        Ok(out)
    }
}

fn send(url: &Uri, headers: &[(String, String)]) -> Result<Response, Error> {
    let auth = url.authority.as_deref().unwrap_or("");
    if auth.contains('@') {
        return Err(res("HTTP URL with userinfo"));
    }
    let (host, port) = uri::split_host_port(auth);
    if host.is_empty() {
        return Err(res("HTTP URL with empty host"));
    }
    let port: u16 = match port {
        None | Some("") => 80,
        Some(p) => p.parse().map_err(|_| res(format!("bad port {p:?}")))?,
    };
    let connect_host = host.trim_start_matches('[').trim_end_matches(']');
    let stream = TcpStream::connect((connect_host, port)).map_err(|e| res(format!("connect to {auth} failed: {e}")))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(60)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(60)));
    let target = format!(
        "{}{}",
        if url.path.is_empty() { "/" } else { &url.path },
        url.query.as_ref().map(|q| format!("?{q}")).unwrap_or_default()
    );
    let host_hdr = match port {
        80 => host.to_string(),
        p => format!("{host}:{p}"),
    };
    let mut req = format!("GET {target} HTTP/1.1\r\nHost: {host_hdr}\r\n");
    for (k, v) in headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    req.push_str("User-Agent: vzip-rs/0\r\nConnection: close\r\n\r\n");
    (&stream).write_all(req.as_bytes()).map_err(|e| res(format!("HTTP write failed: {e}")))?;
    let mut reader = BufReader::new(stream);
    loop {
        let mut status_line = String::new();
        reader.read_line(&mut status_line).map_err(|e| res(format!("HTTP read failed: {e}")))?;
        let mut parts = status_line.trim_end().splitn(3, ' ');
        let ver = parts.next().unwrap_or("");
        if !ver.starts_with("HTTP/1.") {
            return Err(res(format!("bad HTTP status line {status_line:?}")));
        }
        let status: u16 = parts
            .next()
            .and_then(|s| if s.len() == 3 { s.parse().ok() } else { None })
            .ok_or_else(|| res(format!("bad HTTP status line {status_line:?}")))?;
        let mut hdrs = Vec::new();
        let mut total = 0usize;
        loop {
            let mut l = String::new();
            let n = reader.read_line(&mut l).map_err(|e| res(format!("HTTP read failed: {e}")))?;
            total += n;
            if total > 1 << 20 {
                return Err(res("HTTP headers too large"));
            }
            if n == 0 {
                return Err(res("connection closed in headers"));
            }
            let l = l.trim_end_matches(['\r', '\n']);
            if l.is_empty() {
                break;
            }
            let (k, v) = l.split_once(':').ok_or_else(|| res("bad header line"))?;
            hdrs.push((k.trim().to_string(), v.trim().to_string()));
        }
        if (100..200).contains(&status) && status != 101 {
            continue; // interim response
        }
        return Ok(Response { status, headers: hdrs, reader });
    }
}

fn parse_content_range(v: &str) -> Option<(u64, u64, Option<u64>)> {
    let (unit, rest) = v.trim().split_once(' ')?;
    if !unit.eq_ignore_ascii_case("bytes") {
        return None;
    }
    let (range, total) = rest.trim_start().split_once('/')?;
    let (a, z) = range.split_once('-')?;
    let num = |s: &str| -> Option<u64> {
        if !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit()) { s.parse().ok() } else { None }
    };
    let a = num(a)?;
    let z = num(z)?;
    let total = if total == "*" { None } else { Some(num(total)?) };
    if z < a {
        return None;
    }
    Some((a, z, total))
}

/// Reads bytes `[a, b)` (b > a) of the object at `url`, checking `pins`.
pub fn read_range(url: &Uri, a: u64, b: u64, pins: &Pins, limit: u64) -> Result<Vec<u8>, Error> {
    let mut headers = vec![
        ("Range".to_string(), format!("bytes={}-{}", a, b - 1)),
        ("Accept-Encoding".to_string(), "identity".to_string()),
    ];
    if let Some(e) = pins.etag {
        headers.push(("If-Match".into(), e.to_string()));
    }
    if let Some(m) = pins.modified_not_after {
        let d = crate::httpdate::format(m)
            .ok_or_else(|| res("modified_not_after pin outside years 1-9999 cannot be sent"))?;
        headers.push(("If-Unmodified-Since".into(), d));
    }
    let mut cur = url.clone();
    cur.fragment = None;
    let mut redirects = 0;
    loop {
        let scheme = cur.scheme.as_deref().unwrap_or("").to_ascii_lowercase();
        if scheme == "https" {
            return Err(res("https: is not supported by this reader"));
        }
        if scheme != "http" {
            return Err(res(format!("unsupported scheme {scheme:?}")));
        }
        let resp = send(&cur, &headers)?;
        match resp.status {
            301 | 302 | 303 | 307 | 308 => {
                redirects += 1;
                if redirects > 5 {
                    return Err(res("more than 5 redirects"));
                }
                let loc = resp.one("location")?.ok_or_else(|| res("redirect without Location"))?;
                let r = uri::parse_reference(loc).map_err(|e| res(format!("bad Location: {e}")))?;
                let mut next = uri::resolve(&cur, &r);
                next.fragment = None;
                let s = next.scheme.as_deref().unwrap_or("").to_ascii_lowercase();
                if s != "http" && s != "https" {
                    return Err(res(format!("redirect to non-HTTP scheme {s:?}")));
                }
                cur = next;
                continue;
            }
            200 | 206 => {}
            412 => return Err(res("precondition failed (412): pin failed")),
            416 => return Err(res("range not satisfiable (416): object too short")),
            s => return Err(res(format!("HTTP status {s}"))),
        }
        let ce = resp.all("content-encoding");
        if !ce.is_empty() {
            let joined = ce.join(", ");
            if !joined.trim().eq_ignore_ascii_case("identity") {
                return Err(res(format!("response has Content-Encoding {joined:?}")));
            }
        }
        if let Some(pin) = pins.etag {
            match resp.one("etag")? {
                Some(e) if e == pin => {}
                Some(e) => return Err(res(format!("ETag {e:?} does not match pin {pin:?}"))),
                None => return Err(res("response has no ETag; etag pin cannot be checked")),
            }
        }
        if let Some(pin) = pins.modified_not_after {
            let lm = resp.one("last-modified")?.ok_or_else(|| res("response has no Last-Modified"))?;
            let t = crate::httpdate::parse(lm).ok_or_else(|| res(format!("unparseable Last-Modified {lm:?}")))?;
            if t > pin {
                return Err(res(format!("Last-Modified {lm:?} is after modified_not_after pin")));
            }
        }
        if resp.status == 206 {
            let crs = resp.all("content-range");
            if crs.len() != 1 {
                return Err(res("206 response without exactly one Content-Range"));
            }
            let (ra, rz, total) =
                parse_content_range(crs[0]).ok_or_else(|| res(format!("bad Content-Range {:?}", crs[0])))?;
            if ra != a || rz != b - 1 {
                return Err(res(format!("server returned range {ra}-{rz}, requested {a}-{}", b - 1)));
            }
            if let Some(t) = total {
                if rz >= t {
                    return Err(res("Content-Range end not less than total"));
                }
            }
            if let Some(p) = pins.size {
                match total {
                    Some(t) if t == p => {}
                    Some(t) => return Err(res(format!("object size {t} does not match size pin {p}"))),
                    None => return Err(res("object size unknown (*); size pin cannot be checked")),
                }
            }
            let body = resp.body(limit)?;
            if body.len() as u64 != rz - ra + 1 {
                return Err(res("206 body length does not match Content-Range"));
            }
            return Ok(body);
        }
        // 200: whole object
        let body = resp.body(limit)?;
        if let Some(p) = pins.size {
            if body.len() as u64 != p {
                return Err(res(format!("object size {} does not match size pin {p}", body.len())));
            }
        }
        if (body.len() as u64) < b {
            return Err(res(format!("object has {} bytes, need {b}", body.len())));
        }
        return Ok(body[a as usize..b as usize].to_vec());
    }
}
