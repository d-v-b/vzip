//! vzip reader (spec §3, §4, §6, §7, §8).

use crate::http;
use crate::proto::{self, CdIndex, Range, SourceKind};
use crate::uri::{self, Uri};
use crate::{Error, HIDDEN_PREFIX, INDEX_KEY, SOURCES_KEY};
use std::cell::RefCell;
use std::collections::HashMap;
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::Path;

/// Largest window a single get may return (resource limit, §10). Larger
/// requests fail with a request error.
pub const MAX_WINDOW: u64 = 1 << 30;

const SIG_EOCD: u32 = 0x0605_4b50;
const SIG_Z64_EOCD: u32 = 0x0606_4b50;
const SIG_Z64_LOC: u32 = 0x0706_4b50;
const SIG_CDR: u32 = 0x0201_4b50;

fn u16le(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}
fn u32le(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn u64le(b: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Bytes,
    Reference,
    Missing,
}

/// A get request (§8.2).
#[derive(Debug, Clone, Copy)]
pub enum Request {
    Whole,
    Range(u64, u64),
    Offset(u64),
    Suffix(u64),
}

impl Request {
    fn check(&self) -> Result<(), Error> {
        if let Request::Range(s, e) = self {
            if s > e {
                return Err(Error::Request(format!("range start {} > end {}", s, e)));
            }
        }
        Ok(())
    }
    /// The window `[a, b)` for a value of size `n`.
    fn window(&self, n: u64) -> (u64, u64) {
        match *self {
            Request::Whole => (0, n),
            Request::Range(s, e) => (s.min(n), e.min(n)),
            Request::Offset(s) => (s.min(n), n),
            Request::Suffix(c) => (n.saturating_sub(c), n),
        }
    }
}

/// A raw central directory record.
#[derive(Debug, Clone)]
pub struct Record {
    pub name: Vec<u8>,
    pub flags: u16,
    pub method: u16,
    pub csize: u32,
    pub usize_: u32,
    pub lho: u32,
    pub extra: Vec<u8>,
}

/// Parses one central directory record at `buf[pos..]`, bounded by `buf`.
/// Returns the record and the position after it.
fn parse_record(buf: &[u8], pos: usize) -> Result<(Record, usize), String> {
    if buf.len() - pos < 46 {
        return Err(format!("truncated central directory record at {}", pos));
    }
    if u32le(buf, pos) != SIG_CDR {
        return Err(format!("bad central directory record signature at {}", pos));
    }
    let nlen = u16le(buf, pos + 28) as usize;
    let xlen = u16le(buf, pos + 30) as usize;
    let clen = u16le(buf, pos + 32) as usize;
    let end = pos + 46 + nlen + xlen + clen;
    if end > buf.len() {
        return Err(format!("central directory record at {} overruns", pos));
    }
    let name = buf[pos + 46..pos + 46 + nlen].to_vec();
    let extra = buf[pos + 46 + nlen..pos + 46 + nlen + xlen].to_vec();
    Ok((
        Record {
            name,
            flags: u16le(buf, pos + 8),
            method: u16le(buf, pos + 10),
            csize: u32le(buf, pos + 20),
            usize_: u32le(buf, pos + 24),
            lho: u32le(buf, pos + 42),
            extra,
        },
        end,
    ))
}

/// Parses a buffer that must be exactly a sequence of whole records.
fn parse_records(buf: &[u8]) -> Result<Vec<Record>, String> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < buf.len() {
        let (r, next) = parse_record(buf, pos)?;
        out.push(r);
        pos = next;
    }
    Ok(out)
}

/// Valid key name for a record: non-empty valid UTF-8 (§3.3).
fn record_key(r: &Record) -> Option<&str> {
    if r.name.is_empty() {
        return None;
    }
    std::str::from_utf8(&r.name).ok()
}

/// What a lookup resolves an entry to, after entry-error checks.
#[derive(Debug, Clone)]
pub enum EntryInfo {
    Bytes {
        method: u16,
        body_offset: u64,
        csize: u64,
        usize_: u64,
    },
    Reference {
        block_id: u16,
        payload: Vec<u8>,
        method: u16,
        body_offset: u64,
        csize: u64,
        usize_: u64,
    },
}

