//! The reader (spec/bindings/phkv.md §22
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#22-reading-a-phkv-store>):
//! opening, the operations of spec/refkv.md §6.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>, and the binding's own
//! `entry_info` and `property`. It reads through [`ReadAt`], so the same code serves files,
//! memory, and ranged HTTP. `url` sources are read through a [`Resolver`], which the application
//! gives: it applies the reader policy and checks pins (spec/refkv.md §6.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>).

use crate::format::{self, section, Codec, DirEntry, Page, Parsed, Prop, Record, Stored, Superblock, Value};
use crate::ptrhash::{self, Params};
use refkv_vzip::pb::{self, Range, Source, SourceKind};
use std::io;
use std::sync::OnceLock;

/// Random access to the bytes of a store.
pub trait ReadAt {
    fn size(&self) -> io::Result<u64>;
    /// Fills `buf` from `offset`; fails if the bytes are not all there.
    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()>;
}

impl ReadAt for [u8] {
    fn size(&self) -> io::Result<u64> {
        Ok(self.len() as u64)
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        let start = usize::try_from(offset).map_err(|_| io::ErrorKind::UnexpectedEof)?;
        let end = start.checked_add(buf.len()).filter(|&e| e <= self.len()).ok_or(io::ErrorKind::UnexpectedEof)?;
        buf.copy_from_slice(&self[start..end]);
        Ok(())
    }
}

impl ReadAt for Vec<u8> {
    fn size(&self) -> io::Result<u64> {
        self.as_slice().size()
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        self.as_slice().read_at(offset, buf)
    }
}

impl ReadAt for std::fs::File {
    fn size(&self) -> io::Result<u64> {
        Ok(self.metadata()?.len())
    }

    #[cfg(unix)]
    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        std::os::unix::fs::FileExt::read_exact_at(self, buf, offset)
    }

    #[cfg(windows)]
    fn read_at(&self, mut offset: u64, mut buf: &mut [u8]) -> io::Result<()> {
        while !buf.is_empty() {
            match std::os::windows::fs::FileExt::seek_read(self, buf, offset)? {
                0 => return Err(io::ErrorKind::UnexpectedEof.into()),
                n => {
                    buf = &mut buf[n..];
                    offset += n as u64;
                }
            }
        }
        Ok(())
    }
}

/// Reads `url` sources, under the application's policy, checking their pins (spec/refkv.md §5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#51-pins>). An `Err` is a resolution
/// error.
pub trait Resolver {
    /// Bytes `[offset, offset + length)` of the object `source` names.
    fn read(&self, source: &Source, offset: u64, length: u64) -> Result<Vec<u8>, String>;
}

/// A resolver that refuses every `url` source.
pub struct NoUrls;

impl Resolver for NoUrls {
    fn read(&self, source: &Source, _: u64, _: u64) -> Result<Vec<u8>, String> {
        Err(format!("url sources are not resolved: {:?}", source.url_str().unwrap_or("")))
    }
}

/// The classes of error of spec/refkv.md §6.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>, and two of this reader's:
/// a property that cannot be read, and an I/O failure after open.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    Archive,
    Entry,
    Body,
    Payload,
    Resolution,
    Request,
    Property,
    Io,
}

#[derive(Debug)]
pub struct ReadError {
    pub class: Class,
    pub message: String,
}

impl std::fmt::Display for ReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?} error: {}", self.class, self.message)
    }
}

impl std::error::Error for ReadError {}

fn err<T>(class: Class, m: impl Into<String>) -> Result<T, ReadError> {
    Err(ReadError { class, message: m.into() })
}

fn io_err(e: io::Error) -> ReadError {
    ReadError { class: Class::Io, message: e.to_string() }
}

/// The resource limits of spec/refkv.md §6.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>, and the bound on
/// the memory of one request (spec/refkv.md §8
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#8-security-considerations>).
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// sources per store (rule 4)
    pub sources: usize,
    /// reads of url sources per get (rule 5)
    pub url_reads: usize,
    /// each section read at open, stored and decoded (rule 6)
    pub open_bytes: u64,
    /// the bytes one get or raw may hold in memory, a value decoded whole included
    pub request_bytes: u64,
    /// the bytes read at a record's offset at first
    pub record_read: u64,
}

