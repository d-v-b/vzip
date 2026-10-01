//! vzip writer (spec §9).

use crate::proto::{self, CdIndex, Page, Pinned, Range, Source, SourceKind};
use crate::reader::is_strong_etag;
use crate::uri;
use crate::zip::*;
use std::collections::HashMap;
use std::io::Write;

#[derive(Debug, Clone)]
pub enum Value {
    Bytes { data: Vec<u8>, compress: bool },
    Ranges(Vec<Range>),
}

#[derive(Debug, Clone)]
pub struct WEntry {
    pub key: String,
    pub value: Value,
    pub pinned: bool,
}

#[derive(Debug, Clone)]
pub struct WriteSpec {
    /// `None`: no page index.
    pub page_size: Option<u64>,
    /// Reference entry bodies mirror the payload.
    pub mirror: bool,
    pub sources: Vec<Source>,
    pub entries: Vec<WEntry>,
}

impl Default for WriteSpec {
    fn default() -> Self {
        WriteSpec { page_size: None, mirror: true, sources: vec![], entries: vec![] }
    }
}

const DOS_DATE: u16 = 0x0021; // 1980-01-01
const DOS_TIME: u16 = 0;
const MAX_PAYLOAD: usize = 65519;

struct Placed {
    name: Vec<u8>,
    method: u16,
    crc: u32,
    csize: u64,
    usize: u64,
    lho: u64,
    ref_block: Option<(u16, Vec<u8>)>,
}

