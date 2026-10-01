//! Hand-written protobuf encoding/decoding of the vzip messages (§5, Appendix A).

pub type PResult<T> = Result<T, String>;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Range {
    pub source: u32,
    pub offset: u64,
    pub length: u64,
    /// Present => literal range.
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
    pub kind: Option<SourceKind>,
    pub size: Option<u64>,
    pub etag: Option<String>,
    pub modified_not_after: Option<i64>,
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

// ---------------------------------------------------------------- decoding

enum Field<'a> {
    Varint(u64),
    I64,
    Len(&'a [u8]),
    I32,
}

struct Reader<'a> {
    b: &'a [u8],
    i: usize,
}

fn read_varint(b: &[u8], i: &mut usize) -> PResult<u64> {
    let mut v: u64 = 0;
    for n in 0..10 {
        let c = *b.get(*i).ok_or("truncated varint")?;
        *i += 1;
        if n == 9 && c > 1 {
            // 10th byte may carry only bit 63 (and no continuation)
            return Err("varint exceeds 2^64-1 or is longer than 10 bytes".into());
        }
        v |= ((c & 0x7f) as u64) << (7 * n);
        if c & 0x80 == 0 {
            return Ok(v);
        }
    }
    Err("varint longer than 10 bytes".into())
}

impl<'a> Reader<'a> {
    fn new(b: &'a [u8]) -> Self {
        Reader { b, i: 0 }
    }
    fn next(&mut self) -> PResult<Option<(u32, Field<'a>)>> {
        if self.i >= self.b.len() {
            return Ok(None);
        }
        let tag = read_varint(self.b, &mut self.i)?;
        let wt = (tag & 7) as u8;
        let num = tag >> 3;
        if num == 0 || num > (1 << 29) - 1 {
            return Err(format!("invalid field number {num}"));
        }
        let f = match wt {
            0 => Field::Varint(read_varint(self.b, &mut self.i)?),
            1 => {
                if self.b.len() - self.i < 8 {
                    return Err("truncated I64 field".into());
                }
                self.i += 8;
                Field::I64
            }
            2 => {
                let n = read_varint(self.b, &mut self.i)?;
                if n > (self.b.len() - self.i) as u64 {
                    return Err("LEN field extends past end of message".into());
                }
                let s = &self.b[self.i..self.i + n as usize];
                self.i += n as usize;
                Field::Len(s)
            }
            5 => {
                if self.b.len() - self.i < 4 {
                    return Err("truncated I32 field".into());
                }
                self.i += 4;
                Field::I32
            }
            w => return Err(format!("invalid wire type {w}")),
        };
        Ok(Some((num as u32, f)))
    }
}

fn want_varint(f: Field, name: &str) -> PResult<u64> {
    match f {
        Field::Varint(v) => Ok(v),
        _ => Err(format!("field {name} has wrong wire type")),
    }
}
fn want_u32(f: Field, name: &str) -> PResult<u32> {
    let v = want_varint(f, name)?;
    u32::try_from(v).map_err(|_| format!("uint32 field {name} exceeds 2^32-1"))
}
fn want_len<'a>(f: Field<'a>, name: &str) -> PResult<&'a [u8]> {
    match f {
        Field::Len(s) => Ok(s),
        _ => Err(format!("field {name} has wrong wire type")),
    }
}
fn want_str(f: Field, name: &str) -> PResult<String> {
    let b = want_len(f, name)?;
    String::from_utf8(b.to_vec()).map_err(|_| format!("string field {name} is not valid UTF-8"))
}

pub fn decode_range(b: &[u8]) -> PResult<Range> {
    let mut r = Reader::new(b);
    let mut out = Range::default();
    while let Some((n, f)) = r.next()? {
        match n {
            1 => out.source = want_u32(f, "Range.source")?,
            3 => out.offset = want_varint(f, "Range.offset")?,
            4 => out.length = want_varint(f, "Range.length")?,
            5 => out.data = Some(want_len(f, "Range.data")?.to_vec()),
            _ => {} // unknown (incl. reserved 2): skipped
        }
    }
    Ok(out)
}

pub fn decode_concat(b: &[u8]) -> PResult<Vec<Range>> {
    let mut r = Reader::new(b);
    let mut parts = Vec::new();
    while let Some((n, f)) = r.next()? {
        if n == 1 {
            parts.push(decode_range(want_len(f, "Concat.parts")?)?);
        }
    }
    Ok(parts)
}