impl Default for Limits {
    fn default() -> Limits {
        Limits { sources: 1 << 22, url_reads: 1024, open_bytes: 256 << 20, request_bytes: 1 << 32, record_read: 512 }
    }
}

/// A request of `get` (spec/refkv.md §6.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Request {
    Whole,
    Range(u64, u64),
    Offset(u64),
    Suffix(u64),
}

impl Request {
    /// The window `[a, b)` of a value of `n` bytes.
    fn window(self, n: u64) -> (u64, u64) {
        match self {
            Request::Whole => (0, n),
            Request::Range(s, e) => (s.min(n), e.min(n)),
            Request::Offset(s) => (s.min(n), n),
            Request::Suffix(c) => (n.saturating_sub(c), n),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Bytes,
    Reference,
}

/// What [`Store::entry_info`] reports.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EntryInfo {
    Bytes { codec: Codec, stored_length: u64, size: u64, crc32c: Option<u32>, hidden: bool },
    Reference { payload_length: u64, hidden: bool },
}

/// A PROPERTY section's header, read when a property is first asked for.
#[derive(Clone, Debug)]
struct PropertySection {
    name: String,
    type_code: u8,
    entry: DirEntry,
    present: u64,
    values: u64,
}

/// An opened phkv store.
pub struct Store<R: ReadAt> {
    r: R,
    file_size: u64,
    sb: Superblock,
    limits: Limits,
    sources: Vec<Source>,
    revision: Option<u32>,
    metadata: Vec<(String, Vec<u8>)>,
    /// `[start, end)` of RECORDS, the SLOTS section's offset, the perfect hash and the pages
    records: (u64, u64),
    slots: u64,
    hash: Option<(Params, Vec<u8>)>,
    pages: Vec<Page>,
    property_entries: Vec<DirEntry>,
    properties: OnceLock<Result<Vec<PropertySection>, String>>,
}

/// How many bytes of the end of the file a reader reads first.
const TAIL: u64 = (64 << 10) + format::SUPERBLOCK_SIZE;

impl<R: ReadAt> Store<R> {
    pub fn open(r: R) -> Result<Store<R>, ReadError> {
        Store::open_with(r, Limits::default())
    }

