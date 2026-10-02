//! Minimal HTTP/1.1 client (HEAD, ranged GET) with a block cache.

use crate::Error;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;

const BLOCK: u64 = 64 * 1024;

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

pub struct Http {
    addr: String,
    host_header: String,
    path: String,
    conn: Option<BufReader<TcpStream>>,
}

impl Http {
    pub fn new(url: &str) -> Result<Http, Error> {
        let rest = url
            .strip_prefix("http://")
            .ok_or_else(|| Error::Io(format!("only http:// URLs are supported: {url}")))?;
        let (authority, path) = match rest.find(['/', '?']) {
            Some(i) => (&rest[..i], rest[i..].to_string()),
            None => (rest, "/".to_string()),
        };
        let path = if path.starts_with('?') { format!("/{path}") } else { path };
        let authority = authority.rsplit('@').next().unwrap_or(authority);
        let addr = if authority.starts_with('[') {
            if authority.contains("]:") { authority.to_string() } else { format!("{authority}:80") }
        } else if authority.contains(':') {
            authority.to_string()
        } else {
            format!("{authority}:80")
        };
        Ok(Http { addr, host_header: authority.to_string(), path, conn: None })
    }

    fn connect(&mut self) -> Result<&mut BufReader<TcpStream>, Error> {
        if self.conn.is_none() {
            let s = TcpStream::connect(&self.addr)
                .map_err(|e| Error::Io(format!("connect {}: {e}", self.addr)))?;
            let _ = s.set_nodelay(true);
            self.conn = Some(BufReader::new(s));
        }
        Ok(self.conn.as_mut().unwrap())
    }

    fn request(&mut self, method: &str, range: Option<(u64, u64)>) -> Result<Response, Error> {
        let mut last = None;
        for _ in 0..3 {
            match self.try_request(method, range) {
                Ok(r) => return Ok(r),
                Err(e) => {
                    self.conn = None;
                    last = Some(e);
                }
            }
        }
        Err(Error::Io(last.unwrap()))
    }

    fn try_request(&mut self, method: &str, range: Option<(u64, u64)>) -> Result<Response, String> {
        let mut req = format!(
            "{method} {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: vzip-virtualize-rs\r\nAccept-Encoding: identity\r\nConnection: keep-alive\r\n",
            self.path, self.host_header
        );
        if let Some((a, b)) = range {
            req.push_str(&format!("Range: bytes={a}-{b}\r\n"));
        }
        req.push_str("\r\n");
        let conn = self.connect().map_err(|e| e.to_string())?;
        conn.get_mut().write_all(req.as_bytes()).map_err(|e| e.to_string())?;
        let mut line = String::new();
        if conn.read_line(&mut line).map_err(|e| e.to_string())? == 0 {
            return Err("connection closed".into());
        }
        let mut parts = line.split_whitespace();
        let _ver = parts.next();
        let status: u16 = parts
            .next()
            .and_then(|s| s.parse().ok())
            .ok_or_else(|| format!("bad status line {line:?}"))?;
        let mut headers = Vec::new();
        loop {
            let mut h = String::new();
            if conn.read_line(&mut h).map_err(|e| e.to_string())? == 0 {
                return Err("connection closed in headers".into());
            }
            let h = h.trim_end_matches(['\r', '\n']);
            if h.is_empty() {
                break;
            }
            if let Some((k, v)) = h.split_once(':') {
                headers.push((k.trim().to_string(), v.trim().to_string()));
            }
        }
        let mut resp = Response { status, headers, body: Vec::new() };
        let close = resp
            .header("Connection")
            .map(|v| v.eq_ignore_ascii_case("close"))
            .unwrap_or(false);
        if method != "HEAD" && status != 204 && status != 304 {
            if resp
                .header("Transfer-Encoding")
                .map(|v| v.to_ascii_lowercase().contains("chunked"))
                .unwrap_or(false)
            {
                loop {
                    let mut sz = String::new();
                    conn.read_line(&mut sz).map_err(|e| e.to_string())?;
                    let sz = sz.trim().split(';').next().unwrap_or("");
                    let n = usize::from_str_radix(sz, 16).map_err(|_| "bad chunk size")?;
                    if n == 0 {
                        loop {
                            let mut t = String::new();
                            conn.read_line(&mut t).map_err(|e| e.to_string())?;
                            if t.trim().is_empty() {
                                break;
                            }
                        }
                        break;
                    }
                    let start = resp.body.len();
                    resp.body.resize(start + n, 0);
                    conn.read_exact(&mut resp.body[start..]).map_err(|e| e.to_string())?;
                    let mut crlf = [0u8; 2];
                    conn.read_exact(&mut crlf).map_err(|e| e.to_string())?;
                }
            } else if let Some(cl) = resp.header("Content-Length") {
                let n: usize = cl.parse().map_err(|_| "bad Content-Length")?;
                resp.body.resize(n, 0);
                conn.read_exact(&mut resp.body).map_err(|e| e.to_string())?;
            } else {
                conn.read_to_end(&mut resp.body).map_err(|e| e.to_string())?;
                self.conn = None;
            }
        }
        if close {
            self.conn = None;
        }
        Ok(resp)
    }
}

