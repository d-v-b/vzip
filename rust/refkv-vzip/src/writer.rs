//! The archive writer: python/src/refkv/vzip/archive.py's `VZipWriter`, byte for byte. The layout
//! (spec/bindings/vzip.md §14.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#142-recommended-layout-informative>):
//!
//! ```text
//! [local header + body]*   one per entry, STORED unless compressed; no local extra field, but
//!                          the 20-byte ZIP64 one of an entry of 4 GiB or more
//! [SourceTable]            the format entry __vz__/sources (DEFLATE)
//! [late entries]           so that a reader's opening request covers them
//! [CdIndex]                only with a page size: the page index over the central directory
//! [central directory]      sorted, the format entries' records last; a reference entry carries
//!                          its payload in extra field 0x7a76 (a Range) or 0x7a77 (a Concat)
//! [zip64 EOCD + locator]   always; the EOCD's counts, size and offset are all ones
//! [EOCD]                   comment "vzip/0", then the u64 offset and size of the source table's
//!                          body [and of the page index's]
//! ```
//!
//! Every entry has the fixed DOS date 1980-01-01, and DEFLATE bodies are zlib's at level 9, so
//! that the same input gives the same archive.

use crate::pb::{self, CdIndex, Page, Pinned, Range, Source, SourceKind};
use crate::{Error, CONCAT_EXTRA_ID, INDEX_KEY, RANGE_EXTRA_ID, RESERVED_PREFIX, SOURCES_KEY};
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::path::Path;

/// The largest reference payload (spec/bindings/vzip.md §11.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#113-reference-entries>): a
/// fixed limit, which leaves room for a ZIP64 block wherever the entry lands.
pub const MAX_PAYLOAD: usize = 65519;

/// The revision of spec/refkv.md the writer follows, recorded in every archive's source table
/// (spec/refkv.md §1.3 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#13-versioning>, §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>).
pub const SPEC_REVISION: u32 = 11;

const MAGIC_COMMENT: &[u8] = b"vzip/0";
const ZIP64_EXTRA_ID: u16 = 0x0001;
const SIG_LFH: u32 = 0x0403_4b50;
const SIG_CDH: u32 = 0x0201_4b50;
const SIG_EOCD: u32 = 0x0605_4b50;
const SIG_EOCD64: u32 = 0x0606_4b50;
const SIG_LOC64: u32 = 0x0706_4b50;
const LFH_SIZE: u64 = 30;
const U32: u64 = 0xffff_ffff;
const U16: u16 = 0xffff;
const FLAG_UTF8: u16 = 0x0800;
const DOS_DATE: u16 = (1 << 5) | 1; // 1980-01-01, for reproducible archives
const MADE_BY: u16 = 20; // spec/bindings/vzip.md §14.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#142-recommended-layout-informative>

/// Whether an entry needs ZIP64 sizes (spec/bindings/vzip.md §10.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7).
fn is_large(size: u64, csize: u64) -> bool {
    size >= U32 || csize >= U32
}

fn invalid<T>(m: impl Into<String>) -> Result<T, Error> {
    Err(Error::Invalid(m.into()))
}

/// Raw DEFLATE at level 9 (zlib's `compressobj(9, DEFLATED, -15)`).
pub fn deflate(data: &[u8]) -> Vec<u8> {
    let mut c = flate2::Compress::new(flate2::Compression::new(9), false);
    let mut out = Vec::with_capacity(data.len() / 2 + 64);
    loop {
        let st = c.compress_vec(&data[c.total_in() as usize..], &mut out, flate2::FlushCompress::Finish).expect("deflate");
        if st == flate2::Status::StreamEnd {
            return out;
        }
        out.reserve(out.capacity().max(1 << 10));
    }
}

/// A central directory record to write: name, local header offset, size, compressed size,
/// method, CRC-32 and extra field.
struct Rec {
    name: String,
    off: u64,
    size: u64,
    csize: u64,
    method: u16,
    crc: u32,
    extra: Vec<u8>,
}

/// A source of the table: one interned, or one of `add_url_refs`' (url, size pin).
enum Slot {
    Source(Source),
    Bulk(String, Option<u64>),
}

impl Slot {
    fn url(&self) -> Option<&str> {
        match self {
            Slot::Source(s) => s.url_str(),
            Slot::Bulk(u, _) => Some(u),
        }
    }
}