    /// Opens a store (spec/bindings/phkv.md §22.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#221-opening>): every
    /// failure is an archive error.
    pub fn open_with(r: R, limits: Limits) -> Result<Store<R>, ReadError> {
        let archive = |m: String| ReadError { class: Class::Archive, message: m };
        let file_size = r.size().map_err(|e| archive(e.to_string()))?;
        let tail_len = file_size.min(TAIL);
        let tail_at = file_size - tail_len;
        let mut tail = vec![0u8; tail_len as usize];
        r.read_at(tail_at, &mut tail).map_err(|e| archive(e.to_string()))?;
        // a read of [off, off + len), from the tail when it holds it
        let read = |off: u64, len: u64| -> Result<Vec<u8>, ReadError> {
            if off.checked_add(len).is_none_or(|e| e > file_size) {
                return Err(archive(format!("[{off}, {off} + {len}) lies outside the file")));
            }
            if off >= tail_at {
                let s = (off - tail_at) as usize;
                return Ok(tail[s..s + len as usize].to_vec());
            }
            let mut b = vec![0u8; usize::try_from(len).map_err(|_| archive("too large".into()))?];
            r.read_at(off, &mut b).map_err(|e| archive(e.to_string()))?;
            Ok(b)
        };

        // the superblock (spec/bindings/phkv.md §17.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#172-finding-the-superblock>)
        let mut found = None;
        for d in format::probe_distances() {
            if d + format::SUPERBLOCK_SIZE > file_size {
                break;
            }
            let b = read(file_size - d - format::SUPERBLOCK_SIZE, format::SUPERBLOCK_SIZE)?;
            if let Some(sb) = Superblock::matching(&b, d) {
                found = Some(sb);
                break;
            }
        }
        let Some(sb) = found else {
            return err(Class::Archive, "not a phkv store: no superblock at any probe distance");
        };
        if sb.version != format::VERSION {
            return err(Class::Archive, format!("unsupported format version {}", sb.version));
        }
        if sb.flags & !format::FLAG_HYBRID != 0 {
            return err(Class::Archive, format!("unsupported superblock flags {:#x}", sb.flags));
        }
        let n = sb.key_count;

        // the directory (spec/bindings/phkv.md §17.3 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#173-section-directory>)
        if sb.directory_length > limits.open_bytes {
            return err(Class::Archive, "the section directory exceeds the limit for data read at open");
        }
        let dir = read(sb.directory_offset, sb.directory_length)?;
        if format::crc32c(&dir) != sb.directory_crc32c {
            return err(Class::Archive, "the section directory does not match its CRC-32C");
        }
        let dir = DirEntry::decode_all(&dir).map_err(archive)?;

        // the sections, by type, count and codec (spec/bindings/phkv.md §17.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#174-sections>)
        let mut one: [Option<DirEntry>; 8] = [None; 8];
        let mut property_entries = Vec::new();
        for e in &dir {
            let known = (section::SOURCES..=section::METADATA).contains(&e.kind);
            if !known {
                if e.flags & format::FLAG_MUST_UNDERSTAND != 0 {
                    return err(Class::Archive, format!("unknown section type {:#x} must be understood", e.kind));
                }
                continue;
            }
            let codec = Codec::from_u8(e.codec).ok_or_else(|| archive(format!("section type {} has unknown codec {}", e.kind, e.codec)))?;
            if matches!(e.kind, section::RECORDS | section::SLOTS | section::PROPERTY) && codec != Codec::None {
                return err(Class::Archive, format!("section type {} must use codec 0", e.kind));
            }
            if codec == Codec::None && e.length != e.size {
                return err(Class::Archive, format!("section type {}: codec 0 with length {} and size {}", e.kind, e.length, e.size));
            }
            if !e.within(file_size) {
                return err(Class::Archive, format!("section type {} lies outside the file", e.kind));
            }
            if e.kind == section::PROPERTY {
                property_entries.push(*e);
            } else if one[e.kind as usize].replace(*e).is_some() {
                return err(Class::Archive, format!("more than one section of type {}", e.kind));
            }
        }
        let keyed = [section::RECORDS, section::PAGES, section::PTRHASH, section::SLOTS];
        for t in keyed {
            if one[t as usize].is_some() != (n > 0) {
                return err(Class::Archive, format!("section type {t} {} in a store of {n} keys", if n > 0 { "missing" } else { "present" }));
            }
        }
        let whole = |e: &DirEntry| -> Result<Vec<u8>, ReadError> {
            if e.size > limits.open_bytes || e.length > limits.open_bytes {
                return err(Class::Archive, format!("section type {} exceeds the limit for data read at open", e.kind));
            }
            let stored = read(e.offset, e.length)?;
            if format::crc32c(&stored) != e.crc32c {
                return err(Class::Archive, format!("section type {} does not match its CRC-32C", e.kind));
            }
            format::decode(Codec::from_u8(e.codec).unwrap(), &stored, e.size).map_err(|m| archive(format!("section type {}: {m}", e.kind)))
        };

        // the sections read whole, and their contents
        let Some(src) = one[section::SOURCES as usize] else {
            return err(Class::Archive, "no SOURCES section");
        };
        let (sources, revision) = pb::decode_source_table(&whole(&src)?).map_err(|e| archive(format!("source table: {e}")))?;
        if sources.len() > limits.sources {
            return err(Class::Archive, format!("{} sources, more than the limit of {}", sources.len(), limits.sources));
        }
        for s in &sources {
            match &s.kind {
                SourceKind::Url(u) if u.is_empty() => return err(Class::Archive, "an empty url source"),
                SourceKind::Key(k) if k.is_empty() => return err(Class::Archive, "an empty key source"),
                _ => {}
            }
        }
        let metadata = match one[section::METADATA as usize] {
            Some(e) => format::decode_metadata(&whole(&e)?).map_err(|m| archive(format!("METADATA: {m}")))?,
            None => Vec::new(),
        };
        let (mut records, mut slots, mut hash, mut pages) = ((0, 0), 0, None, Vec::new());
        if n > 0 {
            let rec = one[section::RECORDS as usize].unwrap();
            records = (rec.offset, rec.offset + rec.length);
            let (params, pilots) = format::decode_ptrhash(whole(&one[section::PTRHASH as usize].unwrap())?, n).map_err(|m| archive(format!("PTRHASH: {m}")))?;
            let sl = one[section::SLOTS as usize].unwrap();
            if params.total_slots().checked_mul(8) != Some(sl.length) {
                return err(Class::Archive, format!("SLOTS of {} bytes for {} slots", sl.length, params.total_slots()));
            }
            slots = sl.offset;
            hash = Some((params, pilots));
            pages = format::decode_pages(&whole(&one[section::PAGES as usize].unwrap())?, records.0, records.1).map_err(|m| archive(format!("PAGES: {m}")))?;
        }
        Ok(Store { r, file_size, sb, limits, sources, revision, metadata, records, slots, hash, pages, property_entries, properties: OnceLock::new() })
    }

