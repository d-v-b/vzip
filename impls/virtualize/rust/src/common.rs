//! Errors, the HTTP-backed byte source, JSON values and small helpers.

use std::cell::RefCell;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

/// A rejection (exit 3) or a failure (network error, bug; exit 2).
#[derive(Debug)]
pub enum E {
    Reject(String),
    Fail(String),
}

pub type R<T> = Result<T, E>;

#[macro_export]
macro_rules! rej {
    ($($t:tt)*) => { return Err($crate::common::E::Reject(format!($($t)*))) };
}

pub fn reject<T>(msg: impl Into<String>) -> R<T> {
    Err(E::Reject(msg.into()))
}

pub const MAX_SAFE: u64 = (1u64 << 53) - 1;

/// Rejects an offset or length above 2^53 - 1 (VIRTUALIZE.md §1.2).
pub fn safe(v: u64, what: &str) -> R<u64> {
    if v > MAX_SAFE {
        rej!("{what} {v} is above 2^53 - 1");
    }
    Ok(v)
}

// ---------------------------------------------------------------- HTTP

struct Url {
    host: String,
    port: u16,
    path: String,
}

fn parse_url(url: &str) -> R<Url> {
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| E::Fail(format!("only http:// URLs are supported: {url}")))?;
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let path = path.split('#').next().unwrap_or("/");
    let (host, port) = match authority.rfind(':') {
        Some(i) if !authority[i + 1..].is_empty() && authority[i + 1..].bytes().all(|b| b.is_ascii_digit()) => (
            authority[..i].to_string(),
            authority[i + 1..].parse().map_err(|_| E::Fail("bad port".into()))?,
        ),
        _ => (authority.to_string(), 80),
    };
    Ok(Url { host, port, path: path.to_string() })
}

struct Response {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Response {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

fn find_sub(h: &[u8], n: &[u8]) -> Option<usize> {
    h.windows(n.len()).position(|w| w == n)
}

fn http_once(u: &Url, method: &str, range: Option<(u64, u64)>) -> Result<Response, String> {
    let mut s = TcpStream::connect((u.host.as_str(), u.port)).map_err(|e| e.to_string())?;
    s.set_read_timeout(Some(Duration::from_secs(600))).ok();
    let host = if u.port == 80 { u.host.clone() } else { format!("{}:{}", u.host, u.port) };
    let mut req = format!("{method} {} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nAccept-Encoding: identity\r\n", u.path);
    if let Some((a, b)) = range {
        req.push_str(&format!("Range: bytes={a}-{b}\r\n"));
    }
    req.push_str("\r\n");
    s.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut buf = Vec::new();
    let mut tmp = [0u8; 65536];
    // Read the headers.
    let hend = loop {
        if let Some(i) = find_sub(&buf, b"\r\n\r\n") {
            break i;
        }
        let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("connection closed before headers".into());
        }
        buf.extend_from_slice(&tmp[..n]);
    };
    let head = String::from_utf8_lossy(&buf[..hend]).to_string();
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap_or("");
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|x| x.parse().ok())
        .ok_or_else(|| format!("bad status line {status_line:?}"))?;
    let headers: Vec<(String, String)> = lines
        .filter_map(|l| l.split_once(':').map(|(k, v)| (k.trim().to_string(), v.trim().to_string())))
        .collect();
    let mut resp = Response { status, headers, body: Vec::new() };
    if method == "HEAD" {
        return Ok(resp);
    }
    let mut body = buf[hend + 4..].to_vec();
    let chunked = resp
        .header("transfer-encoding")
        .map(|v| v.to_ascii_lowercase().contains("chunked"))
        .unwrap_or(false);
    let clen: Option<usize> = if chunked { None } else { resp.header("content-length").and_then(|v| v.parse().ok()) };
    loop {
        if let Some(l) = clen {
            if body.len() >= l {
                body.truncate(l);
                break;
            }
        }
        let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&tmp[..n]);
    }
    if let Some(l) = clen {
        if body.len() != l {
            return Err("short body".into());
        }
    }
    if chunked {
        let mut out = Vec::new();
        let mut i = 0;
        loop {
            let j = find_sub(&body[i..], b"\r\n").ok_or("bad chunked encoding")? + i;
            let szs = String::from_utf8_lossy(&body[i..j]);
            let sz = usize::from_str_radix(szs.split(';').next().unwrap_or("").trim(), 16)
                .map_err(|_| "bad chunk size")?;
            i = j + 2;
            if sz == 0 {
                break;
            }
            if i + sz > body.len() {
                return Err("truncated chunk".into());
            }
            out.extend_from_slice(&body[i..i + sz]);
            i += sz + 2;
        }
        body = out;
    }
    resp.body = body;
    Ok(resp)
}