/// How the archive is written.
#[derive(Clone, Copy, Debug)]
pub struct Options {
    /// Each reference entry's body is its payload (spec/bindings/vzip.md §11.3 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#113-reference-entries>), so that a ZIP
    /// tool that ignores extra fields still sees it; else the bodies are empty.
    pub mirror_refs: bool,
    /// Write a page index with pages of about this many bytes of central directory.
    pub page_size: Option<u64>,
}

impl Default for Options {
    fn default() -> Options {
        Options { mirror_refs: true, page_size: None }
    }
}

/// Streams a vzip archive to `W`.
pub struct Writer<W: Write> {
    w: W,
    pos: u64,
    opts: Options,
    cd: Vec<Rec>,
    names: HashSet<String>,
    ref_names: HashSet<String>,
    /// the ranges of the sources that are not url sources, for the bounds checks of spec/refkv.md
    /// §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>
    range_checks: Vec<(String, Range)>,
    interned: HashMap<Source, u32>,
    sources: Vec<Slot>,
    late: Vec<(String, Vec<u8>, bool)>,
    late_keys: HashSet<String>,
}

impl<W: Write> Writer<W> {
    pub fn new(w: W, opts: Options) -> Writer<W> {
        Writer {
            w,
            pos: 0,
            opts,
            cd: Vec::new(),
            names: HashSet::new(),
            ref_names: HashSet::new(),
            range_checks: Vec::new(),
            interned: HashMap::new(),
            sources: Vec::new(),
            late: Vec::new(),
            late_keys: HashSet::new(),
        }
    }

    /// Interns `src` into the source table, and gives its index for `Range::source`: equal
    /// sources share one. Index 0 is free on the wire, so register the most used source first.
    pub fn source(&mut self, src: Source) -> Result<u32, Error> {
        if let Some(&i) = self.interned.get(&src) {
            return Ok(i);
        }
        src.check().map_err(Error::Invalid)?;
        let i = self.next_index()?;
        self.interned.insert(src.clone(), i);
        self.sources.push(Slot::Source(src));
        Ok(i)
    }

    fn next_index(&self) -> Result<u32, Error> {
        u32::try_from(self.sources.len()).or_else(|_| invalid("more than 2^32 sources"))
    }

    /// The source index of an external object (absolute, or relative to the archive).
    pub fn url(&mut self, url: &str) -> Result<u32, Error> {
        self.source(Source::url(url))
    }

    /// The source index of another entry of this archive (checked at `close`).
    pub fn internal(&mut self, key: &str) -> Result<u32, Error> {
        self.source(Source::key(key))
    }

    /// The source index of literal bytes stored in the table, such as a shared header.
    pub fn blob(&mut self, data: &[u8]) -> Result<u32, Error> {
        self.source(Source::data(data))
    }

    /// How many sources the table has so far.
    pub fn source_count(&self) -> usize {
        self.sources.len()
    }

    fn write(&mut self, b: &[u8]) -> Result<(), Error> {
        self.w.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }

    fn check_new_key(&self, name: &str) -> Result<(), Error> {
        if name.is_empty() {
            return invalid("empty key");
        }
        if name.len() > 0xffff {
            return invalid(format!("key of {} bytes: a ZIP file name has at most 65535", name.len()));
        }
        if self.names.contains(name) {
            return invalid(format!("duplicate key {name:?}"));
        }
        Ok(())
    }

    /// A local file header; a large entry's sizes go in a 20-byte ZIP64 extra field
    /// (spec/bindings/vzip.md §10.1 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rules 4 and 7).
    fn local_header(&mut self, name: &str, method: u16, crc: u32, size: u64, csize: u64) -> Result<(), Error> {
        let mut h = Vec::with_capacity(LFH_SIZE as usize + name.len() + 20);
        let large = is_large(size, csize);
        h.extend_from_slice(&SIG_LFH.to_le_bytes());
        h.extend_from_slice(&(if large { 45u16 } else { 20 }).to_le_bytes());
        h.extend_from_slice(&FLAG_UTF8.to_le_bytes());
        h.extend_from_slice(&method.to_le_bytes());
        h.extend_from_slice(&0u16.to_le_bytes());
        h.extend_from_slice(&DOS_DATE.to_le_bytes());
        h.extend_from_slice(&crc.to_le_bytes());
        let (c32, s32) = if large { (U32 as u32, U32 as u32) } else { (csize as u32, size as u32) };
        h.extend_from_slice(&c32.to_le_bytes());
        h.extend_from_slice(&s32.to_le_bytes());
        h.extend_from_slice(&(name.len() as u16).to_le_bytes());
        h.extend_from_slice(&(if large { 20u16 } else { 0 }).to_le_bytes());
        h.extend_from_slice(name.as_bytes());
        if large {
            h.extend_from_slice(&ZIP64_EXTRA_ID.to_le_bytes());
            h.extend_from_slice(&16u16.to_le_bytes());
            h.extend_from_slice(&size.to_le_bytes());
            h.extend_from_slice(&csize.to_le_bytes());
        }
        self.write(&h)
    }

