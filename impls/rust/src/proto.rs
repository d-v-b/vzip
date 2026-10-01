//! Hand-written protobuf wire format (spec §5, Appendix A).
//!
//! Decoders return `Err(String)` for a malformed message; callers attach the
//! error class that fits the context (payload error, archive error).

pub type DResult<T> = std::result::Result<T, String>;

// ---------------------------------------------------------------------------
// Messages

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
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceKind {
    Url(String),
    Key(String),
    Data(Vec<u8>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Source {
    /// `None` when no oneof member was on the wire (an archive error, §6).
    pub kind: Option<SourceKind>,
    pub size: Option<u64>,
    pub etag: Option<String>,
    pub modified_not_after: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Page {
    pub first_key: String,
    pub offset: u64,
    pub length: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
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
// Decoding

#[derive(Debug, Clone, Copy)]
enum Value<'a> {
    Varint(u64),
    I64,
    Len(&'a [u8]),
    I32,
}

fn read_varint(buf: &[u8], pos: &mut usize) -> DResult<u64> {
    let mut result: u64 = 0;
    for i in 0..10 {
        let b = *buf.get(*pos).ok_or_else(|| "truncated varint".to_string())?;
        *pos += 1;
        let low = (b & 0x7f) as u64;
        if i == 9 {
            if b & 0x80 != 0 {
                return Err("varint longer than 10 bytes".into());
            }
            if low > 1 {
                return Err("varint exceeds 2^64-1".into());
            }
        }
        result |= low << (7 * i);
        if b & 0x80 == 0 {
            return Ok(result);
        }
    }
    unreachable!()
}

/// Iterate over the fields of a message, validating the framing.
fn for_each_field<'a>(
    buf: &'a [u8],
    mut f: impl FnMut(u32, Value<'a>) -> DResult<()>,
) -> DResult<()> {
    let mut pos = 0usize;
    while pos < buf.len() {
        let tag = read_varint(buf, &mut pos)?;
        let field = tag >> 3;
        let wt = (tag & 7) as u8;
        if field == 0 || field > (1 << 29) - 1 {
            return Err(format!("invalid field number {field}"));
        }
        let value = match wt {
            0 => Value::Varint(read_varint(buf, &mut pos)?),
            1 => {
                if buf.len() - pos < 8 {
                    return Err("truncated I64 field".into());
                }
                pos += 8;
                Value::I64
            }
            2 => {
                let len = read_varint(buf, &mut pos)?;
                if len > (buf.len() - pos) as u64 {
                    return Err("LEN field extends past end of message".into());
                }
                let s = &buf[pos..pos + len as usize];
                pos += len as usize;
                Value::Len(s)
            }
            5 => {
                if buf.len() - pos < 4 {
                    return Err("truncated I32 field".into());
                }
                pos += 4;
                Value::I32
            }
            other => return Err(format!("invalid wire type {other}")),
        };
        f(field as u32, value)?;
    }
    Ok(())
}

fn want_varint(field: u32, v: Value) -> DResult<u64> {
    match v {
        Value::Varint(x) => Ok(x),
        _ => Err(format!("field {field}: wrong wire type, expected VARINT")),
    }
}

fn want_u32(field: u32, v: Value) -> DResult<u32> {
    let x = want_varint(field, v)?;
    if x > u32::MAX as u64 {
        return Err(format!("field {field}: uint32 value {x} exceeds 2^32-1"));
    }
    Ok(x as u32)
}

fn want_len<'a>(field: u32, v: Value<'a>) -> DResult<&'a [u8]> {
    match v {
        Value::Len(s) => Ok(s),
        _ => Err(format!("field {field}: wrong wire type, expected LEN")),
    }
}

fn want_string(field: u32, v: Value) -> DResult<String> {
    let s = want_len(field, v)?;
    String::from_utf8(s.to_vec()).map_err(|_| format!("field {field}: string is not valid UTF-8"))
}

/// Decode a `Range` message, without the semantic checks of §5.2.
pub fn decode_range_raw(buf: &[u8]) -> DResult<Range> {
    let mut r = Range::default();
    for_each_field(buf, |field, v| {
        match field {
            1 => r.source = want_u32(field, v)?,
            3 => r.offset = want_varint(field, v)?,
            4 => r.length = want_varint(field, v)?,
            5 => r.data = Some(want_len(field, v)?.to_vec()),
            _ => {} // unknown or reserved (2): skipped
        }
        Ok(())
    })?;
    Ok(r)
}

/// Check a decoded Range against §5.2.
pub fn check_range(r: &Range, num_sources: usize) -> DResult<()> {
    if r.data.is_some() {
        if r.source != 0 || r.offset != 0 || r.length != 0 {
            return Err("literal range has non-zero source, offset or length".into());
        }
    } else {
        if (r.source as u64) >= num_sources as u64 {
            return Err(format!(
                "source index {} out of bounds ({} sources)",
                r.source, num_sources
            ));
        }
        if r.offset.checked_add(r.length).is_none() {
            return Err("offset + length exceeds 2^64-1".into());
        }
    }
    Ok(())
}

/// Decode a reference payload. `concat` selects 0x7A77 (Concat) vs 0x7A76 (Range).
/// Applies all checks of §5.2 and §5.3.
pub fn decode_payload(buf: &[u8], concat: bool, num_sources: usize) -> DResult<Vec<Range>> {
    let parts = if concat {
        let mut parts = Vec::new();
        for_each_field(buf, |field, v| {
            if field == 1 {
                let s = want_len(field, v)?;
                parts.push(decode_range_raw(s)?);
            }
            Ok(())
        })?;
        parts
    } else {
        vec![decode_range_raw(buf)?]
    };
    let mut total: u64 = 0;
    for p in &parts {
        check_range(p, num_sources)?;
        total = total
            .checked_add(p.size())
            .ok_or_else(|| "total size exceeds 2^64-1".to_string())?;
    }
    Ok(parts)
}

fn decode_source(buf: &[u8]) -> DResult<Source> {
    let mut s = Source { kind: None, size: None, etag: None, modified_not_after: None };
    for_each_field(buf, |field, v| {
        match field {
            1 => s.kind = Some(SourceKind::Url(want_string(field, v)?)),
            2 => s.kind = Some(SourceKind::Key(want_string(field, v)?)),
            3 => s.kind = Some(SourceKind::Data(want_len(field, v)?.to_vec())),
            4 => s.size = Some(want_varint(field, v)?),
            5 => s.etag = Some(want_string(field, v)?),
            6 => s.modified_not_after = Some(want_varint(field, v)? as i64),
            _ => {}
        }
        Ok(())
    })?;
    Ok(s)
}

pub fn decode_source_table(buf: &[u8]) -> DResult<Vec<Source>> {
    let mut out = Vec::new();
    for_each_field(buf, |field, v| {
        if field == 1 {
            out.push(decode_source(want_len(field, v)?)?);
        }
        Ok(())
    })?;
    Ok(out)
}

fn decode_page(buf: &[u8]) -> DResult<Page> {
    let mut p = Page { first_key: String::new(), offset: 0, length: 0 };
    for_each_field(buf, |field, v| {
        match field {
            1 => p.first_key = want_string(field, v)?,
            2 => p.offset = want_varint(field, v)?,
            3 => p.length = want_varint(field, v)?,
            _ => {}
        }
        Ok(())
    })?;
    Ok(p)
}

fn decode_pinned(buf: &[u8]) -> DResult<Pinned> {
    let mut p = Pinned { key: String::new(), data_offset: 0, size: 0, csize: 0, method: 0 };
    for_each_field(buf, |field, v| {
        match field {
            1 => p.key = want_string(field, v)?,
            2 => p.data_offset = want_varint(field, v)?,
            3 => p.size = want_varint(field, v)?,
            4 => p.csize = want_varint(field, v)?,
            5 => p.method = want_u32(field, v)?,
            _ => {}
        }
        Ok(())
    })?;
    Ok(p)
}

pub fn decode_cd_index(buf: &[u8]) -> DResult<CdIndex> {
    let mut idx = CdIndex::default();
    for_each_field(buf, |field, v| {
        match field {
            1 => idx.pages.push(decode_page(want_len(field, v)?)?),
            2 => idx.pinned.push(decode_pinned(want_len(field, v)?)?),
            _ => {}
        }
        Ok(())
    })?;
    Ok(idx)
}

// ---------------------------------------------------------------------------
// Encoding (canonical: field-number order, minimal varints, defaults omitted)

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

pub fn encode_concat(parts: &[Range]) -> Vec<u8> {
    let mut out = Vec::new();
    for p in parts {
        // Repeated message elements are always emitted, even when empty.
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

pub fn encode_source_table(sources: &[Source]) -> Vec<u8> {
    let mut out = Vec::new();
    for s in sources {
        put_bytes(&mut out, 1, &encode_source(s), true);
    }
    out
}

pub fn encode_cd_index(idx: &CdIndex) -> Vec<u8> {
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
    fn range_roundtrip_and_canonical() {
        let r = Range { source: 3, offset: 0, length: 300, data: None };
        let enc = encode_range(&r);
        assert_eq!(enc, vec![0x08, 3, 0x20, 0xac, 0x02]);
        assert_eq!(decode_range_raw(&enc).unwrap(), r);
        let lit = Range { data: Some(vec![]), ..Default::default() };
        assert_eq!(encode_range(&lit), vec![0x2a, 0]);
        assert_eq!(decode_payload(&encode_range(&lit), false, 0).unwrap(), vec![lit]);
        // negative int64 encodes as 10 bytes
        let s = Source { kind: Some(SourceKind::Url("a".into())), size: Some(0), etag: None, modified_not_after: Some(-1) };
        let enc = encode_source(&s);
        assert_eq!(enc.len(), 3 + 2 + 11);
        assert_eq!(decode_source_table(&encode_source_table(&[s.clone()])).unwrap(), vec![s]);
    }

    #[test]
    fn malformed_inputs() {
        // wire type 3 (group) on unknown field
        assert!(decode_range_raw(&[0x33]).is_err());
        // field number 0
        assert!(decode_range_raw(&[0x00, 0x00]).is_err());
        // truncated
        assert!(decode_range_raw(&[0x08]).is_err());
        // len past end
        assert!(decode_range_raw(&[0x2a, 0x05, 1]).is_err());
        // 11-byte varint
        assert!(decode_range_raw(&[0x18, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x00]).is_err());
        // varint overflow (10th byte = 2)
        assert!(decode_range_raw(&[0x18, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x02]).is_err());
        // max u64 ok
        assert!(decode_range_raw(&[0x18, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01]).is_ok());
        // wrong wire type for known field
        assert!(decode_range_raw(&[0x0d, 0, 0, 0, 0]).is_err());
        // uint32 overflow
        assert!(decode_range_raw(&[0x08, 0x80, 0x80, 0x80, 0x80, 0x10]).is_err());
        // reserved field 2 skipped, whatever wire type
        assert!(decode_range_raw(&[0x12, 0x01, 0xff]).is_ok());
        // non-minimal varint accepted
        assert_eq!(decode_range_raw(&[0x08, 0x81, 0x00]).unwrap().source, 1);
        // invalid utf8 string in source
        assert!(decode_source_table(&[0x0a, 0x03, 0x0a, 0x01, 0xff]).is_err());
        // field number too large: tag = (2^29) << 3
        let mut b = Vec::new();
        put_varint(&mut b, (1u64 << 29) << 3);
        b.push(0);
        assert!(decode_range_raw(&b).is_err());
    }
}