/// Applies §4.1, §3.2 and §8.4 entry-error rules to a record.
fn entry_info(r: &Record) -> Result<EntryInfo, Error> {
    let ee = |m: String| Error::Entry(m);
    // Parse extra field blocks.
    let mut blocks: Vec<(u16, &[u8])> = Vec::new();
    let x = &r.extra;
    let mut p = 0;
    while p < x.len() {
        if x.len() - p < 4 {
            return Err(ee("extra field does not parse".into()));
        }
        let id = u16le(x, p);
        let sz = u16le(x, p + 2) as usize;
        if x.len() - p - 4 < sz {
            return Err(ee("extra field does not parse".into()));
        }
        blocks.push((id, &x[p + 4..p + 4 + sz]));
        p += 4 + sz;
    }
    let refs: Vec<&(u16, &[u8])> = blocks.iter().filter(|(id, _)| *id == 0x7A76 || *id == 0x7A77).collect();
    if refs.len() > 1 {
        return Err(ee("more than one reference block".into()));
    }
    if r.method != 0 && r.method != 8 {
        return Err(ee(format!("unsupported compression method {}", r.method)));
    }
    if r.flags & 1 != 0 {
        return Err(ee("entry is encrypted".into()));
    }
    if refs.len() == 1 && r.method == 8 {
        return Err(ee("reference entry uses method 8".into()));
    }
    // ZIP64 extended information.
    let mut usize_ = r.usize_ as u64;
    let mut csize = r.csize as u64;
    let mut lho = r.lho as u64;
    let need_u = r.usize_ == u32::MAX;
    let need_c = r.csize == u32::MAX;
    let need_o = r.lho == u32::MAX;
    if need_u || need_c || need_o {
        let z: Vec<&(u16, &[u8])> = blocks.iter().filter(|(id, _)| *id == 0x0001).collect();
        if z.is_empty() {
            return Err(ee("all-ones field without ZIP64 extra block".into()));
        }
        if z.len() > 1 {
            return Err(ee("more than one ZIP64 extra block".into()));
        }
        let d = z[0].1;
        let needed = 8 * (need_u as usize + need_c as usize + need_o as usize);
        if d.len() < needed {
            return Err(ee("ZIP64 extra block too short".into()));
        }
        let mut q = 0;
        if need_u {
            usize_ = u64le(d, q);
            q += 8;
        }
        if need_c {
            csize = u64le(d, q);
            q += 8;
        }
        if need_o {
            lho = u64le(d, q);
        }
    }
    let body_offset = lho as u128 + 30 + r.name.len() as u128;
    let body_offset = if body_offset > u64::MAX as u128 { u64::MAX } else { body_offset as u64 };
    Ok(match refs.first() {
        None => EntryInfo::Bytes { method: r.method, body_offset, csize, usize_ },
        Some((id, data)) => EntryInfo::Reference {
            block_id: *id,
            payload: data.to_vec(),
            method: r.method,
            body_offset,
            csize,
            usize_,
        },
    })
}