fn http(u: &Url, method: &str, range: Option<(u64, u64)>) -> R<Response> {
    let mut last = String::new();
    for attempt in 0..4 {
        if attempt > 0 {
            std::thread::sleep(Duration::from_millis(500 * attempt));
        }
        match http_once(u, method, range) {
            Ok(r) if r.status >= 500 => last = format!("HTTP status {}", r.status),
            Ok(r) => return Ok(r),
            Err(e) => last = e,
        }
    }
    Err(E::Fail(format!("{method} failed: {last}")))
}

// ---------------------------------------------------------------- Source

const BLOCK: u64 = 256 * 1024;

/// The input file, read over HTTP with a block cache.
pub struct Source {
    url: Url,
    pub size: u64,
    cache: RefCell<HashMap<u64, Vec<u8>>>,
}

impl Source {
    pub fn open(url: &str) -> R<Source> {
        let u = parse_url(url)?;
        let r = http(&u, "HEAD", None)?;
        if r.status != 200 {
            return Err(E::Fail(format!("HEAD returned {}", r.status)));
        }
        let size = r
            .header("content-length")
            .and_then(|v| v.parse::<u64>().ok())
            .ok_or_else(|| E::Fail("HEAD without Content-Length".into()))?;
        Ok(Source { url: u, size, cache: RefCell::new(HashMap::new()) })
    }

    fn fetch_blocks(&self, first: u64, last: u64) -> R<()> {
        let a = first * BLOCK;
        let b = ((last + 1) * BLOCK).min(self.size) - 1;
        let r = http(&self.url, "GET", Some((a, b)))?;
        if r.status != 206 {
            return Err(E::Fail(format!("GET range returned {}", r.status)));
        }
        if r.body.len() as u64 != b - a + 1 {
            return Err(E::Fail("range response has the wrong length".into()));
        }
        let mut c = self.cache.borrow_mut();
        for blk in first..=last {
            let s = (blk * BLOCK - a) as usize;
            let e = (((blk + 1) * BLOCK).min(self.size) - a) as usize;
            c.insert(blk, r.body[s..e].to_vec());
        }
        Ok(())
    }