    pub fn superblock(&self) -> &Superblock {
        &self.sb
    }

    pub fn sources(&self) -> &[Source] {
        &self.sources
    }

    /// The revision of spec/refkv.md the writer followed, as the source table records it.
    pub fn revision(&self) -> Option<u32> {
        self.revision
    }

    /// The entries of the METADATA section.
    pub fn metadata(&self) -> &[(String, Vec<u8>)] {
        &self.metadata
    }

    pub fn key_count(&self) -> u64 {
        self.sb.key_count
    }

    fn read(&self, off: u64, len: u64, class: Class) -> Result<Vec<u8>, ReadError> {
        if off.checked_add(len).is_none_or(|e| e > self.file_size) {
            return err(class, format!("[{off}, {off} + {len}) lies outside the file"));
        }
        if len > self.limits.request_bytes {
            return err(Class::Request, format!("a read of {len} bytes exceeds the limit of {}", self.limits.request_bytes));
        }
        let mut b = vec![0u8; len as usize];
        self.r.read_at(off, &mut b).map_err(io_err)?;
        Ok(b)
    }

    /// The record at `off` in RECORDS: an entry error if it is outside or malformed.
    fn record_at(&self, off: u64) -> Result<Record, ReadError> {
        let (start, end) = self.records;
        if off < start || off >= end {
            return err(Class::Entry, format!("a slot points at {off}, outside RECORDS [{start}, {end})"));
        }
        let mut len = self.limits.record_read.min(end - off);
        loop {
            let b = self.read(off, len, Class::Entry)?;
            match format::parse_record(&b, self.sb.hybrid()).map_err(|m| ReadError { class: Class::Entry, message: format!("record at {off}: {m}") })? {
                Parsed::Whole(rec, _) => return Ok(rec),
                Parsed::Needs(n) if n > end - off => return err(Class::Entry, format!("record at {off} extends past the end of RECORDS")),
                Parsed::Needs(n) if n <= len => return err(Class::Entry, format!("record at {off} is malformed")),
                Parsed::Needs(n) => len = n,
            }
        }
    }

    /// The record of `key`, hidden or not (spec/bindings/phkv.md §19.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#195-lookup>).
    fn lookup(&self, key: &str) -> Result<Option<Record>, ReadError> {
        let Some((params, pilots)) = &self.hash else {
            return Ok(None);
        };
        let s = ptrhash::slot(params, pilots, ptrhash::hash(key.as_bytes(), params.seed));
        let b = self.read(self.slots + 8 * s, 8, Class::Entry)?;
        let off = format::le_u64(&b, 0);
        if off == format::EMPTY_SLOT {
            return Ok(None);
        }
        let rec = self.record_at(off)?;
        Ok((rec.key == key.as_bytes()).then_some(rec))
    }