pub fn decode_source(b: &[u8]) -> PResult<Source> {
    let mut r = Reader::new(b);
    let mut s = Source { kind: None, size: None, etag: None, modified_not_after: None };
    while let Some((n, f)) = r.next()? {
        match n {
            1 => s.kind = Some(SourceKind::Url(want_str(f, "Source.url")?)),
            2 => s.kind = Some(SourceKind::Key(want_str(f, "Source.key")?)),
            3 => s.kind = Some(SourceKind::Data(want_len(f, "Source.data")?.to_vec())),
            4 => s.size = Some(want_varint(f, "Source.size")?),
            5 => s.etag = Some(want_str(f, "Source.etag")?),
            6 => s.modified_not_after = Some(want_varint(f, "Source.modified_not_after")? as i64),
            _ => {}
        }
    }
    Ok(s)
}

pub fn decode_source_table(b: &[u8]) -> PResult<Vec<Source>> {
    let mut r = Reader::new(b);
    let mut v = Vec::new();
    while let Some((n, f)) = r.next()? {
        if n == 1 {
            v.push(decode_source(want_len(f, "SourceTable.sources")?)?);
        }
    }
    Ok(v)
}

fn decode_page(b: &[u8]) -> PResult<Page> {
    let mut r = Reader::new(b);
    let mut p = Page::default();
    while let Some((n, f)) = r.next()? {
        match n {
            1 => p.first_key = want_str(f, "Page.first_key")?,
            2 => p.offset = want_varint(f, "Page.offset")?,
            3 => p.length = want_varint(f, "Page.length")?,
            _ => {}
        }
    }
    Ok(p)
}

fn decode_pinned(b: &[u8]) -> PResult<Pinned> {
    let mut r = Reader::new(b);
    let mut p = Pinned::default();
    while let Some((n, f)) = r.next()? {
        match n {
            1 => p.key = want_str(f, "Pinned.key")?,
            2 => p.data_offset = want_varint(f, "Pinned.data_offset")?,
            3 => p.size = want_varint(f, "Pinned.size")?,
            4 => p.csize = want_varint(f, "Pinned.csize")?,
            5 => p.method = want_u32(f, "Pinned.method")?,
            _ => {}
        }
    }
    Ok(p)
}

pub fn decode_cd_index(b: &[u8]) -> PResult<CdIndex> {
    let mut r = Reader::new(b);
    let mut x = CdIndex::default();
    while let Some((n, f)) = r.next()? {
        match n {
            1 => x.pages.push(decode_page(want_len(f, "CdIndex.pages")?)?),
            2 => x.pinned.push(decode_pinned(want_len(f, "CdIndex.pinned")?)?),
            _ => {}
        }
    }
    Ok(x)
}

// ---------------------------------------------------------------- encoding

fn put_varint(out: &mut Vec<u8>, mut v: u64) {
    loop {
        let c = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            out.push(c);
            return;
        }
        out.push(c | 0x80);
    }
}
fn put_tag(out: &mut Vec<u8>, n: u32, wt: u8) {
    put_varint(out, ((n as u64) << 3) | wt as u64);
}
fn put_u64(out: &mut Vec<u8>, n: u32, v: u64) {
    if v != 0 {
        put_tag(out, n, 0);
        put_varint(out, v);
    }
}
fn put_u64_always(out: &mut Vec<u8>, n: u32, v: u64) {
    put_tag(out, n, 0);
    put_varint(out, v);
}
fn put_bytes_always(out: &mut Vec<u8>, n: u32, b: &[u8]) {
    put_tag(out, n, 2);
    put_varint(out, b.len() as u64);
    out.extend_from_slice(b);
}
fn put_bytes(out: &mut Vec<u8>, n: u32, b: &[u8]) {
    if !b.is_empty() {
        put_bytes_always(out, n, b);
    }
}

pub fn encode_range(r: &Range) -> Vec<u8> {
    let mut o = Vec::new();
    put_u64(&mut o, 1, r.source as u64);
    put_u64(&mut o, 3, r.offset);
    put_u64(&mut o, 4, r.length);
    if let Some(d) = &r.data {
        put_bytes_always(&mut o, 5, d);
    }
    o
}

pub fn encode_concat(parts: &[Range]) -> Vec<u8> {
    let mut o = Vec::new();
    for p in parts {
        put_bytes_always(&mut o, 1, &encode_range(p));
    }
    o
}