fn deflate(data: &[u8]) -> Vec<u8> {
    let mut e = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

fn put16(o: &mut Vec<u8>, v: u16) {
    o.extend_from_slice(&v.to_le_bytes());
}
fn put32(o: &mut Vec<u8>, v: u32) {
    o.extend_from_slice(&v.to_le_bytes());
}
fn put64(o: &mut Vec<u8>, v: u64) {
    o.extend_from_slice(&v.to_le_bytes());
}

fn write_local(out: &mut Vec<u8>, name: &[u8], method: u16, crc: u32, csize: u64, usize: u64, body: &[u8]) -> u64 {
    let lho = out.len() as u64;
    put32(out, SIG_LOCAL);
    put16(out, 20);
    put16(out, 0x0800);
    put16(out, method);
    put16(out, DOS_TIME);
    put16(out, DOS_DATE);
    put32(out, crc);
    put32(out, csize as u32);
    put32(out, usize as u32);
    put16(out, name.len() as u16);
    put16(out, 0);
    out.extend_from_slice(name);
    out.extend_from_slice(body);
    lho
}

fn cd_record(p: &Placed) -> Vec<u8> {
    let zip64 = p.lho >= 0xFFFF_FFFF;
    let mut extra = Vec::new();
    if zip64 {
        put16(&mut extra, ID_ZIP64);
        put16(&mut extra, 8);
        put64(&mut extra, p.lho);
    }
    if let Some((id, payload)) = &p.ref_block {
        put16(&mut extra, *id);
        put16(&mut extra, payload.len() as u16);
        extra.extend_from_slice(payload);
    }
    let mut o = Vec::with_capacity(46 + p.name.len() + extra.len());
    put32(&mut o, SIG_CDR);
    put16(&mut o, 20); // version made by
    put16(&mut o, if zip64 { 45 } else { 20 });
    put16(&mut o, 0x0800);
    put16(&mut o, p.method);
    put16(&mut o, DOS_TIME);
    put16(&mut o, DOS_DATE);
    put32(&mut o, p.crc);
    put32(&mut o, p.csize as u32);
    put32(&mut o, p.usize as u32);
    put16(&mut o, p.name.len() as u16);
    put16(&mut o, extra.len() as u16);
    put16(&mut o, 0); // comment
    put16(&mut o, 0); // disk start
    put16(&mut o, 0); // internal attrs
    put32(&mut o, 0); // external attrs
    put32(&mut o, if zip64 { 0xFFFF_FFFF } else { p.lho as u32 });
    o.extend_from_slice(&p.name);
    o.extend_from_slice(&extra);
    o
}

/// Validate a spec against §9.1. Returns the encoded reference payloads.
fn validate(spec: &WriteSpec) -> Result<HashMap<usize, (u16, Vec<u8>)>, String> {
    if let Some(ps) = spec.page_size {
        if ps < 1 {
            return Err("page_size must be at least 1".into());
        }
    }
    // Keys.
    let mut keys: HashMap<&str, &WEntry> = HashMap::new();
    for e in &spec.entries {
        if e.key.is_empty() {
            return Err("empty key".into());
        }
        if e.key == SOURCES_KEY || e.key == INDEX_KEY {
            return Err(format!("key {} is reserved for a format entry", e.key));
        }
        if e.key.len() > 65535 {
            return Err(format!("key of {} bytes is longer than 65535 bytes", e.key.len()));
        }
        if keys.insert(e.key.as_str(), e).is_some() {
            return Err(format!("duplicate key {:?}", e.key));
        }
        if e.pinned {
            if spec.page_size.is_none() {
                return Err(format!("entry {:?} is pinned but there is no page index", e.key));
            }
            if !matches!(e.value, Value::Bytes { .. }) {
                return Err(format!("pinned entry {:?} is not a bytes entry", e.key));
            }
        }
        if let Value::Bytes { data, .. } = &e.value {
            if data.len() as u64 >= 0xFFFF_FFFF {
                return Err(format!("entry {:?} is 4 GiB or larger", e.key));
            }
        }
    }
    // Sources.
    for (i, s) in spec.sources.iter().enumerate() {
        let has_pin = s.size.is_some() || s.etag.is_some() || s.modified_not_after.is_some();
        match &s.kind {
            None => return Err(format!("source {i} has no kind")),
            Some(SourceKind::Url(u)) => {
                if u.is_empty() {
                    return Err(format!("source {i}: empty url"));
                }
                if !uri::is_uri_reference(u) {
                    return Err(format!("source {i}: {u:?} is not an RFC 3986 URI-reference"));
                }
                if let Some(e) = &s.etag {
                    if !is_strong_etag(e) {
                        return Err(format!("source {i}: etag {e:?} is not a strong entity tag"));
                    }
                }
            }
            Some(SourceKind::Key(k)) => {
                if has_pin {
                    return Err(format!("source {i}: pin on a key source"));
                }
                if k == SOURCES_KEY || k == INDEX_KEY {
                    return Err(format!("source {i}: key source names format entry {k}"));
                }
                match keys.get(k.as_str()) {
                    None => return Err(format!("source {i}: key source names absent key {k:?}")),
                    Some(e) if matches!(e.value, Value::Ranges(_)) => {
                        return Err(format!("source {i}: key source names reference entry {k:?}"))
                    }
                    _ => {}
                }
            }
            Some(SourceKind::Data(_)) => {
                if has_pin {
                    return Err(format!("source {i}: pin on a data source"));
                }
            }
        }
    }
    // References.
    let mut payloads = HashMap::new();
    for (ei, e) in spec.entries.iter().enumerate() {
        if let Value::Ranges(parts) = &e.value {
            let mut total: u64 = 0;
            for p in parts {
                proto::check_range(p, spec.sources.len()).map_err(|m| format!("entry {:?}: {m}", e.key))?;
                total = total
                    .checked_add(p.size())
                    .ok_or_else(|| format!("entry {:?}: total size exceeds 2^64-1", e.key))?;
            }
            let (id, payload) = if parts.len() == 1 {
                (ID_RANGE, proto::encode_range(&parts[0]))
            } else {
                (ID_CONCAT, proto::encode_concat(parts))
            };
            if payload.len() > MAX_PAYLOAD {
                return Err(format!(
                    "entry {:?}: reference payload of {} bytes exceeds 65519",
                    e.key,
                    payload.len()
                ));
            }
            payloads.insert(ei, (id, payload));
        }
    }
    Ok(payloads)
}

/// Build an archive in memory.
pub fn write_archive(spec: &WriteSpec) -> Result<Vec<u8>, String> {
    let payloads = validate(spec)?;
    let mut out = Vec::new();
    let mut body_placed: Vec<Placed> = Vec::new();

    let place_entry = |out: &mut Vec<u8>, ei: usize, e: &WEntry| -> Result<Placed, String> {
        let name = e.key.as_bytes().to_vec();
        match &e.value {
            Value::Bytes { data, compress } => {
                let crc = crc32fast::hash(data);
                let (method, body) = if *compress { (8u16, deflate(data)) } else { (0u16, data.clone()) };
                if body.len() as u64 >= 0xFFFF_FFFF {
                    return Err(format!("entry {:?}: compressed size is 4 GiB or larger", e.key));
                }
                let lho = write_local(out, &name, method, crc, body.len() as u64, data.len() as u64, &body);
                Ok(Placed { name, method, crc, csize: body.len() as u64, usize: data.len() as u64, lho, ref_block: None })
            }
            Value::Ranges(_) => {
                let (id, payload) = payloads[&ei].clone();
                let body: &[u8] = if spec.mirror { &payload } else { &[] };
                let crc = crc32fast::hash(body);
                let lho = write_local(out, &name, 0, crc, body.len() as u64, body.len() as u64, body);
                Ok(Placed {
                    name,
                    method: 0,
                    crc,
                    csize: body.len() as u64,
                    usize: body.len() as u64,
                    lho,
                    ref_block: Some((id, payload)),
                })
            }
        }
    };

    // 1. Unpinned entries, in input order.
    for (ei, e) in spec.entries.iter().enumerate() {
        if !e.pinned {
            body_placed.push(place_entry(&mut out, ei, e)?);
        }
    }
    // 2. Source table.
    let st = proto::encode_source_table(&spec.sources);
    let st_c = deflate(&st);
    let st_lho = write_local(&mut out, SOURCES_KEY.as_bytes(), 8, crc32fast::hash(&st), st_c.len() as u64, st.len() as u64, &st_c);
    let sources_offset = st_lho + 30 + SOURCES_KEY.len() as u64;
    let sources_placed = Placed {
        name: SOURCES_KEY.as_bytes().to_vec(),
        method: 8,
        crc: crc32fast::hash(&st),
        csize: st_c.len() as u64,
        usize: st.len() as u64,
        lho: st_lho,
        ref_block: None,
    };
    // 3. Pinned entries.
    let mut pinned = Vec::new();
    for (ei, e) in spec.entries.iter().enumerate() {
        if e.pinned {
            let p = place_entry(&mut out, ei, e)?;
            pinned.push(Pinned {
                key: e.key.clone(),
                data_offset: p.lho + 30 + p.name.len() as u64,
                size: p.usize,
                csize: p.csize,
                method: p.method as u32,
            });
            body_placed.push(p);
        }
    }
    // Body records, sorted in UTF-8 order.
    body_placed.sort_by(|a, b| a.name.cmp(&b.name));
    let body_recs: Vec<Vec<u8>> = body_placed.iter().map(cd_record).collect();

    // 4. Page index.
    let mut format_placed = vec![sources_placed];
    let mut index_loc = None;
    if let Some(ps) = spec.page_size {
        let mut pages: Vec<Page> = Vec::new();
        let mut off = 0u64;
        for (p, rec) in body_placed.iter().zip(&body_recs) {
            let len = rec.len() as u64;
            let start_new = match pages.last() {
                None => true,
                Some(last) => last.length + len > ps,
            };
            if start_new {
                pages.push(Page { first_key: String::from_utf8(p.name.clone()).unwrap(), offset: off, length: len });
            } else {
                pages.last_mut().unwrap().length += len;
            }
            off += len;
        }
        let idx = proto::encode_cd_index(&CdIndex { pages, pinned });
        let idx_c = deflate(&idx);
        let lho = write_local(&mut out, INDEX_KEY.as_bytes(), 8, crc32fast::hash(&idx), idx_c.len() as u64, idx.len() as u64, &idx_c);
        index_loc = Some((lho + 30 + INDEX_KEY.len() as u64, idx_c.len() as u64));
        format_placed.push(Placed {
            name: INDEX_KEY.as_bytes().to_vec(),
            method: 8,
            crc: crc32fast::hash(&idx),
            csize: idx_c.len() as u64,
            usize: idx.len() as u64,
            lho,
            ref_block: None,
        });
    }
    // 5. Central directory.
    let cd_offset = out.len() as u64;
    for r in &body_recs {
        out.extend_from_slice(r);
    }
    for p in &format_placed {
        out.extend_from_slice(&cd_record(p));
    }
    let cd_size = out.len() as u64 - cd_offset;
    let n = (body_recs.len() + format_placed.len()) as u64;
    // 6. ZIP64 end records, if needed.
    if n >= 0xFFFF || cd_size >= 0xFFFF_FFFF || cd_offset >= 0xFFFF_FFFF {
        let z_off = out.len() as u64;
        put32(&mut out, SIG_ZIP64_EOCD);
        put64(&mut out, 44);
        put16(&mut out, 20); // made by (§9.2)
        put16(&mut out, 45); // needed
        put32(&mut out, 0);
        put32(&mut out, 0);
        put64(&mut out, n);
        put64(&mut out, n);
        put64(&mut out, cd_size);
        put64(&mut out, cd_offset);
        put32(&mut out, SIG_ZIP64_LOC);
        put32(&mut out, 0);
        put64(&mut out, z_off);
        put32(&mut out, 1);
    }
    // 7. End of central directory + comment.
    let mut comment = b"vzip/0".to_vec();
    put64(&mut comment, sources_offset);
    put64(&mut comment, st_c.len() as u64);
    if let Some((o, s)) = index_loc {
        put64(&mut comment, o);
        put64(&mut comment, s);
    }
    put32(&mut out, SIG_EOCD);
    put16(&mut out, 0);
    put16(&mut out, 0);
    put16(&mut out, n.min(0xFFFF) as u16);
    put16(&mut out, n.min(0xFFFF) as u16);
    put32(&mut out, cd_size.min(0xFFFF_FFFF) as u32);
    put32(&mut out, cd_offset.min(0xFFFF_FFFF) as u32);
    put16(&mut out, comment.len() as u16);
    out.extend_from_slice(&comment);
    Ok(out)
}
