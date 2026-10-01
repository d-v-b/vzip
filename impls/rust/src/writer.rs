//! vzip writer (SPEC §3, §9).

use crate::proto::{self, CdIndex, Page, Pinned, Range, Source, SourceKind};
use crate::reader::{INDEX_KEY, MAGIC, SOURCES_KEY};
use crate::uri;
use std::collections::HashMap;
use std::io::Write;

pub const MAX_PAYLOAD: usize = 65519;

#[derive(Debug, Clone)]
pub enum Content {
    Bytes { data: Vec<u8>, compress: bool },
    Ranges(Vec<Range>),
}

#[derive(Debug, Clone)]
pub struct Entry {
    pub key: String,
    pub content: Content,
    pub pinned: bool,
}

#[derive(Debug, Clone)]
pub struct Spec {
    /// None: no page index.
    pub page_size: Option<u64>,
    pub mirror: bool,
    pub sources: Vec<Source>,
    pub entries: Vec<Entry>,
}

fn deflate(data: &[u8]) -> Vec<u8> {
    let mut e = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

/// Encode a reference payload: (extra ID, payload) per §4.3.
pub fn encode_reference(ranges: &[Range]) -> (u16, Vec<u8>) {
    if ranges.len() == 1 {
        (0x7A76, ranges[0].encode())
    } else {
        (0x7A77, proto::encode_concat(ranges))
    }
}

struct Prepared {
    name: String,
    method: u16,
    body: Vec<u8>,
    crc: u32,
    usize: u64,
    reference: Option<(u16, Vec<u8>)>,
    lho: u64,
}

fn validate(spec: &Spec) -> Result<(), String> {
    let ns = spec.sources.len();
    // keys
    let mut kinds: HashMap<&str, bool> = HashMap::new(); // key -> is_bytes
    for e in &spec.entries {
        if e.key.is_empty() {
            return Err("empty key".into());
        }
        if e.key == SOURCES_KEY || e.key == INDEX_KEY {
            return Err(format!("key {:?} is a format entry's key", e.key));
        }
        if kinds.insert(&e.key, matches!(e.content, Content::Bytes { .. })).is_some() {
            return Err(format!("duplicate key {:?}", e.key));
        }
        if e.pinned {
            if spec.page_size.is_none() {
                return Err(format!("entry {:?} is pinned but there is no page index", e.key));
            }
            if !matches!(e.content, Content::Bytes { .. }) {
                return Err(format!("pinned entry {:?} is not a bytes entry", e.key));
            }
        }
        match &e.content {
            Content::Bytes { data, .. } => {
                if data.len() as u64 >= 0xFFFF_FFFF {
                    return Err(format!("entry {:?} is too large", e.key));
                }
            }
            Content::Ranges(rs) => {
                let mut total: u64 = 0;
                for r in rs {
                    if r.data.is_none() {
                        if r.source as usize >= ns {
                            return Err(format!(
                                "entry {:?}: source index {} out of bounds ({ns} sources)",
                                e.key, r.source
                            ));
                        }
                        if r.offset.checked_add(r.length).is_none() {
                            return Err(format!("entry {:?}: offset + length exceeds 2^64-1", e.key));
                        }
                    }
                    total = total
                        .checked_add(r.size())
                        .ok_or_else(|| format!("entry {:?}: total size exceeds 2^64-1", e.key))?;
                }
                let (_, payload) = encode_reference(rs);
                if payload.len() > MAX_PAYLOAD {
                    return Err(format!(
                        "entry {:?}: reference payload is {} bytes (max {MAX_PAYLOAD})",
                        e.key,
                        payload.len()
                    ));
                }
            }
        }
    }
    if let Some(ps) = spec.page_size {
        if ps < 1 {
            return Err("page_size must be at least 1".into());
        }
    }
    for (i, s) in spec.sources.iter().enumerate() {
        match &s.kind {
            SourceKind::Url(u) => {
                if u.is_empty() {
                    return Err(format!("source {i}: empty url"));
                }
                uri::parse_uri_reference(u).map_err(|e| format!("source {i}: invalid url {u:?}: {e}"))?;
            }
            SourceKind::Key(k) => {
                if s.has_pins() {
                    return Err(format!("source {i}: pin on a key source"));
                }
                if k == SOURCES_KEY || k == INDEX_KEY {
                    return Err(format!("source {i}: key source names a format entry"));
                }
                match kinds.get(k.as_str()) {
                    None => return Err(format!("source {i}: key {k:?} is absent")),
                    Some(false) => return Err(format!("source {i}: key {k:?} is a reference entry")),
                    Some(true) => {}
                }
            }
            SourceKind::Data(_) => {
                if s.has_pins() {
                    return Err(format!("source {i}: pin on a data source"));
                }
            }
        }
        if let Some(e) = &s.etag {
            if !proto::is_strong_etag(e) {
                return Err(format!("source {i}: etag {e:?} is not a strong entity tag"));
            }
        }
    }
    Ok(())
}

fn local_header(p: &Prepared) -> Vec<u8> {
    let mut h = Vec::with_capacity(30 + p.name.len());
    h.extend_from_slice(&0x04034b50u32.to_le_bytes());
    h.extend_from_slice(&20u16.to_le_bytes()); // version needed
    h.extend_from_slice(&0x0800u16.to_le_bytes()); // flags: UTF-8
    h.extend_from_slice(&p.method.to_le_bytes());
    h.extend_from_slice(&0u16.to_le_bytes()); // time 00:00
    h.extend_from_slice(&0x0021u16.to_le_bytes()); // date 1980-01-01
    h.extend_from_slice(&p.crc.to_le_bytes());
    h.extend_from_slice(&(p.body.len() as u32).to_le_bytes());
    h.extend_from_slice(&(p.usize as u32).to_le_bytes());
    h.extend_from_slice(&(p.name.len() as u16).to_le_bytes());
    h.extend_from_slice(&0u16.to_le_bytes()); // extra length
    h.extend_from_slice(p.name.as_bytes());
    h
}

fn cd_record(p: &Prepared) -> Vec<u8> {
    let mut extra = Vec::new();
    let big = p.lho >= 0xFFFF_FFFF;
    if big {
        extra.extend_from_slice(&0x0001u16.to_le_bytes());
        extra.extend_from_slice(&8u16.to_le_bytes());
        extra.extend_from_slice(&p.lho.to_le_bytes());
    }
    if let Some((id, payload)) = &p.reference {
        extra.extend_from_slice(&id.to_le_bytes());
        extra.extend_from_slice(&(payload.len() as u16).to_le_bytes());
        extra.extend_from_slice(payload);
    }
    let mut h = Vec::with_capacity(46 + p.name.len() + extra.len());
    h.extend_from_slice(&0x02014b50u32.to_le_bytes());
    h.extend_from_slice(&20u16.to_le_bytes()); // version made by
    h.extend_from_slice(&(if big { 45u16 } else { 20u16 }).to_le_bytes());
    h.extend_from_slice(&0x0800u16.to_le_bytes());
    h.extend_from_slice(&p.method.to_le_bytes());
    h.extend_from_slice(&0u16.to_le_bytes());
    h.extend_from_slice(&0x0021u16.to_le_bytes());
    h.extend_from_slice(&p.crc.to_le_bytes());
    h.extend_from_slice(&(p.body.len() as u32).to_le_bytes());
    h.extend_from_slice(&(p.usize as u32).to_le_bytes());
    h.extend_from_slice(&(p.name.len() as u16).to_le_bytes());
    h.extend_from_slice(&(extra.len() as u16).to_le_bytes());
    h.extend_from_slice(&0u16.to_le_bytes()); // comment length
    h.extend_from_slice(&0u16.to_le_bytes()); // disk start
    h.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
    h.extend_from_slice(&0u32.to_le_bytes()); // external attrs
    h.extend_from_slice(&(if big { 0xFFFF_FFFFu32 } else { p.lho as u32 }).to_le_bytes());
    h.extend_from_slice(p.name.as_bytes());
    h.extend_from_slice(&extra);
    h
}

fn prepare_bytes(name: &str, data: &[u8], compress: bool) -> Result<Prepared, String> {
    let crc = crc32fast::hash(data);
    let (method, body) = if compress { (8u16, deflate(data)) } else { (0u16, data.to_vec()) };
    if body.len() as u64 >= 0xFFFF_FFFF {
        return Err(format!("entry {name:?}: compressed size too large"));
    }
    Ok(Prepared { name: name.to_string(), method, body, crc, usize: data.len() as u64, reference: None, lho: 0 })
}

fn emit(out: &mut Vec<u8>, p: &mut Prepared) -> u64 {
    p.lho = out.len() as u64;
    out.extend_from_slice(&local_header(p));
    let body_offset = out.len() as u64;
    out.extend_from_slice(&p.body);
    body_offset
}

/// Produce an archive, or reject the input (§9.1).
pub fn write_archive(spec: &Spec) -> Result<Vec<u8>, String> {
    validate(spec)?;
    let mut normal = Vec::new();
    let mut pinned = Vec::new();
    for e in &spec.entries {
        let p = match &e.content {
            Content::Bytes { data, compress } => prepare_bytes(&e.key, data, *compress)?,
            Content::Ranges(rs) => {
                let (id, payload) = encode_reference(rs);
                let body = if spec.mirror { payload.clone() } else { Vec::new() };
                Prepared {
                    name: e.key.clone(),
                    method: 0,
                    crc: crc32fast::hash(&body),
                    usize: body.len() as u64,
                    body,
                    reference: Some((id, payload)),
                    lho: 0,
                }
            }
        };
        if e.pinned { pinned.push(p) } else { normal.push(p) }
    }
    let mut out = Vec::new();
    for p in normal.iter_mut() {
        emit(&mut out, p);
    }
    let st = proto::encode_source_table(&spec.sources);
    let mut sources = prepare_bytes(SOURCES_KEY, &st, true)?;
    let sources_offset = emit(&mut out, &mut sources);
    let sources_size = sources.body.len() as u64;
    let mut pins = Vec::new();
    for p in pinned.iter_mut() {
        let off = emit(&mut out, p);
        pins.push(Pinned {
            key: p.name.clone(),
            data_offset: off,
            size: p.usize,
            csize: p.body.len() as u64,
            method: p.method as u32,
        });
    }
    let mut body: Vec<Prepared> = normal.into_iter().chain(pinned).collect();
    let mut cd = Vec::new();
    let mut index_loc = None;
    let mut format_recs = vec![sources];
    if let Some(ps) = spec.page_size {
        body.sort_by(|a, b| a.name.as_bytes().cmp(b.name.as_bytes()));
        let mut pages: Vec<Page> = Vec::new();
        for p in &body {
            let rec = cd_record(p);
            let start = cd.len() as u64;
            match pages.last_mut() {
                Some(last) if last.length + rec.len() as u64 <= ps => last.length += rec.len() as u64,
                _ => pages.push(Page { first_key: p.name.clone(), offset: start, length: rec.len() as u64 }),
            }
            cd.extend_from_slice(&rec);
        }
        let idx = CdIndex { pages, pinned: pins };
        let mut ip = prepare_bytes(INDEX_KEY, &proto::encode_index(&idx), true)?;
        let ioff = emit(&mut out, &mut ip);
        index_loc = Some((ioff, ip.body.len() as u64));
        format_recs.push(ip);
    } else {
        for p in &body {
            cd.extend_from_slice(&cd_record(p));
        }
    }
    for p in &format_recs {
        cd.extend_from_slice(&cd_record(p));
    }
    let n_entries = (body.len() + format_recs.len()) as u64;
    let cd_offset = out.len() as u64;
    let cd_size = cd.len() as u64;
    out.extend_from_slice(&cd);
    let need64 = n_entries >= 0xFFFF || cd_size >= 0xFFFF_FFFF || cd_offset >= 0xFFFF_FFFF;
    if need64 {
        let z_off = out.len() as u64;
        out.extend_from_slice(&0x06064b50u32.to_le_bytes());
        out.extend_from_slice(&44u64.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes()); // made by
        out.extend_from_slice(&45u16.to_le_bytes()); // needed
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&n_entries.to_le_bytes());
        out.extend_from_slice(&n_entries.to_le_bytes());
        out.extend_from_slice(&cd_size.to_le_bytes());
        out.extend_from_slice(&cd_offset.to_le_bytes());
        out.extend_from_slice(&0x07064b50u32.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&z_off.to_le_bytes());
        out.extend_from_slice(&1u32.to_le_bytes());
    }
    let mut comment = Vec::new();
    comment.extend_from_slice(MAGIC);
    comment.extend_from_slice(&sources_offset.to_le_bytes());
    comment.extend_from_slice(&sources_size.to_le_bytes());
    if let Some((o, s)) = index_loc {
        comment.extend_from_slice(&o.to_le_bytes());
        comment.extend_from_slice(&s.to_le_bytes());
    }
    let n16 = if n_entries >= 0xFFFF { 0xFFFFu16 } else { n_entries as u16 };
    let sz32 = if cd_size >= 0xFFFF_FFFF { 0xFFFF_FFFFu32 } else { cd_size as u32 };
    let of32 = if cd_offset >= 0xFFFF_FFFF { 0xFFFF_FFFFu32 } else { cd_offset as u32 };
    out.extend_from_slice(&0x06054b50u32.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&n16.to_le_bytes());
    out.extend_from_slice(&n16.to_le_bytes());
    out.extend_from_slice(&sz32.to_le_bytes());
    out.extend_from_slice(&of32.to_le_bytes());
    out.extend_from_slice(&(comment.len() as u16).to_le_bytes());
    out.extend_from_slice(&comment);
    Ok(out)
}
