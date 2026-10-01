//! vzip writer (§3, §9) and the HARNESS.md JSON description parser.

use crate::json::{self, Value};
use crate::proto::{self, CdIndex, Page, Pinned, Range, Source, SourceKind};
use crate::{deflate, is_strong_etag, uri, INDEX_KEY, SOURCES_KEY};
use std::io::Write;

#[derive(Debug, Clone)]
pub enum EntryData {
    Bytes { data: Vec<u8>, compress: bool },
    Ranges(Vec<Range>),
}

#[derive(Debug, Clone)]
pub struct EntrySpec {
    pub key: String,
    pub data: EntryData,
    pub pinned: bool,
}

#[derive(Debug, Clone)]
pub struct ArchiveSpec {
    pub page_size: Option<u64>,
    pub mirror: bool,
    pub sources: Vec<Source>,
    pub entries: Vec<EntrySpec>,
}

struct Prepared {
    name: Vec<u8>,
    method: u16,
    body: Vec<u8>,
    crc: u32,
    usize: u64,
    /// Reference extra block (header id, payload).
    reference: Option<(u16, Vec<u8>)>,
    pinned: bool,
}

/// Checks the §9.1 requirements and prepares entry bodies.
fn prepare(spec: &ArchiveSpec) -> Result<Vec<Prepared>, String> {
    let mut seen = std::collections::HashMap::new();
    for (i, e) in spec.entries.iter().enumerate() {
        let k = e.key.as_bytes();
        if k.is_empty() {
            return Err(format!("entry {i}: empty key"));
        }
        if k == SOURCES_KEY || k == INDEX_KEY {
            return Err(format!("entry {i}: key {:?} is reserved for a format entry", e.key));
        }
        if k.len() > 65535 {
            return Err(format!("entry {i}: key longer than 65535 bytes"));
        }
        if seen.insert(k.to_vec(), i).is_some() {
            return Err(format!("duplicate key {:?}", e.key));
        }
        if e.pinned {
            if spec.page_size.is_none() {
                return Err(format!("entry {:?} is pinned but there is no page index", e.key));
            }
            if !matches!(e.data, EntryData::Bytes { .. }) {
                return Err(format!("pinned entry {:?} is not a bytes entry", e.key));
            }
        }
    }
    if spec.page_size == Some(0) {
        return Err("page_size must be at least 1".into());
    }
    // Sources.
    let mut source_len: Vec<Option<u64>> = Vec::new();
    for (i, s) in spec.sources.iter().enumerate() {
        let pinned = s.size.is_some() || s.etag.is_some() || s.modified_not_after.is_some();
        match &s.kind {
            None => return Err(format!("source {i} has no kind")),
            Some(SourceKind::Url(u)) => {
                if u.is_empty() {
                    return Err(format!("source {i}: empty url"));
                }
                uri::parse_reference(u).map_err(|e| format!("source {i}: {e}"))?;
                if let Some(e) = &s.etag {
                    if !is_strong_etag(e) {
                        return Err(format!("source {i}: etag {e:?} is not a strong entity tag"));
                    }
                }
                source_len.push(None);
            }
            Some(SourceKind::Key(k)) => {
                if pinned {
                    return Err(format!("source {i}: pins are only allowed on url sources"));
                }
                if k.is_empty() {
                    return Err(format!("source {i}: empty key"));
                }
                if k.as_bytes() == SOURCES_KEY || k.as_bytes() == INDEX_KEY {
                    return Err(format!("source {i}: key source names a format entry"));
                }
                let ent = spec.entries.iter().find(|e| e.key == *k);
                match ent.map(|e| &e.data) {
                    None => return Err(format!("source {i}: key {k:?} is not in the archive")),
                    Some(EntryData::Ranges(_)) => return Err(format!("source {i}: key {k:?} is a reference entry")),
                    Some(EntryData::Bytes { data, .. }) => source_len.push(Some(data.len() as u64)),
                }
            }
            Some(SourceKind::Data(d)) => {
                if pinned {
                    return Err(format!("source {i}: pins are only allowed on url sources"));
                }
                source_len.push(Some(d.len() as u64));
            }
        }
    }
    // Entries.
    let mut out = Vec::new();
    for e in &spec.entries {
        let p = match &e.data {
            EntryData::Bytes { data, compress } => {
                let body = if *compress { deflate(data) } else { data.clone() };
                if data.len() as u64 >= 0xFFFF_FFFF || body.len() as u64 >= 0xFFFF_FFFF {
                    return Err(format!("entry {:?} is 4 GiB or larger", e.key));
                }
                Prepared {
                    name: e.key.as_bytes().to_vec(),
                    method: if *compress { 8 } else { 0 },
                    crc: crc32fast::hash(data),
                    usize: data.len() as u64,
                    body,
                    reference: None,
                    pinned: e.pinned,
                }
            }
            EntryData::Ranges(ranges) => {
                let mut total: u128 = 0;
                for (j, r) in ranges.iter().enumerate() {
                    match &r.data {
                        Some(d) => {
                            if r.source != 0 || r.offset != 0 || r.length != 0 {
                                return Err(format!("entry {:?} range {j}: literal range with source fields", e.key));
                            }
                            total += d.len() as u128;
                        }
                        None => {
                            let src = r.source as usize;
                            if src >= spec.sources.len() {
                                return Err(format!("entry {:?} range {j}: source {src} does not exist", e.key));
                            }
                            let end = r.offset as u128 + r.length as u128;
                            if end > u64::MAX as u128 {
                                return Err(format!("entry {:?} range {j}: offset + length exceeds 2^64-1", e.key));
                            }
                            if let Some(n) = source_len[src] {
                                if end > n as u128 {
                                    return Err(format!(
                                        "entry {:?} range {j}: [{}, {end}) extends past the end of source {src} ({n} bytes)",
                                        e.key, r.offset
                                    ));
                                }
                            }
                            total += r.length as u128;
                        }
                    }
                }
                if total > u64::MAX as u128 {
                    return Err(format!("entry {:?}: reference size exceeds 2^64-1", e.key));
                }
                let (id, payload) = if ranges.len() == 1 {
                    (crate::EXTRA_RANGE, proto::encode_range(&ranges[0]))
                } else {
                    (crate::EXTRA_CONCAT, proto::encode_concat(ranges))
                };
                if payload.len() > crate::MAX_PAYLOAD {
                    return Err(format!("entry {:?}: reference payload is {} bytes (max 65519)", e.key, payload.len()));
                }
                let body = if spec.mirror { payload.clone() } else { Vec::new() };
                Prepared {
                    name: e.key.as_bytes().to_vec(),
                    method: 0,
                    crc: crc32fast::hash(&body),
                    usize: body.len() as u64,
                    body,
                    reference: Some((id, payload)),
                    pinned: false,
                }
            }
        };
        out.push(p);
    }
    Ok(out)
}

