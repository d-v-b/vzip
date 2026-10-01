#![allow(dead_code)]
//! Test helpers: a raw ZIP builder for crafting invalid archives, a tiny HTTP
//! server, and temp dirs.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use vzip::proto::{self, Source};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

pub fn tmpdir() -> PathBuf {
    let base = std::env::var("CARGO_TARGET_TMPDIR").map(PathBuf::from).unwrap_or_else(|_| std::env::temp_dir());
    let d = base.join(format!("vzip-test-{}-{}", std::process::id(), COUNTER.fetch_add(1, Ordering::SeqCst)));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

pub fn deflate(data: &[u8]) -> Vec<u8> {
    let mut e = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

#[derive(Clone)]
pub struct RawEntry {
    pub name: Vec<u8>,
    pub method: u16,
    pub flags: u16,
    /// Stored body (already compressed if method 8).
    pub body: Vec<u8>,
    pub usize: u32,
    pub csize: Option<u32>,
    pub extra: Vec<u8>,
    pub lho_override: Option<u32>,
}

impl RawEntry {
    pub fn stored(name: &str, data: &[u8]) -> Self {
        RawEntry { name: name.as_bytes().to_vec(), method: 0, flags: 0x800, body: data.to_vec(), usize: data.len() as u32, csize: None, extra: vec![], lho_override: None }
    }
    pub fn deflated(name: &str, data: &[u8]) -> Self {
        RawEntry { name: name.as_bytes().to_vec(), method: 8, flags: 0x800, body: deflate(data), usize: data.len() as u32, csize: None, extra: vec![], lho_override: None }
    }
    pub fn reference(name: &str, id: u16, payload: &[u8]) -> Self {
        let mut e = RawEntry::stored(name, &[]);
        e.extra = block(id, payload);
        e
    }
}

pub fn block(id: u16, data: &[u8]) -> Vec<u8> {
    let mut v = id.to_le_bytes().to_vec();
    v.extend_from_slice(&(data.len() as u16).to_le_bytes());
    v.extend_from_slice(data);
    v
}

pub struct RawSpec {
    pub entries: Vec<RawEntry>,
    /// Compressed source table body.
    pub sources_body: Vec<u8>,
    /// Builds the plain CdIndex bytes from (name, cd offset, record length) of
    /// body records (sorted) and their body offsets. `None`: unpaged.
    pub index: Option<Box<dyn Fn(&[(Vec<u8>, u64, u64, u64)]) -> Vec<u8>>>,
    pub comment_override: Option<Vec<u8>>,
}

impl RawSpec {
    pub fn new(entries: Vec<RawEntry>, sources: &[Source]) -> Self {
        RawSpec { entries, sources_body: deflate(&proto::encode_source_table(sources)), index: None, comment_override: None }
    }
}

fn cdrec(e: &RawEntry, lho: u32) -> Vec<u8> {
    let mut o = Vec::new();
    o.extend_from_slice(&0x02014b50u32.to_le_bytes());
    o.extend_from_slice(&20u16.to_le_bytes());
    o.extend_from_slice(&20u16.to_le_bytes());
    o.extend_from_slice(&e.flags.to_le_bytes());
    o.extend_from_slice(&e.method.to_le_bytes());
    o.extend_from_slice(&[0, 0, 0x21, 0]);
    o.extend_from_slice(&crc32fast::hash(&e.body).to_le_bytes());
    o.extend_from_slice(&e.csize.unwrap_or(e.body.len() as u32).to_le_bytes());
    o.extend_from_slice(&e.usize.to_le_bytes());
    o.extend_from_slice(&(e.name.len() as u16).to_le_bytes());
    o.extend_from_slice(&(e.extra.len() as u16).to_le_bytes());
    o.extend_from_slice(&[0; 6]);
    o.extend_from_slice(&[0; 4]);
    o.extend_from_slice(&e.lho_override.unwrap_or(lho).to_le_bytes());
    o.extend_from_slice(&e.name);
    o.extend_from_slice(&e.extra);
    o
}

fn local(out: &mut Vec<u8>, e: &RawEntry) -> u32 {
    let lho = out.len() as u32;
    out.extend_from_slice(&0x04034b50u32.to_le_bytes());
    out.extend_from_slice(&20u16.to_le_bytes());
    out.extend_from_slice(&e.flags.to_le_bytes());
    out.extend_from_slice(&e.method.to_le_bytes());
    out.extend_from_slice(&[0, 0, 0x21, 0]);
    out.extend_from_slice(&crc32fast::hash(&e.body).to_le_bytes());
    out.extend_from_slice(&(e.body.len() as u32).to_le_bytes());
    out.extend_from_slice(&e.usize.to_le_bytes());
    out.extend_from_slice(&(e.name.len() as u16).to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&e.name);
    out.extend_from_slice(&e.body);
    lho
}

pub fn build_raw(spec: &RawSpec) -> Vec<u8> {
    let mut out = Vec::new();
    let mut placed: Vec<(RawEntry, u32)> = Vec::new();
    for e in &spec.entries {
        let lho = local(&mut out, e);
        placed.push((e.clone(), lho));
    }
    let src = RawEntry { name: b"__vz__/sources".to_vec(), method: 8, flags: 0x800, body: spec.sources_body.clone(), usize: 0, csize: None, extra: vec![], lho_override: None };
    let src_lho = local(&mut out, &src);
    let src_off = src_lho as u64 + 30 + 14;
    placed.sort_by(|a, b| a.0.name.cmp(&b.0.name));
    let recs: Vec<Vec<u8>> = placed.iter().map(|(e, l)| cdrec(e, *l)).collect();
    let mut fmt = vec![cdrec(&src, src_lho)];
    let mut idx_loc = None;
    if let Some(f) = &spec.index {
        let mut info = Vec::new();
        let mut off = 0u64;
        for ((e, l), r) in placed.iter().zip(&recs) {
            info.push((e.name.clone(), off, r.len() as u64, *l as u64 + 30 + e.name.len() as u64));
            off += r.len() as u64;
        }
        let plain = f(&info);
        let ie = RawEntry { name: b"__vz__/index".to_vec(), method: 8, flags: 0x800, body: deflate(&plain), usize: plain.len() as u32, csize: None, extra: vec![], lho_override: None };
        let l = local(&mut out, &ie);
        idx_loc = Some((l as u64 + 30 + 12, ie.body.len() as u64));
        fmt.push(cdrec(&ie, l));
    }
    let cd_off = out.len() as u32;
    let n = recs.len() + fmt.len();
    for r in recs.iter().chain(fmt.iter()) {
        out.extend_from_slice(r);
    }
    let cd_size = out.len() as u32 - cd_off;
    let comment = spec.comment_override.clone().unwrap_or_else(|| {
        let mut c = b"vzip/0".to_vec();
        c.extend_from_slice(&src_off.to_le_bytes());
        c.extend_from_slice(&(spec.sources_body.len() as u64).to_le_bytes());
        if let Some((o, s)) = idx_loc {
            c.extend_from_slice(&o.to_le_bytes());
            c.extend_from_slice(&s.to_le_bytes());
        }
        c
    });
    out.extend_from_slice(&0x06054b50u32.to_le_bytes());
    out.extend_from_slice(&[0; 4]);
    out.extend_from_slice(&(n as u16).to_le_bytes());
    out.extend_from_slice(&(n as u16).to_le_bytes());
    out.extend_from_slice(&cd_size.to_le_bytes());
    out.extend_from_slice(&cd_off.to_le_bytes());
    out.extend_from_slice(&(comment.len() as u16).to_le_bytes());
    out.extend_from_slice(&comment);
    out
}

/// A page index with one page per record, and the given pinned list.
pub fn one_page_per_record(info: &[(Vec<u8>, u64, u64, u64)]) -> proto::CdIndex {
    proto::CdIndex {
        pages: info
            .iter()
            .map(|(n, o, l, _)| proto::Page { first_key: String::from_utf8(n.clone()).unwrap(), offset: *o, length: *l })
            .collect(),
        pinned: vec![],
    }
}

// ---------------------------------------------------------------------------
// HTTP server

#[derive(Debug, Clone)]
pub struct HttpRequest {
    pub method: String,
    pub path: String,
    pub headers: Vec<(String, String)>,
}

impl HttpRequest {
    pub fn header(&self, n: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(n)).map(|(_, v)| v.as_str())
    }
}

pub struct Server {
    pub port: u16,
    pub log: Arc<Mutex<Vec<HttpRequest>>>,
}

pub type Handler = Arc<dyn Fn(&HttpRequest) -> Vec<u8> + Send + Sync>;

pub fn serve(handler: Handler) -> Server {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    let log = Arc::new(Mutex::new(Vec::new()));
    let log2 = log.clone();
    std::thread::spawn(move || {
        for s in l.incoming() {
            let mut s = match s {
                Ok(s) => s,
                Err(_) => continue,
            };
            let mut buf = Vec::new();
            let mut tmp = [0u8; 4096];
            while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = match s.read(&mut tmp) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => n,
                };
                buf.extend_from_slice(&tmp[..n]);
            }
            let text = String::from_utf8_lossy(&buf).to_string();
            let mut lines = text.split("\r\n");
            let rl: Vec<&str> = lines.next().unwrap_or("").split(' ').collect();
            let mut headers = Vec::new();
            for l in lines {
                if l.is_empty() {
                    break;
                }
                if let Some((k, v)) = l.split_once(':') {
                    headers.push((k.trim().to_string(), v.trim().to_string()));
                }
            }
            let req = HttpRequest { method: rl.first().unwrap_or(&"").to_string(), path: rl.get(1).unwrap_or(&"").to_string(), headers };
            log2.lock().unwrap().push(req.clone());
            let resp = handler(&req);
            let _ = s.write_all(&resp);
        }
    });
    Server { port, log }
}