    /// Reads `len` bytes at `off`; a read outside the file rejects the input.
    pub fn read(&self, off: u64, len: u64) -> R<Vec<u8>> {
        let end = off.checked_add(len);
        match end {
            Some(e) if e <= self.size => {}
            _ => rej!("read of {len} bytes at {off} is outside the file (size {})", self.size),
        }
        if len == 0 {
            return Ok(Vec::new());
        }
        let first = off / BLOCK;
        let last = (off + len - 1) / BLOCK;
        // Fetch missing runs of blocks.
        let mut blk = first;
        while blk <= last {
            if self.cache.borrow().contains_key(&blk) {
                blk += 1;
                continue;
            }
            let mut end = blk;
            while end < last && !self.cache.borrow().contains_key(&(end + 1)) {
                end += 1;
            }
            self.fetch_blocks(blk, end)?;
            blk = end + 1;
        }
        let mut out = Vec::with_capacity(len as usize);
        let c = self.cache.borrow();
        for blk in first..=last {
            let data = &c[&blk];
            let bs = blk * BLOCK;
            let s = off.max(bs) - bs;
            let e = (off + len).min(bs + data.len() as u64) - bs;
            out.extend_from_slice(&data[s as usize..e as usize]);
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------- JSON

#[derive(Clone, Debug)]
pub enum J {
    Bool(bool),
    U(u64),
    F(f64),
    S(String),
    A(Vec<J>),
    O(Vec<(String, J)>),
}

pub fn obj(members: Vec<(&str, J)>) -> J {
    J::O(members.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn s(v: &str) -> J {
    J::S(v.to_string())
}

fn esc(out: &mut String, v: &str) {
    out.push('"');
    for ch in v.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

impl J {
    pub fn write(&self, out: &mut String) {
        match self {
            J::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            J::U(v) => out.push_str(&v.to_string()),
            J::F(v) => {
                assert!(v.is_finite());
                // Debug formatting is the shortest round-trip representation.
                out.push_str(&format!("{v:?}"));
            }
            J::S(v) => esc(out, v),
            J::A(items) => {
                out.push('[');
                for (i, it) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    it.write(out);
                }
                out.push(']');
            }
            J::O(members) => {
                out.push('{');
                for (i, (k, v)) in members.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    esc(out, k);
                    out.push_str(": ");
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

/// Checks that a computed number is finite (§1.3).
pub fn finite(v: f64, what: &str) -> R<f64> {
    if !v.is_finite() {
        rej!("{what} is not finite");
    }
    Ok(v)
}

pub fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let b = [c[0], *c.get(1).unwrap_or(&0), *c.get(2).unwrap_or(&0)];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | b[2] as u32;
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if c.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if c.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    out
}

// ---------------------------------------------------------------- Output

pub enum Entry {
    Ranges(Vec<(u64, u64)>),
    Json(J),
    Bytes(Vec<u8>),
}

pub struct Output {
    pub entries: Vec<(String, Entry)>,
}

fn varint_len(mut v: u64) -> u64 {
    let mut n = 1;
    while v >= 0x80 {
        v >>= 7;
        n += 1;
    }
    n
}

/// Size of an encoded `Range` with source 0 (SPEC.md §5).
fn range_msg_len(off: u64, len: u64) -> u64 {
    let mut n = 0;
    if off != 0 {
        n += 1 + varint_len(off);
    }
    if len != 0 {
        n += 1 + varint_len(len);
    }
    n
}

/// Size of the reference payload for a list of ranges (SPEC.md §4.3, §5).
pub fn payload_len(ranges: &[(u64, u64)]) -> u64 {
    if ranges.len() == 1 {
        return range_msg_len(ranges[0].0, ranges[0].1);
    }
    ranges
        .iter()
        .map(|&(o, l)| {
            let m = range_msg_len(o, l);
            1 + varint_len(m) + m
        })
        .sum()
}

impl Output {
    pub fn new() -> Output {
        Output { entries: Vec::new() }
    }

    pub fn json(&mut self, key: &str, v: J) {
        self.entries.push((key.to_string(), Entry::Json(v)));
    }

    /// Adds a reference entry, checking §1.2 "References stay in the file".
    pub fn refs(&mut self, key: String, ranges: Vec<(u64, u64)>, file_size: u64) -> R<()> {
        for &(o, l) in &ranges {
            safe(o, "offset")?;
            safe(l, "length")?;
            match o.checked_add(l) {
                Some(e) if e <= file_size => {}
                _ => rej!("range ({o}, {l}) of {key} is outside the file"),
            }
        }
        let p = payload_len(&ranges);
        if p > 65519 {
            rej!("reference payload of {key} is {p} bytes, above 65519");
        }
        self.entries.push((key, Entry::Ranges(ranges)));
        Ok(())
    }

    pub fn to_json(&self, url: &str) -> String {
        let mut out = String::new();
        out.push_str("{\"sources\": [");
        esc(&mut out, url);
        out.push_str("],\n\"entries\": {\n");
        for (i, (k, e)) in self.entries.iter().enumerate() {
            if i > 0 {
                out.push_str(",\n");
            }
            esc(&mut out, k);
            out.push_str(": ");
            match e {
                Entry::Ranges(r) => {
                    out.push_str("{\"ranges\": [");
                    for (j, (o, l)) in r.iter().enumerate() {
                        if j > 0 {
                            out.push_str(", ");
                        }
                        out.push_str(&format!("[0, {o}, {l}]"));
                    }
                    out.push_str("]}");
                }
                Entry::Json(v) => {
                    out.push_str("{\"json\": ");
                    v.write(&mut out);
                    out.push('}');
                }
                Entry::Bytes(b) => {
                    out.push_str("{\"base64\": \"");
                    out.push_str(&base64(b));
                    out.push_str("\"}");
                }
            }
        }
        out.push_str("\n}}\n");
        out
    }
}

// ---------------------------------------------------------------- Zarr / OME helpers

/// The array-to-bytes `bytes` codec (§2.1).
pub fn bytes_codec(item_size: u32, little: bool) -> J {
    if item_size == 1 {
        obj(vec![("name", s("bytes"))])
    } else {
        obj(vec![
            ("name", s("bytes")),
            ("configuration", obj(vec![("endian", s(if little { "little" } else { "big" }))])),
        ])
    }
}

/// The `transpose` codec moving `c` (at index `c_index`) last (§2.1).
pub fn transpose_codec(naxes: usize, c_index: usize) -> J {
    let mut order: Vec<J> = (0..naxes).filter(|&i| i != c_index).map(|i| J::U(i as u64)).collect();
    order.push(J::U(c_index as u64));
    obj(vec![("name", s("transpose")), ("configuration", obj(vec![("order", J::A(order))]))])
}

pub fn zlib_codec() -> J {
    obj(vec![("name", s("zlib")), ("configuration", obj(vec![("level", J::U(1))]))])
}

pub fn zstd_codec() -> J {
    obj(vec![
        ("name", s("zstd")),
        ("configuration", obj(vec![("level", J::U(0)), ("checksum", J::Bool(false))])),
    ])
}

pub fn array_json(shape: &[u64], dtype: &str, chunk: &[u64], codecs: Vec<J>, dims: &[&str]) -> J {
    obj(vec![
        ("zarr_format", J::U(3)),
        ("node_type", s("array")),
        ("shape", J::A(shape.iter().map(|&v| J::U(v)).collect())),
        ("data_type", s(dtype)),
        (
            "chunk_grid",
            obj(vec![
                ("name", s("regular")),
                ("configuration", obj(vec![("chunk_shape", J::A(chunk.iter().map(|&v| J::U(v)).collect()))])),
            ]),
        ),
        (
            "chunk_key_encoding",
            obj(vec![("name", s("default")), ("configuration", obj(vec![("separator", s("/"))]))]),
        ),
        ("fill_value", J::U(0)),
        ("codecs", J::A(codecs)),
        ("dimension_names", J::A(dims.iter().map(|d| s(d)).collect())),
        ("attributes", J::O(vec![])),
    ])
}

pub struct Axis {
    pub name: &'static str,
    pub unit: Option<&'static str>,
}

fn axis_type(name: &str) -> &'static str {
    match name {
        "t" => "time",
        "c" => "channel",
        _ => "space",
    }
}

/// An image group's `zarr.json` (§2.2). `scales[level][axis]`.
pub fn image_json(name: Option<String>, axes: &[Axis], scales: &[Vec<f64>], omero: Option<J>) -> J {
    let mut ms: Vec<(&str, J)> = Vec::new();
    if let Some(n) = name {
        ms.push(("name", J::S(n)));
    }
    ms.push((
        "axes",
        J::A(
            axes.iter()
                .map(|a| {
                    let mut m = vec![("name", s(a.name)), ("type", s(axis_type(a.name)))];
                    if let Some(u) = a.unit {
                        m.push(("unit", s(u)));
                    }
                    obj(m)
                })
                .collect(),
        ),
    ));
    ms.push((
        "datasets",
        J::A(
            scales
                .iter()
                .enumerate()
                .map(|(i, sc)| {
                    obj(vec![
                        ("path", J::S(i.to_string())),
                        (
                            "coordinateTransformations",
                            J::A(vec![obj(vec![
                                ("type", s("scale")),
                                ("scale", J::A(sc.iter().map(|&v| J::F(v)).collect())),
                            ])]),
                        ),
                    ])
                })
                .collect(),
        ),
    ));
    let mut ome = vec![("version", s("0.5")), ("multiscales", J::A(vec![obj(ms)]))];
    if let Some(o) = omero {
        ome.push(("omero", o));
    }
    obj(vec![
        ("zarr_format", J::U(3)),
        ("node_type", s("group")),
        ("attributes", obj(vec![("ome", obj(ome))])),
    ])
}