struct Counting<W: Write> {
    w: W,
    pos: u64,
}
impl<W: Write> Counting<W> {
    fn put(&mut self, b: &[u8]) -> std::io::Result<()> {
        self.w.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }
}

const DOS_TIME: u16 = 0;
const DOS_DATE: u16 = (1 << 5) | 1; // 1980-01-01

fn local_header(p: &Prepared) -> Vec<u8> {
    let mut h = Vec::with_capacity(30 + p.name.len());
    h.extend_from_slice(&0x04034b50u32.to_le_bytes());
    h.extend_from_slice(&20u16.to_le_bytes());
    h.extend_from_slice(&0x0800u16.to_le_bytes());
    h.extend_from_slice(&p.method.to_le_bytes());
    h.extend_from_slice(&DOS_TIME.to_le_bytes());
    h.extend_from_slice(&DOS_DATE.to_le_bytes());
    h.extend_from_slice(&p.crc.to_le_bytes());
    h.extend_from_slice(&(p.body.len() as u32).to_le_bytes());
    h.extend_from_slice(&(p.usize as u32).to_le_bytes());
    h.extend_from_slice(&(p.name.len() as u16).to_le_bytes());
    h.extend_from_slice(&0u16.to_le_bytes());
    h.extend_from_slice(&p.name);
    h
}

