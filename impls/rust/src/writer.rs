//! vzip writer (spec §3, §4, §7, §9).

use crate::proto::{self, CdIndex, Concat, Page, Pinned, Range, Source, SourceKind, SourceTable};
use crate::{is_strong_etag, uri, INDEX_KEY, SOURCES_KEY};
use std::io::Write;

#[derive(Debug, Clone)]
pub enum RangeSpec {
    Source { source: u64, offset: u64, length: u64 },
    Literal(Vec<u8>),
}

#[derive(Debug, Clone)]
pub enum EntryValue {
    Bytes { data: Vec<u8>, compress: bool },
    Ranges(Vec<RangeSpec>),
}

#[derive(Debug, Clone)]
pub struct EntrySpec {
    pub key: String,
    pub value: EntryValue,
    pub pinned: bool,
}

#[derive(Debug, Clone)]
pub struct ArchiveSpec {
    /// `None`: no page index. `Some(n)`: page index with pages of about n bytes.
    pub page_size: Option<u64>,
    /// Reference bodies mirror their payload (true) or are empty (false).
    pub mirror: bool,
    pub sources: Vec<Source>,
    pub entries: Vec<EntrySpec>,
}

const DOS_TIME: u16 = 0;
const DOS_DATE: u16 = (1 << 5) | 1; // 1980-01-01
const FLAGS: u16 = 0x0800; // UTF-8 names
const MAX_PAYLOAD: usize = 65519;

