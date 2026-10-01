//! Hand-written protobuf wire format encoding/decoding for the vzip schema
//! (SPEC §5, Appendix A).

pub type PResult<T> = Result<T, String>;

#[derive(Debug, Clone)]
pub enum Wire<'a> {
    Varint(u64),
    I64,
    Len(&'a [u8]),
    I32,
}

fn read_varint(buf: &[u8], pos: &mut usize) -> PResult<u64> {
    let mut v: u64 = 0;
    for i in 0..10 {
        let b = *buf.get(*pos).ok_or("truncated varint")?;
        *pos += 1;
        if i == 9 {
            if b & 0x80 != 0 {
                return Err("varint longer than 10 bytes".into());
            }
            if b > 1 {
                return Err("varint exceeds 2^64-1".into());
            }
        }
        v |= ((b & 0x7f) as u64) << (7 * i);
        if b & 0x80 == 0 {
            return Ok(v);
        }
    }
    unreachable!()
}

/// Parse a message into its (field number, value) list, checking the
/// message-independent rules of §5.1.
pub fn parse_fields(buf: &[u8]) -> PResult<Vec<(u32, Wire<'_>)>> {
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos < buf.len() {
        let tag = read_varint(buf, &mut pos)?;
        let field = tag >> 3;
        let wt = tag & 7;
        if field == 0 || field > (1 << 29) - 1 {
            return Err(format!("invalid field number {field}"));
        }
        let field = field as u32;
        let v = match wt {
            0 => Wire::Varint(read_varint(buf, &mut pos)?),
            1 => {
                if buf.len() - pos < 8 {
                    return Err("truncated I64".into());
                }
                pos += 8;
                Wire::I64
            }
            2 => {
                let len = read_varint(buf, &mut pos)?;
                if len > (buf.len() - pos) as u64 {
                    return Err("LEN field extends past end of message".into());
                }
                let s = &buf[pos..pos + len as usize];
                pos += len as usize;
                Wire::Len(s)
            }
            5 => {
                if buf.len() - pos < 4 {
                    return Err("truncated I32".into());
                }
                pos += 4;
                Wire::I32
            }
            _ => return Err(format!("invalid wire type {wt}")),
        };
        out.push((field, v));
    }
    Ok(out)
}

fn want_varint(f: u32, w: &Wire) -> PResult<u64> {
    match w {
        Wire::Varint(v) => Ok(*v),
        _ => Err(format!("field {f}: wrong wire type (expected VARINT)")),
    }
}
fn want_u32(f: u32, w: &Wire) -> PResult<u32> {
    let v = want_varint(f, w)?;
    u32::try_from(v).map_err(|_| format!("field {f}: uint32 overflow"))
}
fn want_len<'a>(f: u32, w: &Wire<'a>) -> PResult<&'a [u8]> {
    match w {
        Wire::Len(s) => Ok(s),
        _ => Err(format!("field {f}: wrong wire type (expected LEN)")),
    }
}
fn want_str(f: u32, w: &Wire) -> PResult<String> {
    let b = want_len(f, w)?;
    String::from_utf8(b.to_vec()).map_err(|_| format!("field {f}: string is not valid UTF-8"))
}

// ---------------------------------------------------------------- encoding

pub fn put_varint(out: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        out.push((v as u8) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}
fn put_tag(out: &mut Vec<u8>, field: u32, wt: u8) {
    put_varint(out, ((field as u64) << 3) | wt as u64);
}
fn put_uint(out: &mut Vec<u8>, field: u32, v: u64, always: bool) {
    if v != 0 || always {
        put_tag(out, field, 0);
        put_varint(out, v);
    }
}
fn put_bytes(out: &mut Vec<u8>, field: u32, v: &[u8], always: bool) {
    if !v.is_empty() || always {
        put_tag(out, field, 2);
        put_varint(out, v.len() as u64);
        out.extend_from_slice(v);
    }
}

// ---------------------------------------------------------------- messages

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Range {
    pub source: u32,
    pub offset: u64,
    pub length: u64,
    pub data: Option<Vec<u8>>,
}

impl Range {
    pub fn size(&self) -> u64 {
        match &self.data {
            Some(d) => d.len() as u64,
            None => self.length,
        }
    }
    /// Decode and check against the number of sources (§5.2).
    pub fn decode(buf: &[u8], nsources: usize) -> PResult<Range> {
        let mut r = Range::default();
        for (f, w) in parse_fields(buf)? {
            match f {
                1 => r.source = want_u32(f, &w)?,
                3 => r.offset = want_varint(f, &w)?,
                4 => r.length = want_varint(f, &w)?,
                5 => r.data = Some(want_len(f, &w)?.to_vec()),
                _ => {}
            }
        }
        if r.data.is_some() {
            if r.source != 0 || r.offset != 0 || r.length != 0 {
                return Err("literal range with non-zero source, offset or length".into());
            }
        } else {
            if (r.source as usize) >= nsources {
                return Err(format!(
                    "source index {} out of bounds ({} sources)",
                    r.source, nsources
                ));
            }
            if r.offset.checked_add(r.length).is_none() {
                return Err("offset + length exceeds 2^64-1".into());
            }
        }
        Ok(r)
    }
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        put_uint(&mut out, 1, self.source as u64, false);
        put_uint(&mut out, 3, self.offset, false);
        put_uint(&mut out, 4, self.length, false);
        if let Some(d) = &self.data {
            put_bytes(&mut out, 5, d, true);
        }
        out
    }
}