    /// The record of `key` if it is in the mapping: not hidden.
    fn lookup_visible(&self, key: &str) -> Result<Option<Record>, ReadError> {
        Ok(self.lookup(key)?.filter(|r| !r.hidden))
    }

    pub fn classify(&self, key: &str) -> Result<Option<Kind>, ReadError> {
        Ok(self.lookup_visible(key)?.map(|r| match r.value {
            Value::Bytes { .. } => Kind::Bytes,
            Value::Reference { .. } => Kind::Reference,
        }))
    }

    /// Bytes `[a, b)` of a bytes record's value: a codec-0 value is read only within the window, a
    /// compressed one is decoded whole; the CRC-32C is checked whenever the whole value is had.
    fn bytes_window(&self, rec: &Record, a: u64, b: u64) -> Result<Vec<u8>, ReadError> {
        let Value::Bytes { codec, stored, stored_length, size, crc32c } = &rec.value else { unreachable!() };
        let (stored_length, size) = (*stored_length, *size);
        if let Stored::At(o) = stored
            && o.checked_add(stored_length).is_none_or(|e| e > self.file_size)
        {
            return err(Class::Body, format!("the value of {:?} lies outside the file", String::from_utf8_lossy(&rec.key)));
        }
        let check = |v: &[u8]| match crc32c {
            Some(c) if format::crc32c(v) != *c => err(Class::Body, "the value does not match its CRC-32C"),
            _ => Ok(()),
        };
        if *codec == Codec::None {
            if stored_length != size {
                return err(Class::Body, format!("a codec-0 value of {stored_length} stored bytes and size {size}"));
            }
            let v = match stored {
                Stored::Inline(d) => d[a as usize..b as usize].to_vec(),
                Stored::At(o) => self.read(o + a, b - a, Class::Body)?,
            };
            if a == 0 && b == size {
                check(&v)?;
            }
            return Ok(v);
        }
        if size > self.limits.request_bytes {
            return err(Class::Request, format!("decoding a value of {size} bytes exceeds the limit of {}", self.limits.request_bytes));
        }
        let raw = match stored {
            Stored::Inline(d) => d.clone(),
            Stored::At(o) => self.read(*o, stored_length, Class::Body)?,
        };
        let v = format::decode(*codec, &raw, size).map_err(|m| ReadError { class: Class::Body, message: m })?;
        check(&v)?;
        Ok(v[a as usize..b as usize].to_vec())
    }

    /// `get` (spec/bindings/phkv.md §22.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#222-operations>).
    pub fn get(&self, key: &str, request: Request, resolver: &dyn Resolver) -> Result<Option<Vec<u8>>, ReadError> {
        if let Request::Range(s, e) = request
            && s > e
        {
            return err(Class::Request, format!("range start {s} > end {e}"));
        }
        let Some(rec) = self.lookup_visible(key)? else {
            return Ok(None);
        };
        match &rec.value {
            Value::Bytes { size, .. } => {
                let (a, b) = request.window(*size);
                if b - a > self.limits.request_bytes {
                    return err(Class::Request, "the window exceeds the request limit");
                }
                self.bytes_window(&rec, a, b).map(Some)
            }
            Value::Reference { kind, payload } => self.resolve(*kind, payload, request, resolver).map(Some),
        }
    }

