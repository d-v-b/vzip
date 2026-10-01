//! vzip: a ZIP container for byte-range references (format version 0).

pub mod http;
pub mod httpdate;
pub mod json;
pub mod proto;
pub mod reader;
pub mod uri;
pub mod writer;

pub use reader::{Archive, Kind, Request};

pub const RESERVED_PREFIX: &[u8] = b"__vz__/";
pub const SOURCES_KEY: &[u8] = b"__vz__/sources";
pub const INDEX_KEY: &[u8] = b"__vz__/index";
pub const EXTRA_RANGE: u16 = 0x7A76;
pub const EXTRA_CONCAT: u16 = 0x7A77;
pub const MAX_PAYLOAD: usize = 65519;

/// Resource limit (§10): the largest value window, inflated body, or HTTP
/// body a single operation may materialise. 1 GiB.
pub const MEMORY_LIMIT: u64 = 1 << 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorClass {
    Archive,
    Entry,
    Body,
    Payload,
    Resolution,
    Request,
}

impl ErrorClass {
    pub fn name(self) -> &'static str {
        match self {
            ErrorClass::Archive => "archive",
            ErrorClass::Entry => "entry",
            ErrorClass::Body => "body",
            ErrorClass::Payload => "payload",
            ErrorClass::Resolution => "resolution",
            ErrorClass::Request => "request",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Error {
    pub class: ErrorClass,
    pub msg: String,
}

impl Error {
    pub fn new(class: ErrorClass, msg: impl Into<String>) -> Self {
        Error { class, msg: msg.into() }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} error: {}", self.class.name(), self.msg)
    }
}

impl std::error::Error for Error {}

/// Is `s` a strong entity tag `DQUOTE *etagc DQUOTE` (§6.1)?
pub fn is_strong_etag(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() >= 2
        && b[0] == b'"'
        && b[b.len() - 1] == b'"'
        && b[1..b.len() - 1].iter().all(|&c| c == 0x21 || (0x23..=0x7E).contains(&c))
}

/// Inflates a raw DEFLATE body, requiring a single complete stream that ends
/// exactly at the end of `data` (§8.1). Output beyond `limit` bytes fails.
pub fn inflate_clean(data: &[u8], limit: u64) -> Result<Vec<u8>, String> {
    use flate2::{Decompress, FlushDecompress, Status};
    let mut d = Decompress::new(false);
    let cap_limit = limit.saturating_add(1);
    let mut out: Vec<u8> = Vec::new();
    loop {
        if out.len() as u64 >= cap_limit {
            return Err(format!("DEFLATE body inflates to more than {limit} bytes"));
        }
        let want = (cap_limit - out.len() as u64).min(1 << 20) as usize;
        if out.capacity() - out.len() < want {
            out.reserve(want);
        }
        let (ti, to) = (d.total_in(), d.total_out());
        let st = d
            .decompress_vec(&data[ti as usize..], &mut out, FlushDecompress::None)
            .map_err(|e| format!("invalid DEFLATE stream: {e}"))?;
        if st == Status::StreamEnd {
            break;
        }
        if d.total_in() == ti && d.total_out() == to {
            if ti as usize >= data.len() {
                return Err("DEFLATE stream is truncated (no final block)".into());
            }
            if out.capacity() > out.len() {
                return Err("DEFLATE stream made no progress".into());
            }
        }
    }
    if out.len() as u64 > limit {
        return Err(format!("DEFLATE body inflates to more than {limit} bytes"));
    }
    if d.total_in() as usize != data.len() {
        return Err(format!("{} trailing bytes after DEFLATE stream", data.len() - d.total_in() as usize));
    }
    Ok(out)
}

pub fn deflate(data: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut e = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inflate_checks() {
        let d = deflate(b"hello hello hello");
        assert_eq!(inflate_clean(&d, 100).unwrap(), b"hello hello hello");
        let mut t = d.clone();
        t.push(0);
        assert!(inflate_clean(&t, 100).is_err());
        assert!(inflate_clean(&d[..d.len() - 1], 100).is_err());
        assert!(inflate_clean(&d, 5).is_err());
        assert_eq!(inflate_clean(&deflate(b""), 0).unwrap(), b"");
        assert!(inflate_clean(&[], 10).is_err());
    }
    #[test]
    fn etags() {
        assert!(is_strong_etag("\"abc\""));
        assert!(is_strong_etag("\"\""));
        assert!(!is_strong_etag("W/\"abc\""));
        assert!(!is_strong_etag("\"a b\""));
        assert!(!is_strong_etag("\"a\"b\""));
        assert!(!is_strong_etag("\""));
    }
}