/// Random-access reader over an HTTP resource, caching fixed-size blocks.
pub struct Source {
    http: Http,
    pub size: u64,
    cache: HashMap<u64, Vec<u8>>,
    pub requests: u64,
}

impl Source {
    pub fn open(url: &str) -> Result<Source, Error> {
        let mut http = Http::new(url)?;
        let r = http.request("HEAD", None)?;
        if r.status != 200 {
            return Err(Error::Io(format!("HEAD {url}: status {}", r.status)));
        }
        let size = r
            .header("Content-Length")
            .and_then(|v| v.parse().ok())
            .ok_or_else(|| Error::Io("HEAD: no Content-Length".into()))?;
        Ok(Source { http, size, cache: HashMap::new(), requests: 0 })
    }

    fn fetch(&mut self, first: u64, last: u64) -> Result<(), Error> {
        // fetch blocks first..=last (all missing) in one request
        let a = first * BLOCK;
        let b = ((last + 1) * BLOCK).min(self.size) - 1;
        self.requests += 1;
        let r = self.http.request("GET", Some((a, b)))?;
        if r.status != 206 {
            return Err(Error::Io(format!("GET bytes={a}-{b}: status {}", r.status)));
        }
        if let Some(cr) = r.header("Content-Range") {
            let want = format!("bytes {a}-{b}/");
            if !cr.starts_with(&want) {
                return Err(Error::Io(format!("unexpected Content-Range {cr}")));
            }
        }
        if r.body.len() as u64 != b - a + 1 {
            return Err(Error::Io(format!("short body for bytes={a}-{b}")));
        }
        for blk in first..=last {
            let s = (blk * BLOCK - a) as usize;
            let e = (((blk + 1) * BLOCK).min(self.size) - a) as usize;
            self.cache.insert(blk, r.body[s..e].to_vec());
        }
        Ok(())
    }

    /// Read exactly `len` bytes at `off`. Reading past the end is a rejection
    /// (the file is truncated or a pointer is invalid).
    pub fn read(&mut self, off: u64, len: u64) -> Result<Vec<u8>, Error> {
        let end = off
            .checked_add(len)
            .ok_or_else(|| Error::Reject(format!("read at {off}+{len} overflows")))?;
        if end > self.size {
            return Err(Error::Reject(format!(
                "read of {len} bytes at offset {off} is past end of file ({})",
                self.size
            )));
        }
        if len == 0 {
            return Ok(Vec::new());
        }
        let first = off / BLOCK;
        let last = (end - 1) / BLOCK;
        let mut blk = first;
        while blk <= last {
            if self.cache.contains_key(&blk) {
                blk += 1;
                continue;
            }
            let mut stop = blk;
            while stop < last && !self.cache.contains_key(&(stop + 1)) {
                stop += 1;
            }
            self.fetch(blk, stop)?;
            blk = stop + 1;
        }
        let mut out = Vec::with_capacity(len as usize);
        for blk in first..=last {
            let data = &self.cache[&blk];
            let bs = blk * BLOCK;
            let s = off.max(bs) - bs;
            let e = end.min(bs + data.len() as u64) - bs;
            out.extend_from_slice(&data[s as usize..e as usize]);
        }
        Ok(out)
    }
}