/// Inflates a raw DEFLATE body, requiring it to be one complete stream that
/// ends exactly at the end of `body` (§8.1). If `expected` is given, the
/// output must have exactly that length.
pub fn inflate_clean(body: &[u8], expected: Option<u64>) -> Result<Vec<u8>, String> {
    use flate2::{Decompress, FlushDecompress, Status};
    let mut d = Decompress::new(false);
    let cap_limit: u64 = match expected {
        Some(n) => n + 1,
        None => u32::MAX as u64 + 1,
    };
    let mut out: Vec<u8> = Vec::with_capacity(expected.map(|n| (n + 1).min(1 << 26) as usize).unwrap_or(1024));
    loop {
        if out.len() == out.capacity() {
            if out.len() as u64 >= cap_limit {
                return Err("DEFLATE stream inflates to more bytes than expected".into());
            }
            let grow = (out.capacity().max(1024) as u64).min(cap_limit - out.len() as u64);
            out.reserve_exact(grow as usize);
        }
        let in_before = d.total_in();
        let out_before = d.total_out();
        let st = d
            .decompress_vec(&body[d.total_in() as usize..], &mut out, FlushDecompress::None)
            .map_err(|e| format!("DEFLATE error: {}", e))?;
        match st {
            Status::StreamEnd => break,
            _ => {
                if d.total_in() == in_before && d.total_out() == out_before && out.len() < out.capacity() {
                    return Err("DEFLATE stream is truncated".into());
                }
            }
        }
    }
    if d.total_in() != body.len() as u64 {
        return Err("bytes follow the end of the DEFLATE stream".into());
    }
    if let Some(n) = expected {
        if out.len() as u64 != n {
            return Err(format!("DEFLATE body inflates to {} bytes, expected {}", out.len(), n));
        }
    }
    Ok(out)
}

#[derive(Debug, Clone)]
enum Lookup {
    Missing,
    /// A format entry, by key.
    Format(&'static str),
    Entry(EntryInfo),
}

impl std::fmt::Debug for Archive {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Archive({})", self.base.to_string())
    }
}

pub struct Archive {
    file: File,
    file_size: u64,
    base: Uri,
    cd_offset: u64,
    #[allow(dead_code)]
    cd_size: u64,
    sources: Vec<proto::Source>,
    sources_raw: Vec<u8>,
    index: Option<CdIndex>,
    index_raw: Option<Vec<u8>>,
    /// Unpaged: all records by name (first occurrence wins).
    records: Option<HashMap<Vec<u8>, Record>>,
    page_cache: RefCell<HashMap<usize, Result<Vec<Record>, String>>>,
}

impl Archive {
    pub fn open(path: &Path) -> Result<Archive, Error> {
        let base = uri::base_uri_for_path(path)
            .map_err(|e| Error::Archive(format!("cannot determine base URI: {}", e)))?;
        let file = File::open(path).map_err(|e| Error::Archive(format!("cannot open: {}", e)))?;
        let meta = file.metadata().map_err(|e| Error::Archive(format!("cannot stat: {}", e)))?;
        if !meta.is_file() {
            return Err(Error::Archive("not a regular file".into()));
        }
        Self::open_file(file, meta.len(), base)
    }

    fn read_at(file: &File, off: u64, len: u64) -> Result<Vec<u8>, String> {
        let mut buf = vec![0u8; len as usize];
        file.read_exact_at(&mut buf, off).map_err(|e| format!("read failed: {}", e))?;
        Ok(buf)
    }

