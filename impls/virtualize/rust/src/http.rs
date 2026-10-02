// Minimal HTTP/1.1 client with block cache (plain http only).

use crate::common::{E, R};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

const BS: u64 = 1 << 16;

pub struct Reader {
    host: String,
    port: u16,
    path: String,
    pub size: u64,
    cache: HashMap<u64, Vec<u8>>,
    pub requests: u64,
}

fn fail<T>(msg: impl Into<String>) -> R<T> {
    Err(E::Fail(msg.into()))
}

struct Resp {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Reader {
    pub fn open(url: &str) -> R<Reader> {
        let rest = match url.strip_prefix("http://") {
            Some(r) => r,
            None => return fail(format!("only http:// URLs are supported: {}", url)),
        };
        let (authority, path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let path = match path.find('#') {
            Some(i) => &path[..i],
            None => path,
        };
        let (host, port) = match authority.rfind(':') {
            Some(i) if !authority[i + 1..].contains(']') => {
                let p: u16 = match authority[i + 1..].parse() {
                    Ok(p) => p,
                    Err(_) => return fail("bad port"),
                };
                (&authority[..i], p)
            }
            _ => (authority, 80),
        };
        let mut r = Reader {
            host: host.to_string(),
            port,
            path: path.to_string(),
            size: 0,
            cache: HashMap::new(),
            requests: 0,
        };
        let resp = r.request("HEAD", None)?;
        if resp.status != 200 {
            return fail(format!("HEAD returned status {}", resp.status));
        }
        let cl = header(&resp.headers, "content-length");
        r.size = match cl.and_then(|v| v.trim().parse::<u64>().ok()) {
            Some(v) => v,
            None => return fail("HEAD without Content-Length"),
        };
        Ok(r)
    }

    fn request(&mut self, method: &str, range: Option<(u64, u64)>) -> R<Resp> {
        let mut last = String::new();
        for attempt in 0..4 {
            if attempt > 0 {
                std::thread::sleep(Duration::from_millis(500 << attempt));
            }
            match self.request_once(method, range) {
                Ok(r) => return Ok(r),
                Err(e) => last = e,
            }
        }
        fail(format!("{} request failed: {}", method, last))
    }

    fn request_once(&mut self, method: &str, range: Option<(u64, u64)>) -> Result<Resp, String> {
        self.requests += 1;
        let mut s = TcpStream::connect((self.host.as_str(), self.port)).map_err(|e| e.to_string())?;
        s.set_read_timeout(Some(Duration::from_secs(600))).map_err(|e| e.to_string())?;
        let hosthdr = if self.port == 80 { self.host.clone() } else { format!("{}:{}", self.host, self.port) };
        let mut req = format!(
            "{} {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nAccept-Encoding: identity\r\n",
            method, self.path, hosthdr
        );
        if let Some((a, b)) = range {
            req.push_str(&format!("Range: bytes={}-{}\r\n", a, b));
        }
        req.push_str("\r\n");
        s.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
        let mut buf = Vec::new();
        s.read_to_end(&mut buf).map_err(|e| e.to_string())?;
        let hend = buf
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .ok_or("no end of headers")?;
        let head = String::from_utf8_lossy(&buf[..hend]).to_string();
        let mut lines = head.split("\r\n");
        let status_line = lines.next().ok_or("empty response")?;
        let status: u16 = status_line
            .split_whitespace()
            .nth(1)
            .and_then(|v| v.parse().ok())
            .ok_or("bad status line")?;
        let headers: Vec<(String, String)> = lines
            .filter_map(|l| l.split_once(':').map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_string())))
            .collect();
        let mut body = buf[hend + 4..].to_vec();
        if method != "HEAD" {
            if let Some(te) = header(&headers, "transfer-encoding") {
                if te.to_ascii_lowercase().contains("chunked") {
                    return Err("chunked transfer encoding not supported".into());
                }
            }
            if let Some(cl) = header(&headers, "content-length").and_then(|v| v.parse::<usize>().ok()) {
                if body.len() < cl {
                    return Err("truncated body".into());
                }
                body.truncate(cl);
            }
        } else {
            body.clear();
        }
        Ok(Resp { status, headers, body })
    }

    fn fetch(&mut self, b0: u64, b1: u64) -> R<()> {
        let start = b0 * BS;
        let end = ((b1 + 1) * BS).min(self.size);
        let resp = self.request("GET", Some((start, end - 1)))?;
        if resp.status != 206 {
            return fail(format!("GET returned status {}", resp.status));
        }
        if resp.body.len() as u64 != end - start {
            return fail("short range response");
        }
        for b in b0..=b1 {
            let s = (b * BS - start) as usize;
            let e = (((b + 1) * BS).min(self.size) - start) as usize;
            self.cache.insert(b, resp.body[s..e].to_vec());
        }
        Ok(())
    }

    /// Read `len` bytes at `off`; a read outside the file rejects the input.
    pub fn read(&mut self, off: u64, len: u64) -> R<Vec<u8>> {
        match off.checked_add(len) {
            Some(e) if e <= self.size => {}
            _ => return Err(E::Reject(format!("read outside the file: {} bytes at {}", len, off))),
        }
        if len == 0 {
            return Ok(Vec::new());
        }
        let b0 = off / BS;
        let b1 = (off + len - 1) / BS;
        let mut b = b0;
        while b <= b1 {
            if self.cache.contains_key(&b) {
                b += 1;
                continue;
            }
            let s = b;
            while b <= b1 && !self.cache.contains_key(&b) && b - s < 256 {
                b += 1;
            }
            self.fetch(s, b - 1)?;
        }
        let mut out = Vec::with_capacity(len as usize);
        for b in b0..=b1 {
            let blk = &self.cache[&b];
            let bs = b * BS;
            let s = off.max(bs) - bs;
            let e = (off + len).min(bs + blk.len() as u64) - bs;
            out.extend_from_slice(&blk[s as usize..e as usize]);
        }
        Ok(out)
    }
}

fn header<'a>(h: &'a [(String, String)], name: &str) -> Option<&'a str> {
    h.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
}