    fn entry(&mut self, name: &str, body: &[u8], extra: Vec<u8>, compress: bool) -> Result<(), Error> {
        self.check_new_key(name)?;
        if compress && body.len() as u64 >= U32 {
            return invalid("entries of 4 GiB or more must be STORED (spec/bindings/vzip.md §10.1 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7)");
        }
        let crc = crc32fast::hash(body);
        let deflated;
        let (method, stored): (u16, &[u8]) = if compress {
            deflated = deflate(body);
            if deflated.len() as u64 >= U32 {
                return invalid("a DEFLATE body of 4 GiB or more would make a large entry that is not STORED");
            }
            (8, &deflated)
        } else {
            (0, body)
        };
        self.names.insert(name.to_string());
        let off = self.pos;
        self.local_header(name, method, crc, body.len() as u64, stored.len() as u64)?;
        self.write(stored)?;
        self.cd.push(Rec { name: name.into(), off, size: body.len() as u64, csize: stored.len() as u64, method, crc, extra });
        Ok(())
    }

    /// Adds the bytes of the file at `path` as a STORED entry, streaming it (twice: once for its
    /// CRC-32, once to copy it), so that an entry of any size never sits in memory.
    pub fn add_file(&mut self, key: &str, path: &Path) -> Result<(), Error> {
        if key.starts_with(RESERVED_PREFIX) {
            return invalid(format!("{RESERVED_PREFIX:?} is reserved"));
        }
        if self.late_keys.contains(key) {
            return invalid(format!("duplicate key {key:?}"));
        }
        self.check_new_key(key)?;
        let mut buf = vec![0u8; 1 << 24];
        let (mut crc, mut size) = (crc32fast::Hasher::new(), 0u64);
        let mut f = std::fs::File::open(path)?;
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            crc.update(&buf[..n]);
            size += n as u64;
        }
        let crc = crc.finalize();
        self.names.insert(key.to_string());
        let off = self.pos;
        self.local_header(key, 0, crc, size, size)?;
        let mut f = std::fs::File::open(path)?;
        let mut copied = 0u64;
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            self.write(&buf[..n])?;
            copied += n as u64;
        }
        if copied != size {
            return invalid(format!("{} changed while it was being added", path.display()));
        }
        self.cd.push(Rec { name: key.into(), off, size, csize: size, method: 0, crc, extra: Vec::new() });
        Ok(())
    }

    fn defer(&mut self, key: &str, data: &[u8], compress: bool) -> Result<(), Error> {
        if self.names.contains(key) || self.late_keys.contains(key) {
            return invalid(format!("duplicate key {key:?}"));
        }
        self.late_keys.insert(key.to_string());
        self.late.push((key.to_string(), data.to_vec(), compress));
        Ok(())
    }

    /// Adds a bytes entry, DEFLATE-compressed when `compress` (it is then read whole). A `late`
    /// entry is written just before the central directory, where a reader's opening request
    /// reads it (small metadata documents).
    pub fn add_bytes(&mut self, key: &str, data: &[u8], compress: bool, late: bool) -> Result<(), Error> {
        if key.starts_with(RESERVED_PREFIX) {
            return invalid(format!("{RESERVED_PREFIX:?} is reserved"));
        }
        if late { self.defer(key, data, compress) } else { self.entry(key, data, Vec::new(), compress) }
    }

    /// Adds an entry outside the Zarr key space, `__vz__/<name>`, and gives its key.
    pub fn add_hidden(&mut self, name: &str, data: &[u8], compress: bool, late: bool) -> Result<String, Error> {
        let key = format!("{RESERVED_PREFIX}{name}");
        if key == SOURCES_KEY || key == INDEX_KEY {
            return invalid(format!("{key:?} is written by the writer itself"));
        }
        if late {
            self.defer(&key, data, compress)?;
        } else {
            self.entry(&key, data, Vec::new(), compress)?;
        }
        Ok(key)
    }

    /// Adds a reference whose value is the concatenation of `ranges`: one range is a Range
    /// (extra field 0x7a76), several a Concat (0x7a77). Each range's source must be one the
    /// table has.
    pub fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), Error> {
        if key.starts_with(RESERVED_PREFIX) {
            return invalid(format!("{RESERVED_PREFIX:?} is reserved"));
        }
        for r in &ranges {
            if r.data.is_none() && r.source as usize >= self.sources.len() {
                return invalid(format!("range of {key:?} uses unregistered source {}", r.source));
            }
            if r.data.is_some() && r.crc32c.is_some() {
                return invalid(format!("literal range of {key:?} with a crc32c (spec/refkv.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>)"));
            }
            if r.offset.checked_add(r.length).is_none() {
                return invalid(format!("a range of {key:?}: offset + length exceeds 2^64 - 1"));
            }
        }
        // spec/refkv.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing> (which python/src/refkv/vzip/archive.py does not check)
        if ranges.iter().try_fold(0u64, |n, r| n.checked_add(r.size())).is_none() {
            return invalid(format!("the reference {key:?} exceeds 2^64 - 1 bytes"));
        }
        // url sources are not bounds-checked (spec/refkv.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>): only the others are kept
        for r in &ranges {
            if r.data.is_none() && self.sources[r.source as usize].url().is_none() {
                self.range_checks.push((key.to_string(), r.clone()));
            }
        }
        let (id, payload) = if ranges.len() == 1 { (RANGE_EXTRA_ID, ranges[0].encode()) } else { (CONCAT_EXTRA_ID, pb::encode_concat(&ranges)) };
        self.reference(key, id, payload)
    }

    fn reference(&mut self, key: &str, id: u16, payload: Vec<u8>) -> Result<(), Error> {
        if payload.len() > MAX_PAYLOAD {
            return invalid("reference payload too large for a zip extra field (spec/bindings/vzip.md §11.3 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#113-reference-entries>)");
        }
        let mut extra = Vec::with_capacity(4 + payload.len());
        extra.extend_from_slice(&id.to_le_bytes());
        extra.extend_from_slice(&(payload.len() as u16).to_le_bytes());
        extra.extend_from_slice(&payload);
        let body = if self.opts.mirror_refs { payload } else { Vec::new() };
        self.entry(key, &body, extra, false)?;
        self.ref_names.insert(key.to_string());
        Ok(())
    }

    /// The bulk path for many objects referenced whole: for each (key, url, size, CRC-32C), a new
    /// url source (not interned), and a reference entry `(source, 0, size)` carrying the CRC-32C
    /// when it is given. With `pin_size`, each source pins the object's size (spec/refkv.md §5.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#51-pins>). No interning and no
    /// bounds check per entry (url sources have none), so that millions of entries fit.
    pub fn add_url_refs(&mut self, items: impl IntoIterator<Item = (String, String, u64, Option<u32>)>, pin_size: bool) -> Result<(), Error> {
        for (key, url, size, crc) in items {
            if key.starts_with(RESERVED_PREFIX) {
                return invalid(format!("{RESERVED_PREFIX:?} is reserved"));
            }
            let i = self.next_index()?;
            self.sources.push(Slot::Bulk(url, if pin_size { Some(size) } else { None }));
            let r = Range { crc32c: crc, ..Range::of(i, 0, size) };
            self.reference(&key, RANGE_EXTRA_ID, r.encode())?;
        }
        Ok(())
    }

    /// The writer's requirements on the source table (spec/refkv.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>).
    fn check_sources(&self) -> Result<(), Error> {
        let bad: Vec<&str> = self
            .sources
            .iter()
            .filter_map(|s| match s {
                Slot::Bulk(u, _) if u.is_empty() || !crate::uri::is_uri_reference(u) => Some(u.as_str()),
                _ => None,
            })
            .take(5)
            .collect();
        if !bad.is_empty() {
            return invalid(format!("empty or invalid url sources: {bad:?}"));
        }
        let source = |i: usize| match &self.sources[i] {
            Slot::Source(s) => Some(s),
            Slot::Bulk(..) => None, // a url source
        };
        if self.sources.iter().any(|s| matches!(s, Slot::Source(Source { kind: SourceKind::Key(k), .. }) if k.is_empty())) {
            return invalid("empty key source");
        }
        // the late entries are written by now
        let sizes: HashMap<&str, u64> = self.cd.iter().map(|r| (r.name.as_str(), r.size)).collect();
        for (key, r) in &self.range_checks {
            let n = match source(r.source as usize).map(|s| &s.kind) {
                Some(SourceKind::Data(d)) => Some(d.len() as u64),
                Some(SourceKind::Key(k)) => sizes.get(k.as_str()).copied(),
                _ => None,
            };
            if let Some(n) = n
                && r.offset.checked_add(r.length).is_none_or(|e| e > n) {
                    return invalid(format!(
                        "{key:?}: range [{}, {}) is past the end of source {} ({n} bytes)",
                        r.offset,
                        r.offset as u128 + r.length as u128,
                        r.source
                    ));
                }
        }
        let keys = || {
            self.sources.iter().filter_map(|s| match s {
                Slot::Source(Source { kind: SourceKind::Key(k), .. }) => Some(k.as_str()),
                _ => None,
            })
        };
        let mut missing: Vec<&str> = keys().filter(|k| !self.names.contains(*k)).collect();
        missing.sort_unstable();
        if !missing.is_empty() {
            return invalid(format!("internal references to missing entries: {missing:?}"));
        }
        let mut format: Vec<&str> = keys().filter(|k| *k == SOURCES_KEY || *k == INDEX_KEY).collect();
        format.sort_unstable();
        if !format.is_empty() {
            return invalid(format!("key sources naming format entries: {format:?}"));
        }
        let bad: Vec<&str> = self
            .sources
            .iter()
            .filter_map(|s| match s {
                Slot::Source(s) => s.url_str().filter(|u| u.is_empty() || !crate::uri::is_uri_reference(u)),
                Slot::Bulk(..) => None,
            })
            .collect();
        if !bad.is_empty() {
            return invalid(format!("empty or invalid url sources: {bad:?}"));
        }
        let mut to_refs: Vec<&str> = keys().filter(|k| self.ref_names.contains(*k)).collect();
        to_refs.sort_unstable();
        if !to_refs.is_empty() {
            return invalid(format!("internal references to reference entries: {to_refs:?}"));
        }
        Ok(())
    }

    /// A central directory record. The ZIP64 block (spec/bindings/vzip.md §10.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>) holds the sizes of
    /// a large entry, then an offset that does not fit, in that order, and nothing else.
    fn cd_record(r: &Rec) -> Vec<u8> {
        let mut z64 = Vec::new();
        let (mut size32, mut csize32, mut off32) = (r.size as u32, r.csize as u32, r.off as u32);
        if is_large(r.size, r.csize) {
            z64.extend_from_slice(&r.size.to_le_bytes());
            z64.extend_from_slice(&r.csize.to_le_bytes());
            size32 = U32 as u32;
            csize32 = U32 as u32;
        }
        if r.off >= U32 {
            z64.extend_from_slice(&r.off.to_le_bytes());
            off32 = U32 as u32;
        }
        let mut extra = Vec::new();
        if !z64.is_empty() {
            extra.extend_from_slice(&ZIP64_EXTRA_ID.to_le_bytes());
            extra.extend_from_slice(&(z64.len() as u16).to_le_bytes());
            extra.extend_from_slice(&z64);
        }
        extra.extend_from_slice(&r.extra);
        let mut h = Vec::with_capacity(46 + r.name.len() + extra.len());
        h.extend_from_slice(&SIG_CDH.to_le_bytes());
        h.extend_from_slice(&MADE_BY.to_le_bytes());
        h.extend_from_slice(&(if z64.is_empty() { 20u16 } else { 45 }).to_le_bytes());
        h.extend_from_slice(&FLAG_UTF8.to_le_bytes());
        h.extend_from_slice(&r.method.to_le_bytes());
        h.extend_from_slice(&0u16.to_le_bytes());
        h.extend_from_slice(&DOS_DATE.to_le_bytes());
        h.extend_from_slice(&r.crc.to_le_bytes());
        h.extend_from_slice(&csize32.to_le_bytes());
        h.extend_from_slice(&size32.to_le_bytes());
        h.extend_from_slice(&(r.name.len() as u16).to_le_bytes());
        h.extend_from_slice(&(extra.len() as u16).to_le_bytes());
        h.extend_from_slice(&[0; 6]); // comment length, disk, internal attributes
        h.extend_from_slice(&0u32.to_le_bytes()); // external attributes
        h.extend_from_slice(&off32.to_le_bytes());
        h.extend_from_slice(r.name.as_bytes());
        h.extend_from_slice(&extra);
        h
    }

    /// The offset and compressed size of the body of the last entry written.
    fn last_body(&self) -> (u64, u64) {
        let r = self.cd.last().expect("an entry");
        (r.off + LFH_SIZE + r.name.len() as u64 + if is_large(r.size, r.csize) { 20 } else { 0 }, r.csize)
    }

    /// The length of a record's central directory record, which depends only on its name, extra
    /// field, and whether it is large or its offset needs ZIP64 (as [`Self::cd_record`] writes it).
    fn cd_record_len(r: &Rec) -> u64 {
        let z64 = if is_large(r.size, r.csize) { 16 } else { 0 } + if r.off >= U32 { 8 } else { 0 };
        46 + r.name.len() as u64 + r.extra.len() as u64 + if z64 > 0 { 4 + z64 } else { 0 }
    }

    /// The page index over the central directory's body records (every entry's but the format
    /// entries', `extra` included, in key order), encoded.
    fn encode_index(&self, page_size: u64, pinned: &[Pinned], extra: Option<&Rec>) -> Vec<u8> {
        let trailer = |n: &str| n == SOURCES_KEY || n == INDEX_KEY;
        let mut body: Vec<&Rec> = self.cd.iter().filter(|r| !trailer(&r.name)).chain(extra).collect();
        body.sort_unstable_by(|a, b| a.name.as_bytes().cmp(b.name.as_bytes()));
        let mut pages = Vec::new();
        let (mut first, mut start, mut pos) = (0usize, 0u64, 0u64);
        for (i, r) in body.iter().enumerate() {
            if pos - start >= page_size {
                pages.push(Page { first_key: body[first].name.clone(), offset: start, length: pos - start });
                first = i;
                start = pos;
            }
            pos += Self::cd_record_len(r);
        }
        if !body.is_empty() {
            pages.push(Page { first_key: body[first].name.clone(), offset: start, length: pos - start });
        }
        pb::encode_cd_index(&CdIndex { pages, pinned: pinned.to_vec() })
    }

    /// Writes the source table, the late entries, the page index, the central directory and the
    /// end records, and gives the output back.
    pub fn close(self) -> Result<W, Error> {
        self.finish(None)
    }

    /// As [`Self::close`], with one more hidden STORED entry, `__vz__/<name>`, written last before
    /// the central directory. Its body is what `build` gives for the archive's final layout: every
    /// other entry's body offset, and how many bytes will follow the body (the central directory
    /// and the end records), which a body such as a phkv container (spec/bindings/phkv.md §21
    /// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#21-hybrid-profile>) needs.
    /// `build` is called again if its body's size changes whether the entry needs ZIP64.
    pub fn close_with_trailer(self, name: &str, build: &mut dyn FnMut(&Layout) -> Result<Vec<u8>, Error>) -> Result<W, Error> {
        self.finish(Some((name, build)))
    }

    fn finish(mut self, trailer: Option<(&str, &mut dyn FnMut(&Layout) -> Result<Vec<u8>, Error>)>) -> Result<W, Error> {
        let table: Vec<Source> = self
            .sources
            .iter()
            .map(|s| match s {
                Slot::Source(s) => s.clone(),
                Slot::Bulk(u, size) => Source { size: *size, ..Source::url(u.as_str()) },
            })
            .collect();
        let table = pb::encode_source_table(&table, Some(SPEC_REVISION));
        self.entry(SOURCES_KEY, &table, Vec::new(), true)?;
        let mut comment = MAGIC_COMMENT.to_vec();
        let (o, n) = self.last_body();
        comment.extend_from_slice(&o.to_le_bytes());
        comment.extend_from_slice(&n.to_le_bytes());
        let mut pinned = Vec::new();
        for (key, data, compress) in std::mem::take(&mut self.late) {
            self.entry(&key, &data, Vec::new(), compress)?;
            let r = self.cd.last().unwrap();
            let (size, csize, method) = (r.size, r.csize, r.method);
            pinned.push(Pinned { key, data_offset: self.last_body().0, size, csize, method: method as u32 });
        }
        self.check_sources()?;

        let trailer_entry = match trailer {
            None => None,
            Some((name, build)) => {
                let key = format!("{RESERVED_PREFIX}{name}");
                if key == SOURCES_KEY || key == INDEX_KEY {
                    return invalid(format!("{key:?} is written by the writer itself"));
                }
                self.check_new_key(&key)?;
                let body = self.trailer_body(&key, &table, &pinned, build)?;
                Some((key, body))
            }
        };

        if let Some(page_size) = self.opts.page_size {
            // the trailer's record as it will be: its offset is where its body was built to start
            let extra = trailer_entry.as_ref().map(|(key, (body, body_offset, _))| {
                let len = body.len() as u64;
                let lfh = LFH_SIZE + key.len() as u64 + if is_large(len, len) { 20 } else { 0 };
                Rec { off: body_offset - lfh, ..self.trailer_rec(key, len) }
            });
            let index = self.encode_index(page_size, &pinned, extra.as_ref());
            self.entry(INDEX_KEY, &index, Vec::new(), true)?;
            let (o, n) = self.last_body();
            comment.extend_from_slice(&o.to_le_bytes());
            comment.extend_from_slice(&n.to_le_bytes());
        }
        let expected_end = match trailer_entry {
            Some((key, body)) => {
                self.entry(&key, &body.0, Vec::new(), false)?;
                if self.last_body().0 != body.1 {
                    return invalid(format!("{key:?}: its body is at {}, not where it was built for", self.last_body().0));
                }
                Some(self.pos + body.2)
            }
            None => None,
        };

        let trailer = |n: &str| n == SOURCES_KEY || n == INDEX_KEY;
        let mut body: Vec<usize> = (0..self.cd.len()).filter(|&i| !trailer(&self.cd[i].name)).collect();
        body.sort_unstable_by(|&a, &b| self.cd[a].name.as_bytes().cmp(self.cd[b].name.as_bytes()));
        let mut records: Vec<Vec<u8>> = body.iter().map(|&i| Self::cd_record(&self.cd[i])).collect();
        records.extend(self.cd.iter().filter(|r| trailer(&r.name)).map(Self::cd_record));

        let cd_start = self.pos;
        for rec in &records {
            self.write(rec)?;
        }
        let cd_size = self.pos - cd_start;
        let n = self.cd.len() as u64;
        // spec/bindings/vzip.md §10.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>: the zip64 end records are written in every archive, and the
        // end of central directory record's counts, size and offset are always all ones
        let eocd64_off = self.pos;
        let mut t = Vec::with_capacity(56 + 20 + 22 + comment.len());
        t.extend_from_slice(&SIG_EOCD64.to_le_bytes());
        t.extend_from_slice(&44u64.to_le_bytes());
        t.extend_from_slice(&MADE_BY.to_le_bytes());
        t.extend_from_slice(&45u16.to_le_bytes());
        t.extend_from_slice(&[0; 8]); // this disk, the directory's disk
        t.extend_from_slice(&n.to_le_bytes());
        t.extend_from_slice(&n.to_le_bytes());
        t.extend_from_slice(&cd_size.to_le_bytes());
        t.extend_from_slice(&cd_start.to_le_bytes());
        t.extend_from_slice(&SIG_LOC64.to_le_bytes());
        t.extend_from_slice(&0u32.to_le_bytes());
        t.extend_from_slice(&eocd64_off.to_le_bytes());
        t.extend_from_slice(&1u32.to_le_bytes());
        t.extend_from_slice(&SIG_EOCD.to_le_bytes());
        t.extend_from_slice(&[0; 4]);
        t.extend_from_slice(&U16.to_le_bytes());
        t.extend_from_slice(&U16.to_le_bytes());
        t.extend_from_slice(&(U32 as u32).to_le_bytes());
        t.extend_from_slice(&(U32 as u32).to_le_bytes());
        t.extend_from_slice(&(comment.len() as u16).to_le_bytes());
        t.extend_from_slice(&comment);
        self.write(&t)?;
        if let Some(e) = expected_end
            && e != self.pos
        {
            return invalid(format!("the archive ends at {}, not at {e} as the trailer was built for", self.pos));
        }
        self.w.flush()?;
        Ok(self.w)
    }

    /// The central directory record a trailer entry of `len` bytes will have, written next.
    fn trailer_rec(&self, key: &str, len: u64) -> Rec {
        Rec { name: key.into(), off: 0, size: len, csize: len, method: 0, crc: 0, extra: Vec::new() }
    }

    /// The trailer's body, built for the layout it will have: (body, its offset, the bytes after
    /// it). Its central directory record's length depends on whether the entry is large and whether
    /// its offset needs ZIP64, which the page index before it depends on in turn: each is assumed,
    /// and checked, until the assumptions hold.
    fn trailer_body(&self, key: &str, table: &[u8], pinned: &[Pinned], build: &mut dyn FnMut(&Layout) -> Result<Vec<u8>, Error>) -> Result<(Vec<u8>, u64, u64), Error> {
        let sources_stored = deflate(table);
        let (mut large, mut far) = (false, false);
        for _ in 0..4 {
            let mut placeholder = self.trailer_rec(key, if large { U32 } else { 0 });
            placeholder.off = if far { U32 } else { 0 };
            let mut at = self.pos;
            let mut entries: Vec<EntryLayout> = self.cd.iter().map(layout_of).collect();
            let mut index_rec = None;
            if let Some(page_size) = self.opts.page_size {
                let index = self.encode_index(page_size, pinned, Some(&placeholder));
                let csize = deflate(&index).len() as u64;
                let rec = Rec { name: INDEX_KEY.into(), off: at, size: index.len() as u64, csize, method: 8, crc: 0, extra: Vec::new() };
                entries.push(layout_of(&rec));
                at += LFH_SIZE + INDEX_KEY.len() as u64 + csize;
                index_rec = Some(rec);
            }
            if (at >= U32) != far {
                far = !far;
                continue;
            }
            placeholder.off = at;
            let body_offset = at + LFH_SIZE + key.len() as u64 + if large { 20 } else { 0 };
            let cd_size: u64 = self.cd.iter().chain(index_rec.as_ref()).chain(std::iter::once(&placeholder)).map(Self::cd_record_len).sum();
            let comment_len = (MAGIC_COMMENT.len() + if self.opts.page_size.is_some() { 32 } else { 16 }) as u64;
            let tail_len = cd_size + 56 + 20 + 22 + comment_len;
            let body = build(&Layout { entries: &entries, sources_stored: &sources_stored, body_offset, tail_len })?;
            if is_large(body.len() as u64, body.len() as u64) == large {
                return Ok((body, body_offset, tail_len));
            }
            large = !large;
        }
        invalid(format!("{key:?}: its body's size keeps changing whether it needs ZIP64"))
    }
}