    pub fn open_file(file: File, file_size: u64, base: Uri) -> Result<Archive, Error> {
        let ae = |m: String| Error::Archive(m);
        // §3.4: locate the end of central directory record.
        let mut eocd_off = None;
        if file_size >= 60 {
            let b = Self::read_at(&file, file_size - 60, 22).map_err(ae)?;
            if u32le(&b, 0) == SIG_EOCD && u16le(&b, 20) == 38 {
                eocd_off = Some(file_size - 60);
            }
        }
        if eocd_off.is_none() && file_size >= 44 {
            let b = Self::read_at(&file, file_size - 44, 22).map_err(ae)?;
            if u32le(&b, 0) == SIG_EOCD && u16le(&b, 20) == 22 {
                eocd_off = Some(file_size - 44);
            }
        }
        let eocd_off = eocd_off.ok_or_else(|| ae("not a vzip archive: no end of central directory record".into()))?;
        let tail = Self::read_at(&file, eocd_off, file_size - eocd_off).map_err(ae)?;
        let comment = &tail[22..];
        if !comment.starts_with(b"vzip/") {
            return Err(ae("not a vzip archive: comment does not start with vzip/".into()));
        }
        if comment[5] != b'0' {
            return Err(ae(format!(
                "unsupported vzip format version {:?}",
                String::from_utf8_lossy(&comment[5..6])
            )));
        }
        let sources_offset = u64le(comment, 6);
        let sources_size = u64le(comment, 14);
        let paged = comment.len() == 38;
        let (index_offset, index_size) = if paged { (u64le(comment, 22), u64le(comment, 30)) } else { (0, 0) };

        // §3.2: ZIP64.
        let n_disk = u16le(&tail, 8);
        let n_total = u16le(&tail, 10);
        let mut cd_size = u32le(&tail, 12) as u64;
        let mut cd_offset = u32le(&tail, 16) as u64;
        if n_disk == 0xFFFF || n_total == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_offset == 0xFFFF_FFFF {
            if eocd_off < 20 {
                return Err(ae("zip64 locator missing".into()));
            }
            let loc = Self::read_at(&file, eocd_off - 20, 20).map_err(ae)?;
            if u32le(&loc, 0) != SIG_Z64_LOC {
                return Err(ae("zip64 locator missing".into()));
            }
            let z_off = u64le(&loc, 8);
            if z_off.checked_add(56).map_or(true, |e| e > file_size) {
                return Err(ae("zip64 end of central directory record outside the file".into()));
            }
            let z = Self::read_at(&file, z_off, 56).map_err(ae)?;
            if u32le(&z, 0) != SIG_Z64_EOCD {
                return Err(ae("bad zip64 end of central directory signature".into()));
            }
            if u64le(&z, 4) != 44 {
                return Err(ae("zip64 end of central directory record size is not 44".into()));
            }
            cd_size = u64le(&z, 40);
            cd_offset = u64le(&z, 48);
        }
        if cd_offset.checked_add(cd_size).map_or(true, |e| e > file_size) {
            return Err(ae("central directory lies outside the file".into()));
        }

        // Format entries.
        let read_format = |off: u64, size: u64, what: &str| -> Result<Vec<u8>, Error> {
            if off.checked_add(size).map_or(true, |e| e > file_size) {
                return Err(ae(format!("{} body lies outside the file", what)));
            }
            if size > u32::MAX as u64 {
                return Err(ae(format!("{} body too large", what)));
            }
            let body = Self::read_at(&file, off, size).map_err(ae)?;
            inflate_clean(&body, None).map_err(|m| ae(format!("{}: {}", what, m)))
        };
        let sources_raw = read_format(sources_offset, sources_size, SOURCES_KEY)?;
        let table = proto::decode_source_table(&sources_raw)
            .map_err(|m| ae(format!("source table is malformed: {}", m)))?;
        for (i, s) in table.sources.iter().enumerate() {
            match &s.kind {
                None => return Err(ae(format!("source {} has no kind", i))),
                Some(SourceKind::Url(u)) => {
                    if u.is_empty() {
                        return Err(ae(format!("source {} has an empty url", i)));
                    }
                    if let Some(e) = &s.etag {
                        if !crate::is_strong_etag(e) {
                            return Err(ae(format!("source {} etag pin is not a strong entity tag", i)));
                        }
                    }
                }
                Some(_) => {
                    if s.size.is_some() || s.etag.is_some() || s.modified_not_after.is_some() {
                        return Err(ae(format!("source {} has a pin but is not a url source", i)));
                    }
                }
            }
        }

        let mut index = None;
        let mut index_raw = None;
        if paged {
            let raw = read_format(index_offset, index_size, INDEX_KEY)?;
            let ix = proto::decode_cd_index(&raw)
                .map_err(|m| ae(format!("page index is malformed: {}", m)))?;
            let mut expect: u64 = 0;
            for (i, p) in ix.pages.iter().enumerate() {
                if p.length == 0 {
                    return Err(ae(format!("page {} has length 0", i)));
                }
                if p.offset.checked_add(p.length).map_or(true, |e| e > cd_size) {
                    return Err(ae(format!("page {} lies outside the central directory", i)));
                }
                if p.offset != expect {
                    return Err(ae(format!("page {} is not contiguous", i)));
                }
                expect = p.offset + p.length;
                if p.first_key.is_empty() {
                    return Err(ae(format!("page {} has an empty first_key", i)));
                }
                if i > 0 && ix.pages[i - 1].first_key.as_bytes() >= p.first_key.as_bytes() {
                    return Err(ae("page first_key values do not strictly increase".into()));
                }
            }
            let mut seen = std::collections::HashSet::new();
            for p in &ix.pinned {
                if p.key.is_empty() {
                    return Err(ae("pinned key is empty".into()));
                }
                if !seen.insert(p.key.clone()) {
                    return Err(ae(format!("pinned key {:?} listed twice", p.key)));
                }
                if p.key == SOURCES_KEY || p.key == INDEX_KEY {
                    return Err(ae("pinned key is a format entry".into()));
                }
                if p.method != 0 && p.method != 8 {
                    return Err(ae(format!("pinned method {} is not 0 or 8", p.method)));
                }
                if p.data_offset.checked_add(p.csize).map_or(true, |e| e > file_size) {
                    return Err(ae(format!("pinned body of {:?} lies outside the file", p.key)));
                }
            }
            index = Some(ix);
            index_raw = Some(raw);
        }

        let mut records = None;
        if !paged {
            if cd_size > (1 << 32) {
                return Err(ae("central directory too large for this reader".into()));
            }
            let cd = Self::read_at(&file, cd_offset, cd_size).map_err(ae)?;
            let recs = parse_records(&cd).map_err(|m| ae(format!("central directory: {}", m)))?;
            let mut map = HashMap::new();
            for r in recs {
                if r.name == INDEX_KEY.as_bytes() {
                    return Err(ae("archive without a page index has an __vz__/index entry".into()));
                }
                map.entry(r.name.clone()).or_insert(r);
            }
            records = Some(map);
        }

        Ok(Archive {
            file,
            file_size,
            base,
            cd_offset,
            cd_size,
            sources: table.sources,
            sources_raw,
            index,
            index_raw,
            records,
            page_cache: RefCell::new(HashMap::new()),
        })
    }

