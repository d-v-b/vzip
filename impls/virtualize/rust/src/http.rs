//! Minimal HTTP/1.1 client with a block cache, for range reads of one URL.
use crate::common::{Error, MAX_SAFE, Res};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

const BLOCK: u64 = 1 << 16;

pub struct Source {
    host: String,
    port: u16,
    path: String,
    pub size: u64,
    cache: HashMap<u64, Vec<u8>>,
}

fn fail<T>(m: impl Into<String>) -> Res<T> {
    Err(Error::Fail(m.into()))
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

impl Source {
    pub fn open(url: &str) -> Res<Source> {
        let rest = match url.strip_prefix("http://") {
            Some(r) => r,
            None => return fail(format!("only http:// URLs are supported: {url}")),
        };
        let (auth, path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let path = match path.find('#') {
            Some(i) => &path[..i],
            None => path,
        };
        let (host, port) = match auth.rfind(':') {
            Some(i) if !auth.ends_with(']') => {
                let p: u16 = match auth[i + 1..].parse() {
                    Ok(p) => p,
                    Err(_) => return fail("bad port"),
                };
                (auth[..i].to_string(), p)
            }
            _ => (auth.to_string(), 80),
        };
        let mut s = Source { host, port, path: path.to_string(), size: 0, cache: HashMap::new() };
        let r = s.request("HEAD", None)?;
        if r.status != 200 {
            return fail(format!("HEAD returned {}", r.status));
        }
        s.size = match r.header("content-length").and_then(|v| v.trim().parse::<u64>().ok()) {
            Some(n) => n,
            None => return fail("HEAD without Content-Length"),
        };
        Ok(s)
    }

    fn request(&self, method: &str, range: Option<(u64, u64)>) -> Res<Response> {
        let mut last_err = String::new();
        for attempt in 0..3 {
            if attempt > 0 {
                std::thread::sleep(Duration::from_millis(500));
            }
            match self.request_once(method, range) {
                Ok(r) => return Ok(r),
                Err(e) => last_err = e,
            }
        }
        fail(last_err)
    }

    fn request_once(&self, method: &str, range: Option<(u64, u64)>) -> Result<Response, String> {
        let mut st = TcpStream::connect((self.host.as_str(), self.port)).map_err(|e| e.to_string())?;
        st.set_read_timeout(Some(Duration::from_secs(1800))).ok();
        let mut req = format!(
            "{method} {} HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\nAccept-Encoding: identity\r\n",
            self.path, self.host, self.port
        );
        if let Some((a, b)) = range {
            req.push_str(&format!("Range: bytes={a}-{b}\r\n"));
        }
        req.push_str("\r\n");
        st.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
        let mut buf = Vec::new();
        let mut tmp = [0u8; 65536];
        let hdr_end = loop {
            if let Some(p) = find(&buf, b"\r\n\r\n") {
                break p;
            }
            let n = st.read(&mut tmp).map_err(|e| e.to_string())?;
            if n == 0 {
                return Err("connection closed in headers".into());
            }
            buf.extend_from_slice(&tmp[..n]);
        };
        let head = String::from_utf8_lossy(&buf[..hdr_end]).to_string();
        let mut lines = head.split("\r\n");
        let status_line = lines.next().unwrap_or("");
        let status: u16 = status_line
            .split_whitespace()
            .nth(1)
            .and_then(|s| s.parse().ok())
            .ok_or("bad status line")?;
        let headers: Vec<(String, String)> = lines
            .filter_map(|l| l.split_once(':').map(|(k, v)| (k.trim().to_string(), v.trim().to_string())))
            .collect();
        let mut resp = Response { status, headers, body: Vec::new() };
        if method == "HEAD" {
            return Ok(resp);
        }
        let mut body = buf[hdr_end + 4..].to_vec();
        let chunked = resp
            .header("transfer-encoding")
            .map(|v| v.to_ascii_lowercase().contains("chunked"))
            .unwrap_or(false);
        if chunked {
            loop {
                let n = st.read(&mut tmp).map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                body.extend_from_slice(&tmp[..n]);
            }
            resp.body = dechunk(&body)?;
        } else if let Some(cl) = resp.header("content-length").and_then(|v| v.parse::<usize>().ok()) {
            while body.len() < cl {
                let n = st.read(&mut tmp).map_err(|e| e.to_string())?;
                if n == 0 {
                    return Err("connection closed in body".into());
                }
                body.extend_from_slice(&tmp[..n]);
            }
            body.truncate(cl);
            resp.body = body;
        } else {
            loop {
                let n = st.read(&mut tmp).map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                body.extend_from_slice(&tmp[..n]);
            }
            resp.body = body;
        }
        Ok(resp)
    }

    fn fetch(&self, a: u64, b_incl: u64) -> Res<Vec<u8>> {
        let r = self.request("GET", Some((a, b_incl)))?;
        if r.status != 206 {
            return fail(format!("GET range {a}-{b_incl} returned {}", r.status));
        }
        if r.body.len() as u64 != b_incl - a + 1 {
            return fail(format!("GET range {a}-{b_incl}: got {} bytes", r.body.len()));
        }
        Ok(r.body)
    }

    /// Reads `len` bytes at `off`; a read outside the file rejects the input.
    pub fn read(&mut self, off: u64, len: u64) -> Res<Vec<u8>> {
        if off > MAX_SAFE || len > MAX_SAFE {
            crate::rej!("read at {off} (+{len}): offset or length above 2^53-1");
        }
        if off + len > self.size {
            crate::rej!("read at {off} (+{len}) outside the file (size {})", self.size);
        }
        if len == 0 {
            return Ok(Vec::new());
        }
        let first = off / BLOCK;
        let last = (off + len - 1) / BLOCK;
        // fetch missing runs
        let mut b = first;
        while b <= last {
            if self.cache.contains_key(&b) {
                b += 1;
                continue;
            }
            let mut e = b;
            while e < last && !self.cache.contains_key(&(e + 1)) {
                e += 1;
            }
            let a = b * BLOCK;
            let end = ((e + 1) * BLOCK).min(self.size);
            let data = self.fetch(a, end - 1)?;
            for (i, blk) in (b..=e).enumerate() {
                let s = i * BLOCK as usize;
                let t = (s + BLOCK as usize).min(data.len());
                self.cache.insert(blk, data[s..t].to_vec());
            }
            b = e + 1;
        }
        let mut out = Vec::with_capacity(len as usize);
        for blk in first..=last {
            let d = &self.cache[&blk];
            let bstart = blk * BLOCK;
            let s = off.max(bstart) - bstart;
            let e = (off + len).min(bstart + d.len() as u64) - bstart;
            out.extend_from_slice(&d[s as usize..e as usize]);
        }
        Ok(out)
    }
}

fn find(h: &[u8], n: &[u8]) -> Option<usize> {
    h.windows(n.len()).position(|w| w == n)
}

fn dechunk(b: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let mut p = 0;
    loop {
        let le = find(&b[p..], b"\r\n").ok_or("bad chunked body")? + p;
        let line = std::str::from_utf8(&b[p..le]).map_err(|e| e.to_string())?;
        let sz = usize::from_str_radix(line.split(';').next().unwrap().trim(), 16).map_err(|e| e.to_string())?;
        p = le + 2;
        if sz == 0 {
            return Ok(out);
        }
        if p + sz > b.len() {
            return Err("truncated chunk".into());
        }
        out.extend_from_slice(&b[p..p + sz]);
        p += sz + 2;
    }
}