pub fn encode_source(s: &Source) -> Vec<u8> {
    let mut o = Vec::new();
    match &s.kind {
        Some(SourceKind::Url(u)) => put_bytes_always(&mut o, 1, u.as_bytes()),
        Some(SourceKind::Key(k)) => put_bytes_always(&mut o, 2, k.as_bytes()),
        Some(SourceKind::Data(d)) => put_bytes_always(&mut o, 3, d),
        None => {}
    }
    if let Some(v) = s.size {
        put_u64_always(&mut o, 4, v);
    }
    if let Some(e) = &s.etag {
        put_bytes_always(&mut o, 5, e.as_bytes());
    }
    if let Some(m) = s.modified_not_after {
        put_u64_always(&mut o, 6, m as u64);
    }
    o
}

pub fn encode_source_table(s: &[Source]) -> Vec<u8> {
    let mut o = Vec::new();
    for x in s {
        put_bytes_always(&mut o, 1, &encode_source(x));
    }
    o
}

pub fn encode_cd_index(x: &CdIndex) -> Vec<u8> {
    let mut o = Vec::new();
    for p in &x.pages {
        let mut e = Vec::new();
        put_bytes(&mut e, 1, p.first_key.as_bytes());
        put_u64(&mut e, 2, p.offset);
        put_u64(&mut e, 3, p.length);
        put_bytes_always(&mut o, 1, &e);
    }
    for p in &x.pinned {
        let mut e = Vec::new();
        put_bytes(&mut e, 1, p.key.as_bytes());
        put_u64(&mut e, 2, p.data_offset);
        put_u64(&mut e, 3, p.size);
        put_u64(&mut e, 4, p.csize);
        put_u64(&mut e, 5, p.method as u64);
        put_bytes_always(&mut o, 2, &e);
    }
    o
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodings() {
        // Concat with an all-zero Range part is `0a 00`.
        assert_eq!(encode_concat(&[Range::default()]), vec![0x0a, 0x00]);
        // literal empty range keeps field 5
        let lit = Range { data: Some(vec![]), ..Default::default() };
        assert_eq!(encode_range(&lit), vec![0x2a, 0x00]);
        // negative int64 takes 10 bytes
        let s = Source { kind: Some(SourceKind::Url("a".into())), size: None, etag: None, modified_not_after: Some(-1) };
        let e = encode_source(&s);
        assert_eq!(&e[3..], &[0x30, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01]);
        assert_eq!(decode_source(&e).unwrap(), s);
        let r = Range { source: 3, offset: 300, length: 5, data: None };
        assert_eq!(decode_range(&encode_range(&r)).unwrap(), r);
        // reserved field 2 and unknown fields skipped; non-minimal varint accepted
        assert_eq!(decode_range(&[0x10, 0x05, 0x08, 0x81, 0x00, 0x7d, 0, 0, 0, 0]).unwrap().source, 1);
        // last occurrence wins / oneof last wins
        let s = decode_source(&[0x0a, 0x01, b'u', 0x12, 0x01, b'k']).unwrap();
        assert_eq!(s.kind, Some(SourceKind::Key("k".into())));
    }

    #[test]
    fn rejects_group_wire_type() {
        assert!(decode_range(&[0x7b]).is_err()); // field 15, wt 3
    }
    #[test]
    fn rejects_uint32_overflow() {
        assert!(decode_range(&[0x08, 0x80, 0x80, 0x80, 0x80, 0x10]).is_err());
    }
    #[test]
    fn rejects_bad_utf8() {
        assert!(decode_source(&[0x0a, 0x01, 0xff]).is_err());
    }
    #[test]
    fn rejects_long_varint() {
        assert!(decode_range(&[0x18, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x02]).is_err());
        assert!(decode_range(&[0x18, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x00]).is_err());
    }
    #[test]
    fn rejects_truncated() {
        assert!(decode_range(&[0x2a, 0x05, 0x00]).is_err());
        assert!(decode_range(&[0x18]).is_err());
    }
    #[test]
    fn rejects_field_zero() {
        assert!(decode_range(&[0x00, 0x00]).is_err());
    }
    #[test]
    fn rejects_wrong_wire_type_known_field() {
        assert!(decode_range(&[0x0a, 0x00]).is_err());
    }
}