    pub fn base_uri(&self) -> &Uri {
        &self.base
    }

    fn page_records(&self, i: usize) -> Result<Vec<Record>, Error> {
        if let Some(r) = self.page_cache.borrow().get(&i) {
            return r.clone().map_err(Error::Entry);
        }
        let ix = self.index.as_ref().unwrap();
        let p = &ix.pages[i];
        let res = Self::read_at(&self.file, self.cd_offset + p.offset, p.length)
            .and_then(|b| parse_records(&b))
            .map_err(|m| format!("page {} cannot be parsed: {}", i, m));
        self.page_cache.borrow_mut().insert(i, res.clone());
        res.map_err(Error::Entry)
    }

    /// Index of the page that would hold `key` (§7.2).
    fn page_for(&self, key: &[u8]) -> Option<usize> {
        let ix = self.index.as_ref().unwrap();
        let n = ix.pages.partition_point(|p| p.first_key.as_bytes() <= key);
        if n == 0 {
            None
        } else {
            Some(n - 1)
        }
    }

    /// Finds `key`'s entry, including hidden and format entries (§7.2).
    fn lookup(&self, key: &str) -> Result<Lookup, Error> {
        if key == SOURCES_KEY {
            return Ok(Lookup::Format(SOURCES_KEY));
        }
        if key == INDEX_KEY {
            return Ok(if self.index.is_some() { Lookup::Format(INDEX_KEY) } else { Lookup::Missing });
        }
        let kb = key.as_bytes();
        match &self.index {
            None => {
                let recs = self.records.as_ref().unwrap();
                match recs.get(kb) {
                    None => Ok(Lookup::Missing),
                    Some(r) if record_key(r).is_none() => Ok(Lookup::Missing),
                    Some(r) => Ok(Lookup::Entry(entry_info(r)?)),
                }
            }
            Some(ix) => {
                if let Some(p) = ix.pinned.iter().find(|p| p.key == key) {
                    return Ok(Lookup::Entry(EntryInfo::Bytes {
                        method: p.method as u16,
                        body_offset: p.data_offset,
                        csize: p.csize,
                        usize_: p.size,
                    }));
                }
                let pi = match self.page_for(kb) {
                    None => return Ok(Lookup::Missing),
                    Some(i) => i,
                };
                let recs = self.page_records(pi)?;
                match recs.iter().find(|r| r.name == kb) {
                    None => Ok(Lookup::Missing),
                    Some(r) => Ok(Lookup::Entry(entry_info(r)?)),
                }
            }
        }
    }

