//! Minimal HTTP/1.1 client implementing spec §6.2 (http: only).

use crate::httpdate;
use crate::uri::{parse_uri_reference, resolve, UriRef};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

#[derive(Debug, Clone, Default)]
pub struct Pins {
    pub size: Option<u64>,
    pub etag: Option<String>,
    pub modified_not_after: Option<i64>,
}

/// Maximum response size we buffer (a resource limit).
pub const MAX_RESPONSE: u64 = 1 << 31;

pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

fn find_crlfcrlf(b: &[u8], from: usize) -> Option<usize> {
    b[from..].windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + from)
}

fn decode_chunked(mut b: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    loop {
        let eol = b.windows(2).position(|w| w == b"\r\n").ok_or("bad chunked encoding")?;
        let line = std::str::from_utf8(&b[..eol]).map_err(|_| "bad chunk size")?;
        let size_str = line.split(';').next().unwrap_or("").trim();
        let size = u64::from_str_radix(size_str, 16).map_err(|_| "bad chunk size")?;
        b = &b[eol + 2..];
        if size == 0 {
            return Ok(out);
        }
        if (b.len() as u64) < size + 2 {
            return Err("truncated chunk".into());
        }
        out.extend_from_slice(&b[..size as usize]);
        b = &b[size as usize + 2..];
    }
}

fn send(uri: &UriRef, extra_headers: &[(String, String)]) -> Result<Response, String> {
    let auth = uri.authority.as_deref().ok_or("http: URL without authority")?;
    let hostport = match auth.rfind('@') {
        Some(i) => &auth[i + 1..],
        None => auth,
    };
    let (host, port) = if let Some(rest) = hostport.strip_prefix('[') {
        let end = rest.find(']').ok_or("bad IP literal")?;
        let port = rest[end + 1..].strip_prefix(':').unwrap_or("");
        (rest[..end].to_string(), port)
    } else {
        match hostport.find(':') {
            Some(i) => (hostport[..i].to_string(), &hostport[i + 1..]),
            None => (hostport.to_string(), ""),
        }
    };
    if host.is_empty() {
        return Err("http: URL with empty host".into());
    }
    let port: u16 = if port.is_empty() { 80 } else { port.parse().map_err(|_| "bad port")? };
    let addrs: Vec<_> = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|e| format!("cannot resolve host {host}: {e}"))?
        .collect();
    let mut stream = None;
    let mut last_err = String::from("no addresses");
    for a in addrs {
        match TcpStream::connect_timeout(&a, Duration::from_secs(30)) {
            Ok(s) => {
                stream = Some(s);
                break;
            }
            Err(e) => last_err = e.to_string(),
        }
    }
    let mut stream = stream.ok_or_else(|| format!("cannot connect to {hostport}: {last_err}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(60)));
    let mut target = if uri.path.is_empty() { "/".to_string() } else { uri.path.clone() };
    if let Some(q) = &uri.query {
        target.push('?');
        target.push_str(q);
    }
    let mut req = format!("GET {target} HTTP/1.1\r\nHost: {hostport}\r\nConnection: close\r\nUser-Agent: vzip-rs/0.1\r\n");
    for (k, v) in extra_headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes()).map_err(|e| format!("write failed: {e}"))?;
    let mut buf = Vec::new();
    (&mut stream)
        .take(MAX_RESPONSE)
        .read_to_end(&mut buf)
        .map_err(|e| format!("read failed: {e}"))?;

    // Skip interim 1xx responses.
    let mut start = 0;
    loop {
        let hend = find_crlfcrlf(&buf, start).ok_or("malformed HTTP response (no header end)")?;
        let head = std::str::from_utf8(&buf[start..hend]).map_err(|_| "non-UTF-8 HTTP headers")?;
        let mut lines = head.split("\r\n");
        let status_line = lines.next().unwrap_or("");
        let mut parts = status_line.splitn(3, ' ');
        let ver = parts.next().unwrap_or("");
        if !ver.starts_with("HTTP/") {
            return Err(format!("malformed status line {status_line:?}"));
        }
        let status: u16 = parts
            .next()
            .and_then(|s| s.parse().ok())
            .ok_or_else(|| format!("malformed status line {status_line:?}"))?;
        let mut headers = Vec::new();
        for l in lines {
            let (k, v) = l.split_once(':').ok_or("malformed header line")?;
            headers.push((k.trim().to_string(), v.trim().to_string()));
        }
        let body_start = hend + 4;
        if (100..200).contains(&status) {
            start = body_start;
            continue;
        }
        let mut resp = Response { status, headers, body: Vec::new() };
        let rest = &buf[body_start..];
        if resp
            .header("Transfer-Encoding")
            .map(|t| t.to_ascii_lowercase().contains("chunked"))
            .unwrap_or(false)
        {
            resp.body = decode_chunked(rest)?;
        } else if let Some(cl) = resp.header("Content-Length") {
            let n: usize = cl.parse().map_err(|_| "bad Content-Length")?;
            if rest.len() < n {
                return Err("truncated HTTP body".into());
            }
            resp.body = rest[..n].to_vec();
        } else {
            resp.body = rest.to_vec();
        }
        return Ok(resp);
    }
}

