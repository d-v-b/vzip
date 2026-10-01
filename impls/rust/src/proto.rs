//! Hand-written protobuf (proto3) encoding and decoding for the vzip schema
//! (spec §5, Appendix A).

pub type DecodeResult<T> = Result<T, String>;

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Range {
    pub source: u32,
    pub offset: u64,
    pub length: u64,
    pub data: Option<Vec<u8>>,
}

impl Range {
    /// Size of the described byte sequence (§5.2).
    pub fn size(&self) -> u64 {
        match &self.data {
            Some(d) => d.len() as u64,
            None => self.length,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Concat {
    pub parts: Vec<Range>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceKind {
    Url(String),
    Key(String),
    Data(Vec<u8>),
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Source {
    pub kind: Option<SourceKind>,
    pub size: Option<u64>,
    pub etag: Option<String>,
    pub modified_not_after: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SourceTable {
    pub sources: Vec<Source>,
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

// ---------------------------------------------------------------------------
// Low-level reader
// ---------------------------------------------------------------------------

const WT_VARINT: u8 = 0;
const WT_I64: u8 = 1;
const WT_LEN: u8 = 2;
const WT_I32: u8 = 5;

enum Value<'a> {
    Varint(u64),
    Len(&'a [u8]),
    Fixed,
}

struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }

    fn varint(&mut self) -> DecodeResult<u64> {
        let mut v: u64 = 0;
        for i in 0..10 {
            let b = *self
                .buf
                .get(self.pos)
                .ok_or_else(|| "truncated varint".to_string())?;
            self.pos += 1;
            if i == 9 {
                // 10th byte: only the lowest bit fits in 64 bits, and it must
                // terminate the varint.
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

    /// Reads the next field. Returns (field number, wire type, value).
    fn field(&mut self) -> DecodeResult<(u32, u8, Value<'a>)> {
        let tag = self.varint()?;
        let wt = (tag & 7) as u8;
        let num = tag >> 3;
        if num == 0 {
            return Err("field number 0".into());
        }
        if num > (1 << 29) - 1 {
            return Err("field number above 2^29-1".into());
        }
        let val = match wt {
            WT_VARINT => Value::Varint(self.varint()?),
            WT_I64 => {
                if self.buf.len() - self.pos < 8 {
                    return Err("truncated I64 field".into());
                }
                self.pos += 8;
                Value::Fixed
            }
            WT_LEN => {
                let len = self.varint()?;
                let rem = (self.buf.len() - self.pos) as u64;
                if len > rem {
                    return Err("LEN field extends past end of message".into());
                }
                let s = &self.buf[self.pos..self.pos + len as usize];
                self.pos += len as usize;
                Value::Len(s)
            }
            WT_I32 => {
                if self.buf.len() - self.pos < 4 {
                    return Err("truncated I32 field".into());
                }
                self.pos += 4;
                Value::Fixed
            }
            _ => return Err(format!("unsupported wire type {}", wt)),
        };
        Ok((num as u32, wt, val))
    }

    fn done(&self) -> bool {
        self.pos >= self.buf.len()
    }
}

fn want_varint(v: Value, name: &str) -> DecodeResult<u64> {
    match v {
        Value::Varint(x) => Ok(x),
        _ => Err(format!("field {} has wrong wire type", name)),
    }
}

fn want_u32(v: Value, name: &str) -> DecodeResult<u32> {
    let x = want_varint(v, name)?;
    if x > u32::MAX as u64 {
        return Err(format!("uint32 field {} exceeds 2^32-1", name));
    }
    Ok(x as u32)
}

fn want_len<'a>(v: Value<'a>, name: &str) -> DecodeResult<&'a [u8]> {
    match v {
        Value::Len(s) => Ok(s),
        _ => Err(format!("field {} has wrong wire type", name)),
    }
}

fn want_string(v: Value, name: &str) -> DecodeResult<String> {
    let b = want_len(v, name)?;
    String::from_utf8(b.to_vec()).map_err(|_| format!("string field {} is not valid UTF-8", name))
}

// ---------------------------------------------------------------------------
// Decoders
// ---------------------------------------------------------------------------

pub fn decode_range(buf: &[u8]) -> DecodeResult<Range> {
    let mut r = Reader::new(buf);
    let mut out = Range::default();
    while !r.done() {
        let (num, _wt, v) = r.field()?;
        match num {
            1 => out.source = want_u32(v, "Range.source")?,
            3 => out.offset = want_varint(v, "Range.offset")?,
            4 => out.length = want_varint(v, "Range.length")?,
            5 => out.data = Some(want_len(v, "Range.data")?.to_vec()),
            _ => {} // unknown (including reserved 2): skipped
        }
    }
    Ok(out)
}

pub fn decode_concat(buf: &[u8]) -> DecodeResult<Concat> {
    let mut r = Reader::new(buf);
    let mut out = Concat::default();
    while !r.done() {
        let (num, _wt, v) = r.field()?;
        if num == 1 {
            out.parts.push(decode_range(want_len(v, "Concat.parts")?)?);
        }
    }
    Ok(out)
}

pub fn decode_source(buf: &[u8]) -> DecodeResult<Source> {
    let mut r = Reader::new(buf);
    let mut out = Source::default();
    while !r.done() {
        let (num, _wt, v) = r.field()?;
        match num {
            1 => out.kind = Some(SourceKind::Url(want_string(v, "Source.url")?)),
            2 => out.kind = Some(SourceKind::Key(want_string(v, "Source.key")?)),
            3 => out.kind = Some(SourceKind::Data(want_len(v, "Source.data")?.to_vec())),
            4 => out.size = Some(want_varint(v, "Source.size")?),
            5 => out.etag = Some(want_string(v, "Source.etag")?),
            6 => out.modified_not_after = Some(want_varint(v, "Source.modified_not_after")? as i64),
            _ => {}
        }
    }
    Ok(out)
}

pub fn decode_source_table(buf: &[u8]) -> DecodeResult<SourceTable> {
    let mut r = Reader::new(buf);
    let mut out = SourceTable::default();
    while !r.done() {
        let (num, _wt, v) = r.field()?;
        if num == 1 {
            out.sources.push(decode_source(want_len(v, "SourceTable.sources")?)?);
        }
    }
    Ok(out)
}

fn decode_page(buf: &[u8]) -> DecodeResult<Page> {
    let mut r = Reader::new(buf);
    let mut out = Page::default();
    while !r.done() {
        let (num, _wt, v) = r.field()?;
        match num {
            1 => out.first_key = want_string(v, "Page.first_key")?,
            2 => out.offset = want_varint(v, "Page.offset")?,
            3 => out.length = want_varint(v, "Page.length")?,
            _ => {}
        }
    }
    Ok(out)
}

fn decode_pinned(buf: &[u8]) -> DecodeResult<Pinned> {
    let mut r = Reader::new(buf);
    let mut out = Pinned::default();
    while !r.done() {
        let (num, _wt, v) = r.field()?;
        match num {
            1 => out.key = want_string(v, "Pinned.key")?,
            2 => out.data_offset = want_varint(v, "Pinned.data_offset")?,
            3 => out.size = want_varint(v, "Pinned.size")?,
            4 => out.csize = want_varint(v, "Pinned.csize")?,
            5 => out.method = want_u32(v, "Pinned.method")?,
            _ => {}
        }
    }
    Ok(out)
}

pub fn decode_cd_index(buf: &[u8]) -> DecodeResult<CdIndex> {
    let mut r = Reader::new(buf);
    let mut out = CdIndex::default();
    while !r.done() {
        let (num, _wt, v) = r.field()?;
        match num {
            1 => out.pages.push(decode_page(want_len(v, "CdIndex.pages")?)?),
            2 => out.pinned.push(decode_pinned(want_len(v, "CdIndex.pinned")?)?),
            _ => {}
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Encoders (canonical: field order, minimal varints, defaults omitted)
// ---------------------------------------------------------------------------

fn put_varint(out: &mut Vec<u8>, mut v: u64) {
    loop {
        let b = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            out.push(b);
            return;
        }
        out.push(b | 0x80);
    }
}

fn put_tag(out: &mut Vec<u8>, num: u32, wt: u8) {
    put_varint(out, ((num as u64) << 3) | wt as u64);
}

fn put_uint(out: &mut Vec<u8>, num: u32, v: u64, always: bool) {
    if v != 0 || always {
        put_tag(out, num, WT_VARINT);
        put_varint(out, v);
    }
}

fn put_bytes(out: &mut Vec<u8>, num: u32, b: &[u8], always: bool) {
    if !b.is_empty() || always {
        put_tag(out, num, WT_LEN);
        put_varint(out, b.len() as u64);
        out.extend_from_slice(b);
    }
}

pub fn encode_range(r: &Range) -> Vec<u8> {
    let mut out = Vec::new();
    put_uint(&mut out, 1, r.source as u64, false);
    put_uint(&mut out, 3, r.offset, false);
    put_uint(&mut out, 4, r.length, false);
    if let Some(d) = &r.data {
        put_bytes(&mut out, 5, d, true);
    }
    out
}

pub fn encode_concat(c: &Concat) -> Vec<u8> {
    let mut out = Vec::new();
    for p in &c.parts {
        // A repeated message element is always emitted, even if empty.
        put_bytes(&mut out, 1, &encode_range(p), true);
    }
    out
}

pub fn encode_source(s: &Source) -> Vec<u8> {
    let mut out = Vec::new();
    match &s.kind {
        Some(SourceKind::Url(u)) => put_bytes(&mut out, 1, u.as_bytes(), true),
        Some(SourceKind::Key(k)) => put_bytes(&mut out, 2, k.as_bytes(), true),
        Some(SourceKind::Data(d)) => put_bytes(&mut out, 3, d, true),
        None => {}
    }
    if let Some(v) = s.size {
        put_uint(&mut out, 4, v, true);
    }
    if let Some(e) = &s.etag {
        put_bytes(&mut out, 5, e.as_bytes(), true);
    }
    if let Some(m) = s.modified_not_after {
        put_uint(&mut out, 6, m as u64, true);
    }
    out
}

pub fn encode_source_table(t: &SourceTable) -> Vec<u8> {
    let mut out = Vec::new();
    for s in &t.sources {
        put_bytes(&mut out, 1, &encode_source(s), true);
    }
    out
}

pub fn encode_cd_index(ix: &CdIndex) -> Vec<u8> {
    let mut out = Vec::new();
    for p in &ix.pages {
        let mut m = Vec::new();
        put_bytes(&mut m, 1, p.first_key.as_bytes(), false);
        put_uint(&mut m, 2, p.offset, false);
        put_uint(&mut m, 3, p.length, false);
        put_bytes(&mut out, 1, &m, true);
    }
    for p in &ix.pinned {
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
    fn roundtrip_and_canonical() {
        let r = Range { source: 2, offset: 300, length: 5, data: None };
        let e = encode_range(&r);
        assert_eq!(e, vec![0x08, 2, 0x18, 0xac, 0x02, 0x20, 5]);
        assert_eq!(decode_range(&e).unwrap(), r);
        let lit = Range { data: Some(vec![]), ..Default::default() };
        assert_eq!(encode_range(&lit), vec![0x2a, 0]);
        assert_eq!(decode_range(&[0x2a, 0]).unwrap(), lit);
        let s = Source {
            kind: Some(SourceKind::Data(vec![])),
            ..Default::default()
        };
        assert_eq!(encode_source(&s), vec![0x1a, 0]);
        let s2 = Source {
            kind: Some(SourceKind::Url("a".into())),
            size: Some(0),
            etag: None,
            modified_not_after: Some(-1),
        };
        let e2 = encode_source(&s2);
        assert_eq!(decode_source(&e2).unwrap(), s2);
    }

    #[test]
    fn decoding_rules() {
        // Non-minimal varint accepted.
        assert_eq!(decode_range(&[0x08, 0x82, 0x00]).unwrap().source, 2);
        // Unknown and reserved fields skipped, all accepted wire types.
        assert!(decode_range(&[0x10, 1, 0x11, 0, 0, 0, 0, 0, 0, 0, 0, 0x15, 0, 0, 0, 0, 0x32, 1, 9]).is_ok());
        // Last occurrence wins.
        assert_eq!(decode_range(&[0x08, 1, 0x08, 3]).unwrap().source, 3);
        // Oneof last wins.
        let s = decode_source(&[0x0a, 1, b'u', 0x12, 1, b'k']).unwrap();
        assert_eq!(s.kind, Some(SourceKind::Key("k".into())));
    }

    #[test]
    fn decoding_rejects_group() {
        assert!(decode_range(&[0x5b]).is_err()); // field 11 wt 3
        assert!(decode_range(&[0x5c]).is_err()); // wt 4
        assert!(decode_range(&[0x5e]).is_err()); // wt 6
        assert!(decode_range(&[0x5f]).is_err()); // wt 7
    }

    #[test]
    fn decoding_rejects_field_zero() {
        assert!(decode_range(&[0x00, 0]).is_err());
    }

    #[test]
    fn decoding_rejects_huge_field_number() {
        // field number 2^29 (tag = 2^32)
        let mut b = Vec::new();
        put_varint(&mut b, 1u64 << 32);
        b.push(0);
        assert!(decode_range(&b).is_err());
    }

    #[test]
    fn decoding_rejects_truncation() {
        assert!(decode_range(&[0x08]).is_err());
        assert!(decode_range(&[0x08, 0x80]).is_err());
        assert!(decode_range(&[0x2a, 5, 1]).is_err());
        assert!(decode_range(&[0x11, 0]).is_err());
    }

    #[test]
    fn decoding_rejects_long_varint() {
        let mut b = vec![0x18];
        b.extend_from_slice(&[0x80; 10]);
        b.push(0);
        assert!(decode_range(&b).is_err());
        let mut b = vec![0x18];
        b.extend_from_slice(&[0xff; 9]);
        b.push(0x02);
        assert!(decode_range(&b).is_err());
        let mut b = vec![0x18];
        b.extend_from_slice(&[0xff; 9]);
        b.push(0x01);
        assert_eq!(decode_range(&b).unwrap().offset, u64::MAX);
    }

    #[test]
    fn decoding_rejects_wrong_wire_type() {
        assert!(decode_range(&[0x0a, 0]).is_err());
        assert!(decode_range(&[0x28, 0]).is_err());
    }

    #[test]
    fn decoding_rejects_uint32_overflow() {
        let mut b = vec![0x08];
        put_varint(&mut b, 1u64 << 32);
        assert!(decode_range(&b).is_err());
    }

    #[test]
    fn decoding_rejects_bad_utf8() {
        assert!(decode_source(&[0x0a, 1, 0xff]).is_err());
    }
}