    // ---------------------------------------------------------------------
    // Operations (§8.2)
    // ---------------------------------------------------------------------

    pub fn classify(&self, key: &str) -> Result<Kind, Error> {
        if key.starts_with(HIDDEN_PREFIX) {
            return Ok(Kind::Missing);
        }
        Ok(match self.lookup(key)? {
            Lookup::Missing => Kind::Missing,
            Lookup::Format(_) => Kind::Missing, // unreachable: format keys are hidden
            Lookup::Entry(EntryInfo::Bytes { .. }) => Kind::Bytes,
            Lookup::Entry(EntryInfo::Reference { .. }) => Kind::Reference,
        })
    }

    pub fn get(&self, key: &str, req: Request) -> Result<Option<Vec<u8>>, Error> {
        req.check()?;
        if key.starts_with(HIDDEN_PREFIX) {
            return Ok(None);
        }
        match self.lookup(key)? {
            Lookup::Missing | Lookup::Format(_) => Ok(None),
            Lookup::Entry(EntryInfo::Bytes { method, body_offset, csize, usize_ }) => {
                let (a, b) = req.window(usize_);
                Ok(Some(self.body_window(method, body_offset, csize, usize_, a, b)?))
            }
            Lookup::Entry(EntryInfo::Reference { block_id, payload, .. }) => {
                Ok(Some(self.resolve_reference(block_id, &payload, req)?))
            }
        }
    }

    pub fn raw(&self, key: &str) -> Result<Option<Vec<u8>>, Error> {
        match self.lookup(key)? {
            Lookup::Missing => Ok(None),
            Lookup::Format(k) => Ok(Some(if k == SOURCES_KEY {
                self.sources_raw.clone()
            } else {
                self.index_raw.clone().unwrap()
            })),
            Lookup::Entry(EntryInfo::Bytes { method, body_offset, csize, usize_ })
            | Lookup::Entry(EntryInfo::Reference { method, body_offset, csize, usize_, .. }) => {
                Ok(Some(self.body_window(method, body_offset, csize, usize_, 0, usize_)?))
            }
        }
    }

    pub fn list(&self, prefix: &str) -> Result<Vec<String>, Error> {
        let pb = prefix.as_bytes();
        let mut out: Vec<String> = Vec::new();
        let keep = |k: &str| k.as_bytes().starts_with(pb) && !k.starts_with(HIDDEN_PREFIX);
        match &self.index {
            None => {
                for r in self.records.as_ref().unwrap().values() {
                    if let Some(k) = record_key(r) {
                        if keep(k) {
                            out.push(k.to_string());
                        }
                    }
                }
            }
            Some(ix) => {
                for p in &ix.pinned {
                    if keep(&p.key) {
                        out.push(p.key.clone());
                    }
                }
                let n = ix.pages.len();
                for i in 0..n {
                    let lo = ix.pages[i].first_key.as_bytes();
                    let hi = if i + 1 < n { Some(ix.pages[i + 1].first_key.as_bytes()) } else { None };
                    // Smallest string >= lo that starts with the prefix.
                    let cand: Option<&[u8]> = if lo <= pb {
                        Some(pb)
                    } else if lo.starts_with(pb) {
                        Some(lo)
                    } else {
                        None
                    };
                    let cand = match cand {
                        None => continue,
                        Some(c) => c,
                    };
                    if let Some(h) = hi {
                        if cand >= h {
                            continue;
                        }
                    }
                    let recs = self.page_records(i)?;
                    for r in recs {
                        let k = match record_key(&r) {
                            Some(k) => k,
                            None => continue,
                        };
                        let kb = k.as_bytes();
                        // Listed only if lookup would find it in this page.
                        if kb < lo || hi.map_or(false, |h| kb >= h) {
                            continue;
                        }
                        if ix.pinned.iter().any(|p| p.key == k) {
                            continue;
                        }
                        if keep(k) {
                            out.push(k.to_string());
                        }
                    }
                }
            }
        }
        out.sort_unstable_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
        out.dedup();
        Ok(out)
    }