fn check_content_encoding(r: &Response) -> Result<(), String> {
    for (k, v) in &r.headers {
        if k.eq_ignore_ascii_case("Content-Encoding") {
            for tok in v.split(',') {
                let t = tok.trim();
                if !t.is_empty() && !t.eq_ignore_ascii_case("identity") {
                    return Err(format!("unsupported Content-Encoding {v:?}"));
                }
            }
        }
    }
    Ok(())
}

fn check_pin_headers(r: &Response, pins: &Pins) -> Result<(), String> {
    if let Some(etag) = &pins.etag {
        match r.header("ETag") {
            None => return Err("etag pin cannot be checked: response has no ETag".into()),
            Some(got) => {
                // Strong comparison: both strong, identical.
                if got.starts_with("W/") || got != etag {
                    return Err(format!("etag pin failed: expected {etag}, got {got}"));
                }
            }
        }
    }
    if let Some(mna) = pins.modified_not_after {
        let lm = r
            .header("Last-Modified")
            .ok_or("modified_not_after pin cannot be checked: no Last-Modified")?;
        let t = httpdate::parse_http_date(lm)
            .ok_or_else(|| format!("modified_not_after pin cannot be checked: bad Last-Modified {lm:?}"))?;
        if t > mna {
            return Err(format!("modified_not_after pin failed: Last-Modified {lm} is after pin {mna}"));
        }
    }
    Ok(())
}

fn parse_content_range(v: &str) -> Option<(u64, u64, Option<u64>)> {
    let rest = v.trim().strip_prefix("bytes ")?.trim_start();
    let (range, total) = rest.split_once('/')?;
    let (a, z) = range.split_once('-')?;
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit());
    if !digits(a) || !digits(z) {
        return None;
    }
    let total = if total == "*" {
        None
    } else if digits(total) {
        Some(total.parse().ok()?)
    } else {
        return None;
    };
    Some((a.parse().ok()?, z.parse().ok()?, total))
}

/// Read bytes `[a, b)` (a < b) of the HTTP object at `uri`, checking pins.
pub fn read_range(uri: &UriRef, a: u64, b: u64, pins: &Pins) -> Result<Vec<u8>, String> {
    assert!(a < b);
    let mut headers = vec![
        ("Range".to_string(), format!("bytes={}-{}", a, b - 1)),
        ("Accept-Encoding".to_string(), "identity".to_string()),
    ];
    if let Some(e) = &pins.etag {
        headers.push(("If-Match".to_string(), e.clone()));
    }
    if let Some(m) = pins.modified_not_after {
        let d = httpdate::format_imf_fixdate(m).ok_or_else(|| {
            format!("modified_not_after pin {m} is outside years 1-9999 and cannot be sent")
        })?;
        headers.push(("If-Unmodified-Since".to_string(), d));
    }
    let mut current = UriRef { fragment: None, ..uri.clone() };
    let mut redirects = 0;
    loop {
        match current.scheme_lower().as_deref() {
            Some("http") => {}
            Some("https") => return Err("https: is not supported by this reader".into()),
            other => return Err(format!("unsupported scheme {other:?}")),
        }
        let resp = send(&current, &headers)?;
        match resp.status {
            301 | 302 | 303 | 307 | 308 => {
                if redirects == 5 {
                    return Err("too many redirects (more than 5)".into());
                }
                redirects += 1;
                let loc = resp.header("Location").ok_or("redirect without Location")?;
                let r = parse_uri_reference(loc)
                    .ok_or_else(|| format!("invalid Location {loc:?}"))?;
                let next = resolve(&current, &r);
                match next.scheme_lower().as_deref() {
                    Some("http") | Some("https") => {}
                    _ => return Err(format!("redirect to non-HTTP URL {loc:?}")),
                }
                current = UriRef { fragment: None, ..next };
            }
            200 => {
                check_content_encoding(&resp)?;
                check_pin_headers(&resp, pins)?;
                let n = resp.body.len() as u64;
                if let Some(s) = pins.size {
                    if s != n {
                        return Err(format!("size pin failed: expected {s}, object is {n} bytes"));
                    }
                }
                if b > n {
                    return Err(format!("object is {n} bytes, shorter than {b}"));
                }
                return Ok(resp.body[a as usize..b as usize].to_vec());
            }
            206 => {
                check_content_encoding(&resp)?;
                let cr = resp.header("Content-Range").ok_or("206 without Content-Range")?;
                let (ra, rz, total) =
                    parse_content_range(cr).ok_or_else(|| format!("bad Content-Range {cr:?}"))?;
                if ra != a || rz != b - 1 {
                    return Err(format!("server returned range {ra}-{rz}, requested {a}-{}", b - 1));
                }
                if resp.body.len() as u64 != b - a {
                    return Err("206 body length does not match Content-Range".into());
                }
                check_pin_headers(&resp, pins)?;
                if let Some(s) = pins.size {
                    match total {
                        None => return Err("size pin cannot be checked: Content-Range total is *".into()),
                        Some(t) if t != s => {
                            return Err(format!("size pin failed: expected {s}, object is {t} bytes"))
                        }
                        _ => {}
                    }
                }
                if let Some(t) = total {
                    if t < b {
                        return Err("Content-Range total smaller than requested range".into());
                    }
                }
                return Ok(resp.body);
            }
            412 => return Err("412 Precondition Failed: a pin failed".into()),
            416 => return Err("416 Range Not Satisfiable: object shorter than requested range".into()),
            s => return Err(format!("unexpected HTTP status {s}")),
        }
    }
}