/// An entry of the archive, as the builder of [`Writer::close_with_trailer`] sees it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EntryLayout {
    pub name: String,
    /// the offset of its body
    pub body_offset: u64,
    /// its uncompressed and compressed sizes
    pub size: u64,
    pub csize: u64,
    /// 0 (STORED) or 8 (DEFLATE)
    pub method: u16,
    /// a reference entry's extra field header ID (0x7a76 or 0x7a77) and payload
    pub payload: Option<(u16, Vec<u8>)>,
}

/// What the builder of [`Writer::close_with_trailer`] is given.
pub struct Layout<'a> {
    /// every entry before the trailer, the format entries included, in the order they are written
    pub entries: &'a [EntryLayout],
    /// the body of `__vz__/sources`: the source table, DEFLATE-compressed
    pub sources_stored: &'a [u8],
    /// where the trailer's body starts
    pub body_offset: u64,
    /// the bytes that follow the trailer's body: the central directory and the end records
    pub tail_len: u64,
}

fn layout_of(r: &Rec) -> EntryLayout {
    let body_offset = r.off + LFH_SIZE + r.name.len() as u64 + if is_large(r.size, r.csize) { 20 } else { 0 };
    let payload = (r.extra.len() >= 4).then(|| {
        let id = u16::from_le_bytes([r.extra[0], r.extra[1]]);
        let n = u16::from_le_bytes([r.extra[2], r.extra[3]]) as usize;
        (id, r.extra[4..4 + n].to_vec())
    });
    EntryLayout { name: r.name.clone(), body_offset, size: r.size, csize: r.csize, method: r.method, payload }
}