    // ---------------------------------------------------------------------
    // Bodies
    // ---------------------------------------------------------------------

    /// Reads `[a, b)` of an entry's (inflated) value, applying body-error rules.
    fn body_window(&self, method: u16, body_offset: u64, csize: u64, usize_: u64, a: u64, b: u64) -> Result<Vec<u8>, Error> {
        let be = |m: String| Error::Body(m);
        if body_offset.checked_add(csize).map_or(true, |e| e > self.file_size) {
            return Err(be("entry body lies outside the file".into()));
        }
        if method == 0 {
            if csize != usize_ {
                return Err(be("STORED entry's compressed and uncompressed sizes differ".into()));
            }
            if b - a > MAX_WINDOW {
                return Err(Error::Request("request exceeds the reader's window limit".into()));
            }
            Self::read_at(&self.file, body_offset + a, b - a).map_err(be)
        } else {
            if usize_ > MAX_WINDOW * 4 || csize > MAX_WINDOW * 4 {
                return Err(Error::Request("entry exceeds the reader's inflate limit".into()));
            }
            let body = Self::read_at(&self.file, body_offset, csize).map_err(be)?;
            let v = inflate_clean(&body, Some(usize_)).map_err(be)?;
            Ok(v[a as usize..b as usize].to_vec())
        }
    }

    // ---------------------------------------------------------------------
    // References (§8.3)
    // ---------------------------------------------------------------------

    fn decode_payload(&self, block_id: u16, payload: &[u8]) -> Result<Vec<Range>, Error> {
        let pe = |m: String| Error::Payload(m);
        let parts = if block_id == 0x7A76 {
            vec![proto::decode_range(payload).map_err(|m| pe(format!("malformed Range: {}", m)))?]
        } else {
            proto::decode_concat(payload).map_err(|m| pe(format!("malformed Concat: {}", m)))?.parts
        };
        let mut total: u128 = 0;
        for (i, r) in parts.iter().enumerate() {
            if r.data.is_some() {
                if r.source != 0 || r.offset != 0 || r.length != 0 {
                    return Err(pe(format!("literal range {} has non-zero source/offset/length", i)));
                }
            } else {
                if r.source as usize >= self.sources.len() {
                    return Err(pe(format!("range {} names source {} of {}", i, r.source, self.sources.len())));
                }
                if r.offset as u128 + r.length as u128 > u64::MAX as u128 {
                    return Err(pe(format!("range {} offset + length exceeds 2^64-1", i)));
                }
            }
            total += r.size() as u128;
        }
        if total > u64::MAX as u128 {
            return Err(pe("reference size exceeds 2^64-1".into()));
        }
        Ok(parts)
    }

    fn resolve_reference(&self, block_id: u16, payload: &[u8], req: Request) -> Result<Vec<u8>, Error> {
        let parts = self.decode_payload(block_id, payload)?;
        let n: u64 = parts.iter().map(|r| r.size()).sum();
        let (a, b) = req.window(n);
        if b - a > MAX_WINDOW {
            return Err(Error::Request("request exceeds the reader's window limit".into()));
        }
        let mut out = Vec::with_capacity((b - a) as usize);
        let mut pos: u64 = 0;
        for r in &parts {
            let size = r.size();
            let lo = a.max(pos);
            let hi = b.min(pos + size);
            if lo < hi {
                let i = lo - pos;
                let j = hi - pos;
                match &r.data {
                    Some(d) => out.extend_from_slice(&d[i as usize..j as usize]),
                    None => {
                        let bytes = self.read_source(r.source as usize, r.offset + i, r.offset + j)?;
                        out.extend_from_slice(&bytes);
                    }
                }
            }
            pos += size;
        }
        Ok(out)
    }