    /// Resolves a reference (spec/refkv.md §6.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>).
    fn resolve(&self, kind: u8, payload: &[u8], request: Request, resolver: &dyn Resolver) -> Result<Vec<u8>, ReadError> {
        let payload_err = |m: String| ReadError { class: Class::Payload, message: m };
        if payload.len() as u64 > format::MAX_PAYLOAD {
            return Err(payload_err("a payload longer than 2^24 bytes".into()));
        }
        let ranges = if kind == format::KIND_RANGE {
            vec![Range::decode(payload).map_err(|e| payload_err(e.to_string()))?]
        } else {
            pb::decode_concat(payload).map_err(|e| payload_err(e.to_string()))?
        };
        for r in &ranges {
            if r.data.is_none() && r.source as usize >= self.sources.len() {
                return Err(payload_err(format!("source {} of {} sources", r.source, self.sources.len())));
            }
        }
        let n: u64 = ranges.iter().map(|r| r.size()).sum();
        let (a, b) = request.window(n);
        if b - a > self.limits.request_bytes {
            return err(Class::Request, "the window exceeds the request limit");
        }
        // the ranges the window needs, and whether the url reads are within the limit
        let mut parts: Vec<(&Range, u64, u64)> = Vec::new();
        let mut start = 0u64;
        for r in &ranges {
            let end = start + r.size();
            let (i, j) = (a.max(start), b.min(end));
            if i < j {
                parts.push((r, i - start, j - start));
            }
            start = end;
        }
        let url_reads = parts.iter().filter(|(r, ..)| r.data.is_none() && matches!(self.sources[r.source as usize].kind, SourceKind::Url(_))).count();
        if url_reads > self.limits.url_reads {
            return err(Class::Request, format!("{url_reads} url reads, more than the limit of {}", self.limits.url_reads));
        }
        let mut out = Vec::with_capacity((b - a) as usize);
        for (r, i, j) in parts {
            if let Some(d) = &r.data {
                out.extend_from_slice(&d[i as usize..j as usize]);
                continue;
            }
            // a range with a checksum is read whole, and checked before any of it is used
            let (ri, rj) = if r.crc32c.is_some() { (0, r.length) } else { (i, j) };
            let bytes = self.source_bytes(&self.sources[r.source as usize], r.offset + ri, rj - ri, resolver)?;
            if let Some(c) = r.crc32c
                && format::crc32c(&bytes) != c
            {
                return err(Class::Resolution, format!("a range of source {} does not match its CRC-32C", r.source));
            }
            out.extend_from_slice(&bytes[(i - ri) as usize..(j - ri) as usize]);
        }
        Ok(out)
    }

    /// Bytes `[offset, offset + length)` of a source value; every failure is a resolution error.
    fn source_bytes(&self, src: &Source, offset: u64, length: u64, resolver: &dyn Resolver) -> Result<Vec<u8>, ReadError> {
        let resolution = |m: String| ReadError { class: Class::Resolution, message: m };
        let end = offset.checked_add(length).ok_or_else(|| resolution("range end exceeds 2^64 - 1".into()))?;
        match &src.kind {
            SourceKind::Data(d) => {
                if end > d.len() as u64 {
                    return Err(resolution(format!("a range ends at {end}, past the {} bytes of a data source", d.len())));
                }
                Ok(d[offset as usize..end as usize].to_vec())
            }
            SourceKind::Key(k) => {
                let rec = self.lookup(k).map_err(|e| resolution(format!("key source {k:?}: {}", e.message)))?;
                let rec = match rec {
                    Some(r) if matches!(r.value, Value::Bytes { .. }) => r,
                    Some(_) => return Err(resolution(format!("key source {k:?} is a reference"))),
                    None => return Err(resolution(format!("key source {k:?} is missing"))),
                };
                let Value::Bytes { size, .. } = rec.value else { unreachable!() };
                if end > size {
                    return Err(resolution(format!("a range ends at {end}, past the {size} bytes of key source {k:?}")));
                }
                self.bytes_window(&rec, offset, end).map_err(|e| resolution(format!("key source {k:?}: {}", e.message)))
            }
            SourceKind::Url(_) => {
                let b = resolver.read(src, offset, length).map_err(resolution)?;
                if b.len() as u64 != length {
                    return Err(resolution(format!("the source gave {} bytes of {length}", b.len())));
                }
                Ok(b)
            }
        }
    }