pub fn response(status: u16, headers: &[(&str, String)], body: &[u8]) -> Vec<u8> {
    let mut r = format!("HTTP/1.1 {status} X\r\nContent-Length: {}\r\nConnection: close\r\n", body.len());
    for (k, v) in headers {
        r.push_str(&format!("{k}: {v}\r\n"));
    }
    r.push_str("\r\n");
    let mut b = r.into_bytes();
    b.extend_from_slice(body);
    b
}

/// Serve `obj` honouring Range (single range) with the given extra headers.
pub fn range_response(req: &HttpRequest, obj: &[u8], extra: &[(&str, String)]) -> Vec<u8> {
    if let Some(r) = req.header("Range") {
        let spec = r.strip_prefix("bytes=").unwrap();
        let (a, b) = spec.split_once('-').unwrap();
        let a: usize = a.parse().unwrap();
        let b: usize = b.parse().unwrap();
        if a >= obj.len() {
            return response(416, &[("Content-Range", format!("bytes */{}", obj.len()))], b"");
        }
        let b = b.min(obj.len() - 1);
        let mut h: Vec<(&str, String)> = vec![("Content-Range", format!("bytes {a}-{b}/{}", obj.len()))];
        h.extend_from_slice(extra);
        return response(206, &h, &obj[a..=b]);
    }
    response(200, extra, obj)
}