pub fn decode_concat(buf: &[u8], nsources: usize) -> PResult<Vec<Range>> {
    let mut parts = Vec::new();
    let mut total: u64 = 0;
    for (f, w) in parse_fields(buf)? {
        if f == 1 {
            let r = Range::decode(want_len(f, &w)?, nsources)?;
            total = total
                .checked_add(r.size())
                .ok_or("concat size exceeds 2^64-1")?;
            parts.push(r);
        }
    }
    Ok(parts)
}

pub fn encode_concat(parts: &[Range]) -> Vec<u8> {
    let mut out = Vec::new();
    for p in parts {
        put_bytes(&mut out, 1, &p.encode(), true);
    }
    out
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceKind {
    Url(String),
    Key(String),
    Data(Vec<u8>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Source {
    pub kind: SourceKind,
    pub size: Option<u64>,
    pub etag: Option<String>,
    pub modified_not_after: Option<i64>,
}

impl Source {
    pub fn has_pins(&self) -> bool {
        self.size.is_some() || self.etag.is_some() || self.modified_not_after.is_some()
    }
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        match &self.kind {
            SourceKind::Url(u) => put_bytes(&mut out, 1, u.as_bytes(), true),
            SourceKind::Key(k) => put_bytes(&mut out, 2, k.as_bytes(), true),
            SourceKind::Data(d) => put_bytes(&mut out, 3, d, true),
        }
        if let Some(s) = self.size {
            put_uint(&mut out, 4, s, true);
        }
        if let Some(e) = &self.etag {
            put_bytes(&mut out, 5, e.as_bytes(), true);
        }
        if let Some(m) = self.modified_not_after {
            put_uint(&mut out, 6, m as u64, true);
        }
        out
    }
}

/// `DQUOTE *etagc DQUOTE`, etagc = %x21 / %x23-7E (§6.1).
pub fn is_strong_etag(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() >= 2
        && b[0] == b'"'
        && b[b.len() - 1] == b'"'
        && b[1..b.len() - 1]
            .iter()
            .all(|&c| (0x21..=0x7e).contains(&c) && c != b'"')
}

/// Decode and validate a SourceTable (§6). Errors are archive errors.
pub fn decode_source_table(buf: &[u8]) -> PResult<Vec<Source>> {
    let mut out = Vec::new();
    for (f, w) in parse_fields(buf)? {
        if f != 1 {
            continue;
        }
        let sb = want_len(f, &w)?;
        let mut kind = None;
        let mut size = None;
        let mut etag = None;
        let mut mna = None;
        for (f, w) in parse_fields(sb)? {
            match f {
                1 => kind = Some(SourceKind::Url(want_str(f, &w)?)),
                2 => kind = Some(SourceKind::Key(want_str(f, &w)?)),
                3 => kind = Some(SourceKind::Data(want_len(f, &w)?.to_vec())),
                4 => size = Some(want_varint(f, &w)?),
                5 => etag = Some(want_str(f, &w)?),
                6 => mna = Some(want_varint(f, &w)? as i64),
                _ => {}
            }
        }
        let kind = kind.ok_or("source has no kind")?;
        let s = Source { kind, size, etag, modified_not_after: mna };
        if let SourceKind::Url(u) = &s.kind {
            if u.is_empty() {
                return Err("empty url source".into());
            }
        } else if s.has_pins() {
            return Err("pin on a key or data source".into());
        }
        if let Some(e) = &s.etag {
            if !is_strong_etag(e) {
                return Err(format!("etag pin {e:?} is not a strong entity tag"));
            }
        }
        out.push(s);
    }
    Ok(out)
}

pub fn encode_source_table(sources: &[Source]) -> Vec<u8> {
    let mut out = Vec::new();
    for s in sources {
        put_bytes(&mut out, 1, &s.encode(), true);
    }
    out
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Page {
    pub first_key: String,
    pub offset: u64,
    pub length: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Pinned {
    pub key: String,
    pub data_offset: u64,
    pub size: u64,
    pub csize: u64,
    pub method: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CdIndex {
    pub pages: Vec<Page>,
    pub pinned: Vec<Pinned>,
}

pub fn decode_index(buf: &[u8]) -> PResult<CdIndex> {
    let mut idx = CdIndex::default();
    for (f, w) in parse_fields(buf)? {
        match f {
            1 => {
                let mut p = Page::default();
                for (f, w) in parse_fields(want_len(f, &w)?)? {
                    match f {
                        1 => p.first_key = want_str(f, &w)?,
                        2 => p.offset = want_varint(f, &w)?,
                        3 => p.length = want_varint(f, &w)?,
                        _ => {}
                    }
                }
                idx.pages.push(p);
            }
            2 => {
                let mut p = Pinned::default();
                for (f, w) in parse_fields(want_len(f, &w)?)? {
                    match f {
                        1 => p.key = want_str(f, &w)?,
                        2 => p.data_offset = want_varint(f, &w)?,
                        3 => p.size = want_varint(f, &w)?,
                        4 => p.csize = want_varint(f, &w)?,
                        5 => p.method = want_u32(f, &w)?,
                        _ => {}
                    }
                }
                idx.pinned.push(p);
            }
            _ => {}
        }
    }
    Ok(idx)
}

pub fn encode_index(idx: &CdIndex) -> Vec<u8> {
    let mut out = Vec::new();
    for p in &idx.pages {
        let mut m = Vec::new();
        put_bytes(&mut m, 1, p.first_key.as_bytes(), false);
        put_uint(&mut m, 2, p.offset, false);
        put_uint(&mut m, 3, p.length, false);
        put_bytes(&mut out, 1, &m, true);
    }
    for p in &idx.pinned {
        let mut m = Vec::new();
        put_bytes(&mut m, 1, p.key.as_bytes(), false);
        put_uint(&mut m, 2, p.data_offset, false);
        put_uint(&mut m, 3, p.size, false);
        put_uint(&mut m, 4, p.csize, false);
        put_uint(&mut m, 5, p.method as u64, false);
        put_bytes(&mut out, 2, &m, true);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn range_roundtrip() {
        let cases = vec![
            Range::default(),
            Range { source: 3, offset: 1 << 40, length: 7, data: None },
            Range { data: Some(vec![]), ..Default::default() },
            Range { data: Some(vec![1, 2, 3]), ..Default::default() },
        ];
        for r in cases {
            let enc = r.encode();
            assert_eq!(Range::decode(&enc, 4).unwrap(), r);
        }
        // canonical encodings
        assert_eq!(Range::default().encode(), Vec::<u8>::new());
        assert_eq!(
            Range { data: Some(vec![]), ..Default::default() }.encode(),
            vec![0x2a, 0x00]
        );
    }

    #[test]
    fn decode_tolerances() {
        // unknown field (VARINT, I64, LEN, I32), reserved field 2, non-minimal varint, last wins
        let buf = [
            0x10, 0x05, // field 2 varint (reserved)
            0x31, 0, 0, 0, 0, 0, 0, 0, 0, // field 6 I64
            0x3a, 0x01, 0xff, // field 7 LEN
            0x45, 0, 0, 0, 0, // field 8 I32
            0x18, 0x81, 0x80, 0x00, // offset = 1 (non-minimal)
            0x18, 0x02, // offset = 2 (last wins)
        ];
        let r = Range::decode(&buf, 1).unwrap();
        assert_eq!(r.offset, 2);
    }

    #[test]
    fn malformed_group() {
        assert!(Range::decode(&[0x33, 0x34], 1).is_err());
    }
    #[test]
    fn malformed_field_zero() {
        assert!(Range::decode(&[0x00, 0x00], 1).is_err());
    }
    #[test]
    fn malformed_long_varint() {
        let mut b = vec![0x18];
        b.extend(std::iter::repeat(0x80).take(10));
        b.push(0);
        assert!(Range::decode(&b, 1).is_err());
    }
    #[test]
    fn malformed_varint_overflow() {
        let mut b = vec![0x18];
        b.extend(std::iter::repeat(0xff).take(9));
        b.push(0x02);
        assert!(Range::decode(&b, 1).is_err());
    }
    #[test]
    fn malformed_uint32_overflow() {
        let mut b = vec![0x08];
        put_varint(&mut b, 1 << 32);
        assert!(Range::decode(&b, 1).is_err());
    }
    #[test]
    fn malformed_wrong_wire_type() {
        assert!(Range::decode(&[0x1a, 0x00], 1).is_err());
    }
    #[test]
    fn malformed_truncated_len() {
        assert!(Range::decode(&[0x2a, 0x05, 0x00], 1).is_err());
    }
    #[test]
    fn malformed_literal_with_source() {
        assert!(Range::decode(&[0x18, 0x01, 0x2a, 0x00], 1).is_err());
    }
    #[test]
    fn malformed_source_oob() {
        assert!(Range::decode(&[0x08, 0x01], 1).is_err());
    }
    #[test]
    fn malformed_offset_overflow() {
        let r = Range { source: 0, offset: u64::MAX, length: 1, data: None };
        assert!(Range::decode(&r.encode(), 1).is_err());
    }
    #[test]
    fn malformed_bad_utf8_string() {
        let buf = [0x0a, 0x03, 0x0a, 0x01, 0xff];
        assert!(decode_source_table(&buf).is_err());
    }
    #[test]
    fn etag_check() {
        assert!(is_strong_etag("\"abc\""));
        assert!(is_strong_etag("\"\""));
        assert!(!is_strong_etag("W/\"abc\""));
        assert!(!is_strong_etag("abc"));
        assert!(!is_strong_etag("\"a b\""));
    }
}