fn cd_record(p: &Prepared, lho: u64) -> Vec<u8> {
    let mut extra = Vec::new();
    if let Some((id, payload)) = &p.reference {
        extra.extend_from_slice(&id.to_le_bytes());
        extra.extend_from_slice(&(payload.len() as u16).to_le_bytes());
        extra.extend_from_slice(payload);
    }
    let z64 = lho >= 0xFFFF_FFFF;
    if z64 {
        extra.extend_from_slice(&1u16.to_le_bytes());
        extra.extend_from_slice(&8u16.to_le_bytes());
        extra.extend_from_slice(&lho.to_le_bytes());
    }
    let mut h = Vec::with_capacity(46 + p.name.len() + extra.len());
    h.extend_from_slice(&0x02014b50u32.to_le_bytes());
    h.extend_from_slice(&20u16.to_le_bytes()); // version made by
    h.extend_from_slice(&(if z64 { 45u16 } else { 20 }).to_le_bytes());
    h.extend_from_slice(&0x0800u16.to_le_bytes());
    h.extend_from_slice(&p.method.to_le_bytes());
    h.extend_from_slice(&DOS_TIME.to_le_bytes());
    h.extend_from_slice(&DOS_DATE.to_le_bytes());
    h.extend_from_slice(&p.crc.to_le_bytes());
    h.extend_from_slice(&(p.body.len() as u32).to_le_bytes());
    h.extend_from_slice(&(p.usize as u32).to_le_bytes());
    h.extend_from_slice(&(p.name.len() as u16).to_le_bytes());
    h.extend_from_slice(&(extra.len() as u16).to_le_bytes());
    h.extend_from_slice(&0u16.to_le_bytes()); // comment
    h.extend_from_slice(&0u16.to_le_bytes()); // disk
    h.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
    h.extend_from_slice(&0u32.to_le_bytes()); // external attrs
    h.extend_from_slice(&(lho.min(0xFFFF_FFFF) as u32).to_le_bytes());
    h.extend_from_slice(&p.name);
    h.extend_from_slice(&extra);
    h
}

fn format_entry(name: &[u8], content: &[u8]) -> Prepared {
    let body = deflate(content);
    Prepared {
        name: name.to_vec(),
        method: 8,
        crc: crc32fast::hash(content),
        usize: content.len() as u64,
        body,
        reference: None,
        pinned: false,
    }
}