    /// Reads `[s, e)` (s < e) of a source value (§6, §6.1).
    fn read_source(&self, idx: usize, s: u64, e: u64) -> Result<Vec<u8>, Error> {
        let re = |m: String| Error::Resolution(m);
        let src = &self.sources[idx];
        match src.kind.as_ref().unwrap() {
            SourceKind::Data(d) => {
                if (d.len() as u64) < e {
                    return Err(re(format!("data source {} is shorter than {}", idx, e)));
                }
                Ok(d[s as usize..e as usize].to_vec())
            }
            SourceKind::Key(k) => {
                if k == SOURCES_KEY || k == INDEX_KEY {
                    return Err(re(format!("key source {:?} names a format entry", k)));
                }
                let info = match self.lookup(k) {
                    Ok(Lookup::Entry(i)) => i,
                    Ok(_) => return Err(re(format!("key source {:?} is missing", k))),
                    Err(err) => return Err(re(format!("key source {:?}: {}", k, err.message()))),
                };
                match info {
                    EntryInfo::Reference { .. } => Err(re(format!("key source {:?} is a reference entry", k))),
                    EntryInfo::Bytes { method, body_offset, csize, usize_ } => {
                        if usize_ < e {
                            // Still report body errors first if any? A short
                            // value is a resolution error either way.
                            return Err(re(format!("key source {:?} has {} bytes, shorter than {}", k, usize_, e)));
                        }
                        self.body_window(method, body_offset, csize, usize_, s, e)
                            .map_err(|err| re(format!("key source {:?}: {}", k, err.message())))
                    }
                }
            }
            SourceKind::Url(u) => {
                let r = uri::parse_uri_reference(u)
                    .ok_or_else(|| re(format!("source {} url {:?} is not a valid URI reference", idx, u)))?;
                let target = uri::resolve(&self.base, &r);
                let scheme = target.scheme.as_deref().unwrap_or("").to_ascii_lowercase();
                let pins = http::Pins {
                    size: src.size,
                    etag: src.etag.clone(),
                    modified_not_after: src.modified_not_after,
                };
                match scheme.as_str() {
                    "file" => {
                        let path = uri::file_uri_to_path(&target).map_err(re)?;
                        read_file_source(&path, s, e, &pins).map_err(re)
                    }
                    "http" => http::fetch_range(&target, s, e, &pins).map_err(re),
                    other => Err(re(format!("unsupported URL scheme {:?}", other))),
                }
            }
        }
    }
}

fn read_file_source(path: &Path, s: u64, e: u64, pins: &http::Pins) -> Result<Vec<u8>, String> {
    use std::os::unix::fs::MetadataExt;
    let f = File::open(path).map_err(|err| format!("cannot open {}: {}", path.display(), err))?;
    let meta = f.metadata().map_err(|err| format!("cannot stat {}: {}", path.display(), err))?;
    if !meta.is_file() {
        return Err(format!("{} is not a regular file", path.display()));
    }
    if pins.etag.is_some() {
        return Err("etag pin cannot be checked on a file: URL".into());
    }
    if let Some(sz) = pins.size {
        if meta.len() != sz {
            return Err(format!("size pin failed: file has {} bytes, pinned {}", meta.len(), sz));
        }
    }
    if let Some(t) = pins.modified_not_after {
        // st_mtime is already rounded towards negative infinity (nsec >= 0).
        let m = meta.mtime();
        if m > t {
            return Err(format!("modified_not_after pin failed: mtime {} > {}", m, t));
        }
    }
    if meta.len() < e {
        return Err(format!("file has {} bytes, shorter than {}", meta.len(), e));
    }
    let mut buf = vec![0u8; (e - s) as usize];
    f.read_exact_at(&mut buf, s).map_err(|err| format!("read failed: {}", err))?;
    Ok(buf)
}