    /// `list` (spec/bindings/phkv.md §22.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#222-operations>): the keys
    /// that are not hidden and start with `prefix`, in UTF-8 order.
    pub fn list(&self, prefix: &str) -> Result<Vec<String>, ReadError> {
        let mut out = Vec::new();
        for i in 0..self.pages.len() {
            if !format::page_may_hold(&self.pages, i, prefix.as_bytes()) {
                continue;
            }
            for rec in self.page_records(i)? {
                if !rec.hidden && rec.key.starts_with(prefix.as_bytes()) {
                    out.push(String::from_utf8(rec.key).map_err(|_| ReadError { class: Class::Entry, message: "a key that is not UTF-8".into() })?);
                }
            }
        }
        Ok(out)
    }

    /// The records of page `i`; an entry error if the page cannot be parsed.
    fn page_records(&self, i: usize) -> Result<Vec<Record>, ReadError> {
        let p = &self.pages[i];
        let b = self.read(p.offset, p.length, Class::Entry)?;
        let mut recs = Vec::new();
        let mut at = 0usize;
        while at < b.len() {
            match format::parse_record(&b[at..], self.sb.hybrid()) {
                Ok(Parsed::Whole(rec, len)) => {
                    recs.push(rec);
                    at += len;
                }
                Ok(Parsed::Needs(_)) => return err(Class::Entry, format!("page {i} ends inside a record")),
                Err(m) => return err(Class::Entry, format!("page {i}: {m}")),
            }
        }
        if recs.first().is_none_or(|r| r.key != p.first_key) {
            return err(Class::Entry, format!("page {i} does not start with its first_key"));
        }
        Ok(recs)
    }

    /// `raw`: in a standalone store, a bytes value's stored bytes, not decoded; in a hybrid store,
    /// what vzip's raw view gives: the decoded value, and the container region for `__vz__/phkv`.
    /// A reference gives its payload. Hidden records are included.
    pub fn raw(&self, key: &str) -> Result<Option<Vec<u8>>, ReadError> {
        if self.sb.hybrid() && key == format::HYBRID_KEY {
            return self.read(self.sb.region_offset, self.sb.region_length, Class::Body).map(Some);
        }
        let Some(rec) = self.lookup(key)? else {
            return Ok(None);
        };
        match &rec.value {
            Value::Reference { payload, .. } => Ok(Some(payload.clone())),
            Value::Bytes { size, .. } if self.sb.hybrid() => self.bytes_window(&rec, 0, *size).map(Some),
            Value::Bytes { codec, stored, stored_length, size, crc32c } => {
                let v = match stored {
                    Stored::Inline(d) => d.clone(),
                    Stored::At(o) => {
                        if o.checked_add(*stored_length).is_none_or(|e| e > self.file_size) {
                            return err(Class::Body, "the stored bytes lie outside the file");
                        }
                        self.read(*o, *stored_length, Class::Body)?
                    }
                };
                if *codec == Codec::None {
                    if *stored_length != *size {
                        return err(Class::Body, "a codec-0 value whose stored_length and size differ");
                    }
                    if let Some(c) = crc32c
                        && format::crc32c(&v) != *c
                    {
                        return err(Class::Body, "the value does not match its CRC-32C");
                    }
                }
                Ok(Some(v))
            }
        }
    }

    /// `entry_info`: how a key's value is stored, so that what [`Store::raw`] gives can be
    /// interpreted. Hidden records are included.
    pub fn entry_info(&self, key: &str) -> Result<Option<EntryInfo>, ReadError> {
        Ok(self.lookup(key)?.map(|r| match r.value {
            Value::Bytes { codec, stored_length, size, crc32c, .. } => EntryInfo::Bytes { codec, stored_length, size, crc32c, hidden: r.hidden },
            Value::Reference { payload, .. } => EntryInfo::Reference { payload_length: payload.len() as u64, hidden: r.hidden },
        }))
    }