/// Writes an archive. Validation happens before any byte is written.
pub fn write_archive<W: Write>(spec: &ArchiveSpec, w: W) -> Result<(), String> {
    let prepared = prepare(spec)?;
    let sources = format_entry(SOURCES_KEY, &proto::encode_source_table(&spec.sources));
    if sources.body.len() as u64 >= 0xFFFF_FFFF || sources.usize >= 0xFFFF_FFFF {
        return Err("source table is too large".into());
    }
    let mut w = Counting { w, pos: 0 };
    let io = |e: std::io::Error| format!("write failed: {e}");
    // (prepared index, local header offset)
    let mut offsets: Vec<u64> = vec![0; prepared.len()];
    let paged = spec.page_size.is_some();
    let write_entry = |w: &mut Counting<W>, p: &Prepared| -> std::io::Result<u64> {
        let lho = w.pos;
        w.put(&local_header(p))?;
        w.put(&p.body)?;
        Ok(lho)
    };
    for (i, p) in prepared.iter().enumerate() {
        if !(paged && p.pinned) {
            offsets[i] = write_entry(&mut w, p).map_err(io)?;
        }
    }
    let sources_lho = write_entry(&mut w, &sources).map_err(io)?;
    let sources_body = sources_lho + 30 + sources.name.len() as u64;
    let mut index_info = None;
    let mut cd_body: Vec<u8> = Vec::new();
    if paged {
        for (i, p) in prepared.iter().enumerate() {
            if p.pinned {
                offsets[i] = write_entry(&mut w, p).map_err(io)?;
            }
        }
        // Body records, sorted in UTF-8 order, grouped into pages.
        let mut order: Vec<usize> = (0..prepared.len()).collect();
        order.sort_by(|&a, &b| prepared[a].name.cmp(&prepared[b].name));
        let page_size = spec.page_size.unwrap();
        let mut pages: Vec<Page> = Vec::new();
        for &i in &order {
            let rec = cd_record(&prepared[i], offsets[i]);
            let off = cd_body.len() as u64;
            match pages.last_mut() {
                Some(pg) if pg.length + rec.len() as u64 <= page_size => pg.length += rec.len() as u64,
                _ => pages.push(Page {
                    first_key: String::from_utf8(prepared[i].name.clone()).unwrap(),
                    offset: off,
                    length: rec.len() as u64,
                }),
            }
            cd_body.extend_from_slice(&rec);
        }
        let pinned = prepared
            .iter()
            .enumerate()
            .filter(|(_, p)| p.pinned)
            .map(|(i, p)| Pinned {
                key: String::from_utf8(p.name.clone()).unwrap(),
                data_offset: offsets[i] + 30 + p.name.len() as u64,
                size: p.usize,
                csize: p.body.len() as u64,
                method: p.method as u32,
            })
            .collect();
        let idx = format_entry(INDEX_KEY, &proto::encode_cd_index(&CdIndex { pages, pinned }));
        if idx.body.len() as u64 >= 0xFFFF_FFFF {
            return Err("page index is too large".into());
        }
        let lho = write_entry(&mut w, &idx).map_err(io)?;
        index_info = Some((idx, lho));
    } else {
        for (i, p) in prepared.iter().enumerate() {
            cd_body.extend_from_slice(&cd_record(p, offsets[i]));
        }
    }
    cd_body.extend_from_slice(&cd_record(&sources, sources_lho));
    if let Some((idx, lho)) = &index_info {
        cd_body.extend_from_slice(&cd_record(idx, *lho));
    }
    let cd_offset = w.pos;
    let cd_size = cd_body.len() as u64;
    w.put(&cd_body).map_err(io)?;
    let n = prepared.len() as u64 + 1 + index_info.is_some() as u64;
    if n >= 0xFFFF || cd_size >= 0xFFFF_FFFF || cd_offset >= 0xFFFF_FFFF {
        let z_off = w.pos;
        let mut z = Vec::new();
        z.extend_from_slice(&0x06064b50u32.to_le_bytes());
        z.extend_from_slice(&44u64.to_le_bytes());
        z.extend_from_slice(&45u16.to_le_bytes()); // made by
        z.extend_from_slice(&45u16.to_le_bytes()); // needed
        z.extend_from_slice(&0u32.to_le_bytes());
        z.extend_from_slice(&0u32.to_le_bytes());
        z.extend_from_slice(&n.to_le_bytes());
        z.extend_from_slice(&n.to_le_bytes());
        z.extend_from_slice(&cd_size.to_le_bytes());
        z.extend_from_slice(&cd_offset.to_le_bytes());
        z.extend_from_slice(&0x07064b50u32.to_le_bytes());
        z.extend_from_slice(&0u32.to_le_bytes());
        z.extend_from_slice(&z_off.to_le_bytes());
        z.extend_from_slice(&1u32.to_le_bytes());
        w.put(&z).map_err(io)?;
    }
    let mut comment = b"vzip/0".to_vec();
    comment.extend_from_slice(&sources_body.to_le_bytes());
    comment.extend_from_slice(&(sources.body.len() as u64).to_le_bytes());
    if let Some((idx, lho)) = &index_info {
        comment.extend_from_slice(&(lho + 30 + idx.name.len() as u64).to_le_bytes());
        comment.extend_from_slice(&(idx.body.len() as u64).to_le_bytes());
    }
    let mut e = Vec::new();
    e.extend_from_slice(&0x06054b50u32.to_le_bytes());
    e.extend_from_slice(&0u16.to_le_bytes());
    e.extend_from_slice(&0u16.to_le_bytes());
    e.extend_from_slice(&(n.min(0xFFFF) as u16).to_le_bytes());
    e.extend_from_slice(&(n.min(0xFFFF) as u16).to_le_bytes());
    e.extend_from_slice(&(cd_size.min(0xFFFF_FFFF) as u32).to_le_bytes());
    e.extend_from_slice(&(cd_offset.min(0xFFFF_FFFF) as u32).to_le_bytes());
    e.extend_from_slice(&(comment.len() as u16).to_le_bytes());
    e.extend_from_slice(&comment);
    w.put(&e).map_err(io)?;
    w.w.flush().map_err(io)?;
    Ok(())
}

// ------------------------------------------------------------ JSON description

fn int_field(v: &Value, what: &str, min: i128, max: i128) -> Result<i128, String> {
    let n = v.as_int().ok_or_else(|| format!("{what} must be a JSON integer"))?;
    if n < min || n > max {
        return Err(format!("{what} out of range"));
    }
    Ok(n)
}

fn not_null<'a>(v: &'a Value, k: &str) -> Result<Option<&'a Value>, String> {
    match v.get(k) {
        None => Ok(None),
        Some(Value::Null) => Err(format!("{k} must not be null")),
        Some(x) => Ok(Some(x)),
    }
}

fn bool_field(v: &Value, k: &str, default: bool) -> Result<bool, String> {
    match not_null(v, k)? {
        None => Ok(default),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(format!("{k} must be a boolean")),
    }
}

fn str_field(v: &Value, k: &str) -> Result<Option<String>, String> {
    match not_null(v, k)? {
        None => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(format!("{k} must be a string")),
    }
}

fn hex_field(v: &Value, k: &str) -> Result<Option<Vec<u8>>, String> {
    match str_field(v, k)? {
        None => Ok(None),
        Some(s) => json::unhex(&s).map(Some).ok_or_else(|| format!("{k} must be lowercase even-length hex")),
    }
}