fn deflate(data: &[u8]) -> Vec<u8> {
    let mut e = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

fn crc32(data: &[u8]) -> u32 {
    let mut h = crc32fast::Hasher::new();
    h.update(data);
    h.finalize()
}

struct Written {
    key: String,
    method: u16,
    crc: u32,
    csize: u64,
    usize_: u64,
    lho: u64,
    ref_block: Option<(u16, Vec<u8>)>,
}

impl Written {
    fn body_offset(&self) -> u64 {
        self.lho + 30 + self.key.len() as u64
    }

    fn cd_record(&self) -> Vec<u8> {
        let mut extra = Vec::new();
        let zip64 = self.lho >= 0xFFFF_FFFF;
        if zip64 {
            extra.extend_from_slice(&0x0001u16.to_le_bytes());
            extra.extend_from_slice(&8u16.to_le_bytes());
            extra.extend_from_slice(&self.lho.to_le_bytes());
        }
        if let Some((id, p)) = &self.ref_block {
            extra.extend_from_slice(&id.to_le_bytes());
            extra.extend_from_slice(&(p.len() as u16).to_le_bytes());
            extra.extend_from_slice(p);
        }
        let mut r = Vec::with_capacity(46 + self.key.len() + extra.len());
        r.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        r.extend_from_slice(&20u16.to_le_bytes()); // version made by
        r.extend_from_slice(&(if zip64 { 45u16 } else { 20u16 }).to_le_bytes());
        r.extend_from_slice(&FLAGS.to_le_bytes());
        r.extend_from_slice(&self.method.to_le_bytes());
        r.extend_from_slice(&DOS_TIME.to_le_bytes());
        r.extend_from_slice(&DOS_DATE.to_le_bytes());
        r.extend_from_slice(&self.crc.to_le_bytes());
        r.extend_from_slice(&(self.csize as u32).to_le_bytes());
        r.extend_from_slice(&(self.usize_ as u32).to_le_bytes());
        r.extend_from_slice(&(self.key.len() as u16).to_le_bytes());
        r.extend_from_slice(&(extra.len() as u16).to_le_bytes());
        r.extend_from_slice(&0u16.to_le_bytes()); // comment length
        r.extend_from_slice(&0u16.to_le_bytes()); // disk number start
        r.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
        r.extend_from_slice(&0u32.to_le_bytes()); // external attrs
        r.extend_from_slice(&(if zip64 { 0xFFFF_FFFFu32 } else { self.lho as u32 }).to_le_bytes());
        r.extend_from_slice(self.key.as_bytes());
        r.extend_from_slice(&extra);
        r
    }
}

struct Out {
    buf: Vec<u8>,
    written: Vec<Written>,
}

impl Out {
    fn add(&mut self, key: &str, method: u16, uncompressed: &[u8], body: &[u8], ref_block: Option<(u16, Vec<u8>)>) -> usize {
        let lho = self.buf.len() as u64;
        let crc = crc32(uncompressed);
        let b = &mut self.buf;
        b.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        b.extend_from_slice(&20u16.to_le_bytes());
        b.extend_from_slice(&FLAGS.to_le_bytes());
        b.extend_from_slice(&method.to_le_bytes());
        b.extend_from_slice(&DOS_TIME.to_le_bytes());
        b.extend_from_slice(&DOS_DATE.to_le_bytes());
        b.extend_from_slice(&crc.to_le_bytes());
        b.extend_from_slice(&(body.len() as u32).to_le_bytes());
        b.extend_from_slice(&(uncompressed.len() as u32).to_le_bytes());
        b.extend_from_slice(&(key.len() as u16).to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(key.as_bytes());
        b.extend_from_slice(body);
        self.written.push(Written {
            key: key.to_string(),
            method,
            crc,
            csize: body.len() as u64,
            usize_: uncompressed.len() as u64,
            lho,
            ref_block,
        });
        self.written.len() - 1
    }
}

fn encode_reference(ranges: &[RangeSpec]) -> (u16, Vec<u8>) {
    let conv = |r: &RangeSpec| match r {
        RangeSpec::Source { source, offset, length } => Range {
            source: *source as u32,
            offset: *offset,
            length: *length,
            data: None,
        },
        RangeSpec::Literal(d) => Range { data: Some(d.clone()), ..Default::default() },
    };
    if ranges.len() == 1 {
        (0x7A76, proto::encode_range(&conv(&ranges[0])))
    } else {
        (0x7A77, proto::encode_concat(&Concat { parts: ranges.iter().map(conv).collect() }))
    }
}

/// Validates the input (§9.1) and produces the archive bytes.
pub fn write_archive(spec: &ArchiveSpec) -> Result<Vec<u8>, String> {
    // ---- validation -----------------------------------------------------
    if spec.page_size == Some(0) {
        return Err("page_size must be at least 1".into());
    }
    let mut keys = std::collections::HashMap::new();
    for (i, e) in spec.entries.iter().enumerate() {
        if e.key.is_empty() {
            return Err(format!("entry {}: empty key", i));
        }
        if e.key == SOURCES_KEY || e.key == INDEX_KEY {
            return Err(format!("entry {}: key {:?} is reserved for a format entry", i, e.key));
        }
        if e.key.len() > 65535 {
            return Err(format!("entry {}: key longer than 65535 bytes", i));
        }
        if keys.insert(e.key.clone(), i).is_some() {
            return Err(format!("entry {}: duplicate key {:?}", i, e.key));
        }
        if e.pinned {
            if spec.page_size.is_none() {
                return Err(format!("entry {}: pinned requires a page index", i));
            }
            if !matches!(e.value, EntryValue::Bytes { .. }) {
                return Err(format!("entry {}: only bytes entries can be pinned", i));
            }
        }
    }
    for (i, s) in spec.sources.iter().enumerate() {
        let has_pin = s.size.is_some() || s.etag.is_some() || s.modified_not_after.is_some();
        match &s.kind {
            None => return Err(format!("source {}: no kind", i)),
            Some(SourceKind::Url(u)) => {
                if u.is_empty() {
                    return Err(format!("source {}: empty url", i));
                }
                if uri::parse_uri_reference(u).is_none() {
                    return Err(format!("source {}: url {:?} is not an RFC 3986 URI-reference", i, u));
                }
                if let Some(e) = &s.etag {
                    if !is_strong_etag(e) {
                        return Err(format!("source {}: etag {:?} is not a strong entity tag", i, e));
                    }
                }
            }
            Some(SourceKind::Key(k)) => {
                if has_pin {
                    return Err(format!("source {}: pins are only allowed on url sources", i));
                }
                if k == SOURCES_KEY || k == INDEX_KEY {
                    return Err(format!("source {}: key source names a format entry", i));
                }
                match keys.get(k) {
                    None => return Err(format!("source {}: key {:?} is absent", i, k)),
                    Some(&j) => {
                        if !matches!(spec.entries[j].value, EntryValue::Bytes { .. }) {
                            return Err(format!("source {}: key {:?} is a reference entry", i, k));
                        }
                    }
                }
            }
            Some(SourceKind::Data(_)) => {
                if has_pin {
                    return Err(format!("source {}: pins are only allowed on url sources", i));
                }
            }
        }
    }

    // ---- bodies -----------------------------------------------------------
    struct Prepared<'a> {
        key: &'a str,
        method: u16,
        uncompressed: Vec<u8>,
        body: Vec<u8>,
        ref_block: Option<(u16, Vec<u8>)>,
        pinned: bool,
    }
    let mut prepared = Vec::new();
    for (i, e) in spec.entries.iter().enumerate() {
        match &e.value {
            EntryValue::Bytes { data, compress } => {
                if data.len() as u64 >= 0xFFFF_FFFF {
                    return Err(format!("entry {}: uncompressed size too large", i));
                }
                let (method, body) = if *compress { (8, deflate(data)) } else { (0, data.clone()) };
                if body.len() as u64 >= 0xFFFF_FFFF {
                    return Err(format!("entry {}: compressed size too large", i));
                }
                prepared.push(Prepared {
                    key: &e.key,
                    method,
                    uncompressed: data.clone(),
                    body,
                    ref_block: None,
                    pinned: e.pinned,
                });
            }
            EntryValue::Ranges(ranges) => {
                let mut total: u128 = 0;
                for (k, r) in ranges.iter().enumerate() {
                    match r {
                        RangeSpec::Source { source, offset, length } => {
                            if *source >= spec.sources.len() as u64 {
                                return Err(format!(
                                    "entry {} range {}: source {} out of range ({} sources)",
                                    i,
                                    k,
                                    source,
                                    spec.sources.len()
                                ));
                            }
                            if *offset as u128 + *length as u128 > u64::MAX as u128 {
                                return Err(format!("entry {} range {}: offset + length exceeds 2^64-1", i, k));
                            }
                            total += *length as u128;
                        }
                        RangeSpec::Literal(d) => total += d.len() as u128,
                    }
                }
                if total > u64::MAX as u128 {
                    return Err(format!("entry {}: reference size exceeds 2^64-1", i));
                }
                let (id, payload) = encode_reference(ranges);
                if payload.len() > MAX_PAYLOAD {
                    return Err(format!("entry {}: reference payload is {} bytes (max 65519)", i, payload.len()));
                }
                let body = if spec.mirror { payload.clone() } else { Vec::new() };
                prepared.push(Prepared {
                    key: &e.key,
                    method: 0,
                    uncompressed: body.clone(),
                    body,
                    ref_block: Some((id, payload)),
                    pinned: false,
                });
            }
        }
    }

    // ---- layout (§9.2) ------------------------------------------------------
    let mut out = Out { buf: Vec::new(), written: Vec::new() };
    let mut body_idx = Vec::new();
    for p in prepared.iter().filter(|p| !p.pinned) {
        body_idx.push(out.add(p.key, p.method, &p.uncompressed, &p.body, p.ref_block.clone()));
    }
    let table = SourceTable { sources: spec.sources.clone() };
    let table_raw = proto::encode_source_table(&table);
    let sources_idx = out.add(SOURCES_KEY, 8, &table_raw, &deflate(&table_raw), None);
    let mut pinned_idx = Vec::new();
    for p in prepared.iter().filter(|p| p.pinned) {
        let i = out.add(p.key, p.method, &p.uncompressed, &p.body, None);
        pinned_idx.push(i);
        body_idx.push(i);
    }

    // Body records, sorted in UTF-8 order.
    body_idx.sort_by(|&a, &b| out.written[a].key.as_bytes().cmp(out.written[b].key.as_bytes()));
    let mut cd = Vec::new();
    let mut pages: Vec<Page> = Vec::new();
    for &i in &body_idx {
        let rec = out.written[i].cd_record();
        if let Some(ps) = spec.page_size {
            let start = cd.len() as u64;
            match pages.last_mut() {
                Some(p) if p.length + rec.len() as u64 <= ps => p.length += rec.len() as u64,
                _ => pages.push(Page {
                    first_key: out.written[i].key.clone(),
                    offset: start,
                    length: rec.len() as u64,
                }),
            }
        }
        cd.extend_from_slice(&rec);
    }

    let mut index_idx = None;
    if spec.page_size.is_some() {
        let ix = CdIndex {
            pages,
            pinned: pinned_idx
                .iter()
                .map(|&i| {
                    let w = &out.written[i];
                    Pinned {
                        key: w.key.clone(),
                        data_offset: w.body_offset(),
                        size: w.usize_,
                        csize: w.csize,
                        method: w.method as u32,
                    }
                })
                .collect(),
        };
        let raw = proto::encode_cd_index(&ix);
        index_idx = Some(out.add(INDEX_KEY, 8, &raw, &deflate(&raw), None));
    }
    cd.extend_from_slice(&out.written[sources_idx].cd_record());
    if let Some(i) = index_idx {
        cd.extend_from_slice(&out.written[i].cd_record());
    }

    let n_entries = out.written.len() as u64;
    let cd_offset = out.buf.len() as u64;
    let cd_size = cd.len() as u64;
    out.buf.extend_from_slice(&cd);

    let zip64 = n_entries >= 0xFFFF || cd_size >= 0xFFFF_FFFF || cd_offset >= 0xFFFF_FFFF;
    if zip64 {
        let z_off = out.buf.len() as u64;
        let b = &mut out.buf;
        b.extend_from_slice(&0x0606_4b50u32.to_le_bytes());
        b.extend_from_slice(&44u64.to_le_bytes());
        b.extend_from_slice(&45u16.to_le_bytes()); // version made by
        b.extend_from_slice(&45u16.to_le_bytes()); // version needed
        b.extend_from_slice(&0u32.to_le_bytes());
        b.extend_from_slice(&0u32.to_le_bytes());
        b.extend_from_slice(&n_entries.to_le_bytes());
        b.extend_from_slice(&n_entries.to_le_bytes());
        b.extend_from_slice(&cd_size.to_le_bytes());
        b.extend_from_slice(&cd_offset.to_le_bytes());
        b.extend_from_slice(&0x0706_4b50u32.to_le_bytes());
        b.extend_from_slice(&0u32.to_le_bytes());
        b.extend_from_slice(&z_off.to_le_bytes());
        b.extend_from_slice(&1u32.to_le_bytes());
    }

    let mut comment = Vec::new();
    comment.extend_from_slice(b"vzip/0");
    let sw = &out.written[sources_idx];
    comment.extend_from_slice(&sw.body_offset().to_le_bytes());
    comment.extend_from_slice(&sw.csize.to_le_bytes());
    if let Some(i) = index_idx {
        let iw = &out.written[i];
        comment.extend_from_slice(&iw.body_offset().to_le_bytes());
        comment.extend_from_slice(&iw.csize.to_le_bytes());
    }

    let n16 = if n_entries >= 0xFFFF { 0xFFFF } else { n_entries as u16 };
    let b = &mut out.buf;
    b.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    b.extend_from_slice(&0u16.to_le_bytes());
    b.extend_from_slice(&0u16.to_le_bytes());
    b.extend_from_slice(&n16.to_le_bytes());
    b.extend_from_slice(&n16.to_le_bytes());
    b.extend_from_slice(&(cd_size.min(0xFFFF_FFFF) as u32).to_le_bytes());
    b.extend_from_slice(&(cd_offset.min(0xFFFF_FFFF) as u32).to_le_bytes());
    b.extend_from_slice(&(comment.len() as u16).to_le_bytes());
    b.extend_from_slice(&comment);
    Ok(out.buf)
}