    /// The headers of the PROPERTY sections, read once.
    fn property_sections(&self) -> Result<&[PropertySection], ReadError> {
        let r = self.properties.get_or_init(|| {
            let n = self.sb.key_count;
            let mut out = Vec::new();
            for e in &self.property_entries {
                let head = self.read(e.offset, 8.min(e.length), Class::Property).map_err(|e| e.message)?;
                if head.len() < 8 {
                    return Err("a PROPERTY section shorter than its header".to_string());
                }
                let name_length = format::le_u32(&head, 0) as u64;
                let type_code = head[4];
                if head[5..8] != [0; 3] || !(format::TYPE_U64..=format::TYPE_STRING).contains(&type_code) || name_length == 0 || 8 + name_length > e.length {
                    return Err("a malformed PROPERTY header".to_string());
                }
                let name = String::from_utf8(self.read(e.offset + 8, name_length, Class::Property).map_err(|e| e.message)?).map_err(|_| "a property name that is not UTF-8".to_string())?;
                let (present, values) = format::property_header(name_length, n);
                let fixed = matches!(type_code, format::TYPE_U64 | format::TYPE_I64);
                let need = if fixed { values.checked_add(n.checked_mul(8).ok_or("too many keys")?) } else { values.checked_add((n + 1) * 8) }.ok_or("too many keys")?;
                if (fixed && e.length != need) || (!fixed && e.length < need) {
                    return Err(format!("PROPERTY {name:?} has {} bytes, not what its header implies", e.length));
                }
                if !fixed {
                    let last = format::le_u64(&self.read(e.offset + values + 8 * n, 8, Class::Property).map_err(|e| e.message)?, 0);
                    if need.checked_add(last) != Some(e.length) {
                        return Err(format!("PROPERTY {name:?}: its offsets do not end at its end"));
                    }
                }
                if format::registered_type(&name).is_some_and(|t| t != type_code) {
                    return Err(format!("registered property {name:?} with type {type_code}"));
                }
                out.push(PropertySection { name, type_code, entry: *e, present, values });
            }
            Ok(out)
        });
        r.as_deref().map_err(|m| ReadError { class: Class::Property, message: m.clone() })
    }

    /// `property`: `None` if the key is missing, `Some(None)` if it has no value for `name`.
    pub fn property(&self, key: &str, name: &str) -> Result<Option<Option<Prop>>, ReadError> {
        let Some(rec) = self.lookup(key)? else {
            return Ok(None);
        };
        let Some(sec) = self.property_sections()?.iter().find(|s| s.name == name) else {
            return Ok(Some(None));
        };
        let n = self.sb.key_count;
        let r = rec.rank;
        if r >= n {
            return err(Class::Property, format!("rank {r} of {n} records"));
        }
        let base = sec.entry.offset;
        let bits = self.read(base + sec.present + r / 8, 1, Class::Property)?;
        if bits[0] & (1 << (r % 8)) == 0 {
            return Ok(Some(None));
        }
        let at = base + sec.values + 8 * r;
        let v = match sec.type_code {
            format::TYPE_U64 => Prop::U64(format::le_u64(&self.read(at, 8, Class::Property)?, 0)),
            format::TYPE_I64 => Prop::I64(format::le_u64(&self.read(at, 8, Class::Property)?, 0) as i64),
            t => {
                let offs = self.read(at, 16, Class::Property)?;
                let (s, e) = (format::le_u64(&offs, 0), format::le_u64(&offs, 8));
                let heap = sec.values + 8 * (n + 1);
                if s > e || heap.checked_add(e).is_none_or(|end| end > sec.entry.length) {
                    return err(Class::Property, format!("PROPERTY {name:?}: offsets out of order or past its end"));
                }
                let b = self.read(base + heap + s, e - s, Class::Property)?;
                if t == format::TYPE_STRING {
                    Prop::Str(String::from_utf8(b).map_err(|_| ReadError { class: Class::Property, message: format!("PROPERTY {name:?}: a value that is not UTF-8") })?)
                } else {
                    Prop::Bytes(b)
                }
            }
        };
        Ok(Some(Some(v)))
    }
}