fn array_field<'a>(v: &'a Value, k: &str) -> Result<&'a [Value], String> {
    match not_null(v, k)? {
        None => Ok(&[]),
        Some(Value::Array(a)) => Ok(a),
        Some(_) => Err(format!("{k} must be an array")),
    }
}

const MAX_U64: i128 = u64::MAX as i128;

/// Parses a HARNESS.md write description.
pub fn parse_description(bytes: &[u8]) -> Result<ArchiveSpec, String> {
    let v = json::parse(bytes)?;
    if !matches!(v, Value::Object(_)) {
        return Err("description must be a JSON object".into());
    }
    let page_size = match v.get("page_size") {
        None | Some(Value::Null) => None,
        Some(x) => Some(int_field(x, "page_size", 1, MAX_U64)? as u64),
    };
    let mirror = bool_field(&v, "mirror", true)?;
    let mut sources = Vec::new();
    for (i, s) in array_field(&v, "sources")?.iter().enumerate() {
        if !matches!(s, Value::Object(_)) {
            return Err(format!("source {i} must be an object"));
        }
        let url = str_field(s, "url")?;
        let key = str_field(s, "key")?;
        let data = hex_field(s, "data")?;
        let kind = match (url, key, data) {
            (Some(u), None, None) => SourceKind::Url(u),
            (None, Some(k), None) => SourceKind::Key(k),
            (None, None, Some(d)) => SourceKind::Data(d),
            _ => return Err(format!("source {i} must have exactly one of url, key, data")),
        };
        let size = match not_null(s, "size")? {
            None => None,
            Some(x) => Some(int_field(x, "size", 0, MAX_U64)? as u64),
        };
        let etag = str_field(s, "etag")?;
        let modified_not_after = match not_null(s, "modified_not_after")? {
            None => None,
            Some(x) => Some(int_field(x, "modified_not_after", i64::MIN as i128, i64::MAX as i128)? as i64),
        };
        sources.push(Source { kind: Some(kind), size, etag, modified_not_after });
    }
    let mut entries = Vec::new();
    for (i, e) in array_field(&v, "entries")?.iter().enumerate() {
        if !matches!(e, Value::Object(_)) {
            return Err(format!("entry {i} must be an object"));
        }
        let key = str_field(e, "key")?.ok_or_else(|| format!("entry {i} has no key"))?;
        let compress = bool_field(e, "compress", false)?;
        let pinned = bool_field(e, "pinned", false)?;
        let bytes = hex_field(e, "bytes")?;
        let ranges = match not_null(e, "ranges")? {
            None => None,
            Some(Value::Array(a)) => Some(a),
            Some(_) => return Err(format!("entry {i}: ranges must be an array")),
        };
        let data = match (bytes, ranges) {
            (Some(b), None) => EntryData::Bytes { data: b, compress },
            (None, Some(rs)) => {
                if compress {
                    return Err(format!("entry {i}: compress is only allowed on bytes entries"));
                }
                let mut out = Vec::new();
                for (j, r) in rs.iter().enumerate() {
                    if !matches!(r, Value::Object(_)) {
                        return Err(format!("entry {i} range {j} must be an object"));
                    }
                    let lit = hex_field(r, "data")?;
                    let src = not_null(r, "source")?;
                    let off = not_null(r, "offset")?;
                    let len = not_null(r, "length")?;
                    match lit {
                        Some(d) => {
                            if src.is_some() || off.is_some() || len.is_some() {
                                return Err(format!("entry {i} range {j} mixes data with source fields"));
                            }
                            out.push(Range { data: Some(d), ..Default::default() });
                        }
                        None => {
                            let g = |x: Option<&Value>, n: &str, max: i128| -> Result<i128, String> {
                                x.map(|x| int_field(x, n, 0, max)).unwrap_or(Ok(0))
                            };
                            out.push(Range {
                                source: g(src, "source", u32::MAX as i128)? as u32,
                                offset: g(off, "offset", MAX_U64)? as u64,
                                length: g(len, "length", MAX_U64)? as u64,
                                data: None,
                            });
                        }
                    }
                }
                EntryData::Ranges(out)
            }
            _ => return Err(format!("entry {i} must have exactly one of bytes, ranges")),
        };
        entries.push(EntrySpec { key, data, pinned });
    }
    Ok(ArchiveSpec { page_size, mirror, sources, entries })
}
