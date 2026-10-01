//! vzip reader (SPEC §3, §4, §7, §8).

use crate::proto::{self, CdIndex, Range, Source, SourceKind};
use crate::uri;
use std::cell::RefCell;
use std::collections::HashMap;
use std::fs::File;
use std::os::unix::fs::{FileExt, MetadataExt};
use std::path::Path;

pub const SOURCES_KEY: &str = "__vz__/sources";
pub const INDEX_KEY: &str = "__vz__/index";
pub const HIDDEN_PREFIX: &str = "__vz__/";
pub const MAGIC: &[u8] = b"vzip/1";

/// Bound on the bytes a single request may produce (SPEC §10). Exceeding
/// it is a request error.
pub const MAX_REQUEST_BYTES: u64 = 1 << 30;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Archive(String),
    Entry(String),
    Body(String),
    Payload(String),
    Resolution(String),
    Request(String),
}

impl Error {
    pub fn class(&self) -> &'static str {
        match self {
            Error::Archive(_) => "archive",
            Error::Entry(_) => "entry",
            Error::Body(_) => "body",
            Error::Payload(_) => "payload",
            Error::Resolution(_) => "resolution",
            Error::Request(_) => "request",
        }
    }
    pub fn message(&self) -> &str {
        match self {
            Error::Archive(m)
            | Error::Entry(m)
            | Error::Body(m)
            | Error::Payload(m)
            | Error::Resolution(m)
            | Error::Request(m) => m,
        }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} error: {}", self.class(), self.message())
    }
}

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Bytes,
    Reference,
    Missing,
}

#[derive(Debug, Clone, Copy)]
pub enum Request {
    Whole,
    Range(u64, u64),
    Offset(u64),
    Suffix(u64),
}

impl Request {
    fn window(&self, n: u64) -> (u64, u64) {
        match *self {
            Request::Whole => (0, n),
            Request::Range(s, e) => (s.min(n), e.min(n)),
            Request::Offset(s) => (s.min(n), n),
            Request::Suffix(c) => (n - c.min(n), n),
        }
    }
}

fn le16(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}
fn le32(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn le64(b: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

/// A central directory record, as parsed (not yet interpreted).
#[derive(Debug, Clone)]
pub struct CdRecord {
    pub name: Vec<u8>,
    pub flags: u16,
    pub method: u16,
    pub csize: u32,
    pub usize: u32,
    pub lho: u32,
    pub extra: Vec<u8>,
}

/// Parse a byte slice that must be exactly a sequence of whole central
/// directory records.
pub fn parse_records(buf: &[u8]) -> std::result::Result<Vec<CdRecord>, String> {
    let mut out = Vec::new();
    let mut p = 0usize;
    while p < buf.len() {
        if buf.len() - p < 46 {
            return Err(format!("truncated central directory record at {p}"));
        }
        if le32(buf, p) != 0x02014b50 {
            return Err(format!("bad central directory record signature at {p}"));
        }
        let nlen = le16(buf, p + 28) as usize;
        let xlen = le16(buf, p + 30) as usize;
        let clen = le16(buf, p + 32) as usize;
        let total = 46 + nlen + xlen + clen;
        if buf.len() - p < total {
            return Err(format!("central directory record at {p} overruns its region"));
        }
        out.push(CdRecord {
            name: buf[p + 46..p + 46 + nlen].to_vec(),
            flags: le16(buf, p + 8),
            method: le16(buf, p + 10),
            csize: le32(buf, p + 20),
            usize: le32(buf, p + 24),
            lho: le32(buf, p + 42),
            extra: buf[p + 46 + nlen..p + 46 + nlen + xlen].to_vec(),
        });
        p += total;
    }
    Ok(out)
}

/// The interpreted location and kind of an entry.
#[derive(Debug, Clone)]
pub struct EntryInfo {
    pub reference: Option<(u16, Vec<u8>)>,
    pub method: u16,
    pub csize: u64,
    pub usize: u64,
    /// Body offset, or None if it overflows.
    pub body_offset: Option<u64>,
}

/// Interpret a record per §4.1/§8.4; failures are entry errors.
pub fn interpret_record(r: &CdRecord) -> Result<EntryInfo> {
    let x = &r.extra;
    let mut p = 0usize;
    let mut refs = Vec::new();
    let mut zip64: Option<&[u8]> = None;
    while p < x.len() {
        if x.len() - p < 4 {
            return Err(Error::Entry("extra field does not parse".into()));
        }
        let id = le16(x, p);
        let sz = le16(x, p + 2) as usize;
        if x.len() - p - 4 < sz {
            return Err(Error::Entry("extra field does not parse".into()));
        }
        let data = &x[p + 4..p + 4 + sz];
        if id == 0x7A76 || id == 0x7A77 {
            refs.push((id, data.to_vec()));
        } else if id == 0x0001 && zip64.is_none() {
            zip64 = Some(data);
        }
        p += 4 + sz;
    }
    if refs.len() > 1 {
        return Err(Error::Entry("more than one reference extra block".into()));
    }
    if r.flags & 1 != 0 {
        return Err(Error::Entry("entry is encrypted (bit 0 set)".into()));
    }
    if r.method != 0 && r.method != 8 {
        return Err(Error::Entry(format!("unsupported compression method {}", r.method)));
    }
    let reference = refs.pop();
    if reference.is_some() && r.method == 8 {
        return Err(Error::Entry("reference entry uses method 8".into()));
    }
    // ZIP64 extended information (APPNOTE 4.5.3): fields in fixed order,
    // present only for the header fields that are all ones.
    let mut usize = r.usize as u64;
    let mut csize = r.csize as u64;
    let mut lho = r.lho as u64;
    let mut zp = 0usize;
    let mut take = |what: &str| -> Result<u64> {
        let z = zip64.ok_or_else(|| Error::Entry(format!("{what} is 0xFFFFFFFF but no ZIP64 extra block")))?;
        if z.len() < zp + 8 {
            return Err(Error::Entry(format!("ZIP64 extra block too short for {what}")));
        }
        let v = le64(z, zp);
        zp += 8;
        Ok(v)
    };
    if r.usize == 0xFFFF_FFFF {
        usize = take("uncompressed size")?;
    }
    if r.csize == 0xFFFF_FFFF {
        csize = take("compressed size")?;
    }
    if r.lho == 0xFFFF_FFFF {
        lho = take("local header offset")?;
    }
    let body_offset = lho.checked_add(30 + r.name.len() as u64);
    Ok(EntryInfo { reference, method: r.method, csize, usize, body_offset })
}

/// Inflate a raw DEFLATE body, requiring it to be a single complete stream
/// that ends exactly at the end of `input` (§8.1). `max_out` bounds the
/// output; `expected` (if any) must equal the output size.
pub fn inflate_clean(input: &[u8], expected: Option<u64>) -> Result<Vec<u8>> {
    use flate2::{Decompress, FlushDecompress, Status};
    let mut d = Decompress::new(false);
    let cap = expected.map(|e| e.saturating_add(1)).unwrap_or(u64::MAX);
    let mut out: Vec<u8> = Vec::with_capacity(expected.unwrap_or(1024).min(1 << 24) as usize + 1);
    loop {
        if out.len() == out.capacity() {
            out.reserve(out.capacity().max(4096));
        }
        let (ti, to) = (d.total_in(), d.total_out());
        let st = d
            .decompress_vec(&input[ti as usize..], &mut out, FlushDecompress::None)
            .map_err(|e| Error::Body(format!("invalid DEFLATE data: {e}")))?;
        if out.len() as u64 > cap {
            return Err(Error::Body("DEFLATE body inflates to more than the declared size".into()));
        }
        if out.len() as u64 > MAX_REQUEST_BYTES {
            return Err(Error::Request(format!(
                "inflated body exceeds the reader's limit of {MAX_REQUEST_BYTES} bytes"
            )));
        }
        match st {
            Status::StreamEnd => break,
            _ => {
                if d.total_in() == ti && d.total_out() == to && out.len() < out.capacity() {
                    return Err(Error::Body("truncated DEFLATE stream".into()));
                }
            }
        }
    }
    if d.total_in() as usize != input.len() {
        return Err(Error::Body("trailing bytes after the DEFLATE stream".into()));
    }
    if let Some(e) = expected {
        if out.len() as u64 != e {
            return Err(Error::Body(format!(
                "DEFLATE body inflates to {} bytes, record says {e}",
                out.len()
            )));
        }
    }
    Ok(out)
}

enum Lookup {
    Missing,
    Found(EntryInfo),
}

pub struct Archive {
    file: File,
    file_size: u64,
    base_uri: uri::Uri,
    cd_offset: u64,
    cd_size: u64,
    sources: Vec<Source>,
    sources_loc: (u64, u64),
    index_loc: Option<(u64, u64)>,
    index: Option<CdIndex>,
    /// Unpaged archives: all records with valid names, first occurrence wins.
    records: Option<Vec<(String, CdRecord)>>,
    record_map: HashMap<String, usize>,
    page_cache: RefCell<HashMap<usize, std::result::Result<std::rc::Rc<Vec<CdRecord>>, String>>>,
}

fn arch(m: impl Into<String>) -> Error {
    Error::Archive(m.into())
}

impl Archive {
    pub fn open_path(path: &Path) -> Result<Archive> {
        let base = uri::base_uri_for_path(path).map_err(arch)?;
        let base_uri = uri::parse_uri_reference(&base).map_err(arch)?;
        let file = File::open(path).map_err(|e| arch(format!("cannot open {}: {e}", path.display())))?;
        Self::open(file, base_uri)
    }

    fn read_at(&self, off: u64, len: u64) -> std::io::Result<Vec<u8>> {
        read_file_at(&self.file, off, len)
    }

    pub fn open(file: File, base_uri: uri::Uri) -> Result<Archive> {
        let file_size = file.metadata().map_err(|e| arch(format!("cannot stat archive: {e}")))?.len();
        let rd = |off: u64, len: u64| -> Result<Vec<u8>> {
            read_file_at(&file, off, len).map_err(|e| arch(format!("cannot read archive: {e}")))
        };
        // §3.4: locate the end of central directory record.
        let mut eocd = None;
        if file_size >= 60 {
            let b = rd(file_size - 60, 22)?;
            if le32(&b, 0) == 0x06054b50 && le16(&b, 20) == 38 {
                eocd = Some(file_size - 60);
            }
        }
        if eocd.is_none() && file_size >= 44 {
            let b = rd(file_size - 44, 22)?;
            if le32(&b, 0) == 0x06054b50 && le16(&b, 20) == 22 {
                eocd = Some(file_size - 44);
            }
        }
        let eocd_pos = eocd.ok_or_else(|| arch("no vzip end of central directory record"))?;
        let rec = rd(eocd_pos, file_size - eocd_pos)?;
        let comment = &rec[22..];
        if &comment[..6] != MAGIC {
            return Err(arch("archive comment does not start with vzip/1"));
        }
        let sources_loc = (le64(comment, 6), le64(comment, 14));
        let index_loc = if comment.len() == 38 {
            Some((le64(comment, 22), le64(comment, 30)))
        } else {
            None
        };
        // §3.2: ZIP64.
        let n_disk = le16(&rec, 8);
        let n_total = le16(&rec, 10);
        let mut cd_size = le32(&rec, 12) as u64;
        let mut cd_offset = le32(&rec, 16) as u64;
        if n_disk == 0xFFFF || n_total == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_offset == 0xFFFF_FFFF {
            if eocd_pos < 20 {
                return Err(arch("zip64 end of central directory locator missing"));
            }
            let loc = rd(eocd_pos - 20, 20)?;
            if le32(&loc, 0) != 0x07064b50 {
                return Err(arch("zip64 end of central directory locator missing"));
            }
            let z_off = le64(&loc, 8);
            if z_off.checked_add(56).map_or(true, |e| e > file_size) {
                return Err(arch("zip64 end of central directory record lies outside the file"));
            }
            let z = rd(z_off, 56)?;
            if le32(&z, 0) != 0x06064b50 {
                return Err(arch("bad zip64 end of central directory record signature"));
            }
            if le64(&z, 4) != 44 {
                return Err(arch("zip64 end of central directory record size is not 44"));
            }
            cd_size = le64(&z, 40);
            cd_offset = le64(&z, 48);
        }
        if cd_offset.checked_add(cd_size).map_or(true, |e| e > file_size) {
            return Err(arch("central directory lies outside the file"));
        }
        // Format entries.
        let read_format = |(off, size): (u64, u64), what: &str| -> Result<Vec<u8>> {
            if off.checked_add(size).map_or(true, |e| e > file_size) {
                return Err(arch(format!("{what} body lies outside the file")));
            }
            let body = rd(off, size)?;
            inflate_clean(&body, None).map_err(|e| arch(format!("{what}: {}", e.message())))
        };
        let st = read_format(sources_loc, SOURCES_KEY)?;
        let sources = proto::decode_source_table(&st).map_err(|e| arch(format!("invalid source table: {e}")))?;
        let mut index = None;
        if let Some(il) = index_loc {
            let ib = read_format(il, INDEX_KEY)?;
            let idx = proto::decode_index(&ib).map_err(|e| arch(format!("malformed page index: {e}")))?;
            let mut expect = 0u64;
            for (i, p) in idx.pages.iter().enumerate() {
                if p.length == 0 {
                    return Err(arch(format!("page {i} has length 0")));
                }
                if p.offset != expect {
                    return Err(arch(format!("page {i} is not contiguous")));
                }
                let end = p.offset.checked_add(p.length).ok_or_else(|| arch("page overflows"))?;
                if end > cd_size {
                    return Err(arch(format!("page {i} lies outside the central directory")));
                }
                if i > 0 && idx.pages[i - 1].first_key.as_bytes() >= p.first_key.as_bytes() {
                    return Err(arch("page first_key values do not strictly increase"));
                }
                expect = end;
            }
            let mut seen = std::collections::HashSet::new();
            for p in &idx.pinned {
                if !seen.insert(p.key.clone()) {
                    return Err(arch(format!("pinned key {:?} listed twice", p.key)));
                }
                if p.key == SOURCES_KEY || p.key == INDEX_KEY {
                    return Err(arch("format entry is pinned"));
                }
                if p.method != 0 && p.method != 8 {
                    return Err(arch(format!("pinned method {} is not 0 or 8", p.method)));
                }
                if p.data_offset.checked_add(p.csize).map_or(true, |e| e > file_size) {
                    return Err(arch(format!("pinned body of {:?} lies outside the file", p.key)));
                }
            }
            index = Some(idx);
        }
        let mut records = None;
        let mut record_map = HashMap::new();
        if index.is_none() {
            let cd = rd(cd_offset, cd_size)?;
            let recs = parse_records(&cd).map_err(|e| arch(format!("central directory: {e}")))?;
            let mut v = Vec::new();
            for r in recs {
                if r.name == INDEX_KEY.as_bytes() {
                    return Err(arch("__vz__/index entry in an archive without a page index"));
                }
                if r.name.is_empty() {
                    continue;
                }
                if let Ok(name) = String::from_utf8(r.name.clone()) {
                    if !record_map.contains_key(&name) {
                        record_map.insert(name.clone(), v.len());
                        v.push((name, r));
                    }
                }
            }
            records = Some(v);
        }
        Ok(Archive {
            file,
            file_size,
            base_uri,
            cd_offset,
            cd_size,
            sources,
            sources_loc,
            index_loc,
            index,
            records,
            record_map,
            page_cache: RefCell::new(HashMap::new()),
        })
    }

    pub fn sources(&self) -> &[Source] {
        &self.sources
    }

    fn page_records(&self, i: usize) -> Result<std::rc::Rc<Vec<CdRecord>>> {
        if let Some(r) = self.page_cache.borrow().get(&i) {
            return r.clone().map_err(Error::Entry);
        }
        let p = &self.index.as_ref().unwrap().pages[i];
        let res = match self.read_at(self.cd_offset + p.offset, p.length) {
            Err(e) => Err(format!("cannot read page {i}: {e}")),
            Ok(buf) => parse_records(&buf)
                .map(std::rc::Rc::new)
                .map_err(|e| format!("page {i} cannot be parsed: {e}")),
        };
        self.page_cache.borrow_mut().insert(i, res.clone());
        res.map_err(Error::Entry)
    }

    /// Index of the page that lookup of `k` selects (§7.2).
    fn select_page(&self, k: &[u8]) -> Option<usize> {
        let pages = &self.index.as_ref()?.pages;
        let n = pages.partition_point(|p| p.first_key.as_bytes() <= k);
        if n == 0 { None } else { Some(n - 1) }
    }

    /// Look up a key's entry (hidden keys allowed; format entries excluded).
    fn lookup(&self, k: &str) -> Result<Lookup> {
        if let Some(idx) = &self.index {
            if let Some(p) = idx.pinned.iter().find(|p| p.key == k) {
                return Ok(Lookup::Found(EntryInfo {
                    reference: None,
                    method: p.method as u16,
                    csize: p.csize,
                    usize: p.size,
                    body_offset: Some(p.data_offset),
                }));
            }
            let Some(pi) = self.select_page(k.as_bytes()) else { return Ok(Lookup::Missing) };
            let recs = self.page_records(pi)?;
            match recs.iter().find(|r| r.name == k.as_bytes()) {
                None => Ok(Lookup::Missing),
                Some(r) => Ok(Lookup::Found(interpret_record(r)?)),
            }
        } else {
            match self.record_map.get(k) {
                None => Ok(Lookup::Missing),
                Some(&i) => Ok(Lookup::Found(interpret_record(&self.records.as_ref().unwrap()[i].1)?)),
            }
        }
    }

    pub fn classify(&self, k: &str) -> Result<Kind> {
        if k.starts_with(HIDDEN_PREFIX) {
            return Ok(Kind::Missing);
        }
        Ok(match self.lookup(k)? {
            Lookup::Missing => Kind::Missing,
            Lookup::Found(e) if e.reference.is_some() => Kind::Reference,
            Lookup::Found(_) => Kind::Bytes,
        })
    }

    /// Read `[a, b)` of an entry's (inflated) body; errors are body errors.
    fn body_window(&self, e: &EntryInfo, a: u64, b: u64) -> Result<Vec<u8>> {
        let off = e.body_offset.ok_or_else(|| Error::Body("body offset overflows".into()))?;
        if off.checked_add(e.csize).map_or(true, |end| end > self.file_size) {
            return Err(Error::Body("body lies outside the file".into()));
        }
        if e.method == 0 {
            if e.csize != e.usize {
                return Err(Error::Body("STORED entry's compressed and uncompressed sizes differ".into()));
            }
            let (a, b) = (a.min(e.csize), b.min(e.csize));
            if b - a > MAX_REQUEST_BYTES {
                return Err(Error::Request("request exceeds the reader's limit".into()));
            }
            self.read_at(off + a, b - a).map_err(|err| Error::Body(format!("cannot read body: {err}")))
        } else {
            if e.csize > MAX_REQUEST_BYTES {
                return Err(Error::Request("compressed body exceeds the reader's limit".into()));
            }
            let raw = self.read_at(off, e.csize).map_err(|err| Error::Body(format!("cannot read body: {err}")))?;
            let full = inflate_clean(&raw, Some(e.usize))?;
            let (a, b) = (a.min(full.len() as u64) as usize, b.min(full.len() as u64) as usize);
            Ok(full[a..b].to_vec())
        }
    }

    fn format_raw(&self, (off, size): (u64, u64)) -> Result<Vec<u8>> {
        let body = self.read_at(off, size).map_err(|e| Error::Body(format!("cannot read body: {e}")))?;
        inflate_clean(&body, None)
    }

    pub fn raw(&self, k: &str) -> Result<Option<Vec<u8>>> {
        if k == SOURCES_KEY {
            return self.format_raw(self.sources_loc).map(Some);
        }
        if k == INDEX_KEY {
            return match self.index_loc {
                Some(l) => self.format_raw(l).map(Some),
                None => Ok(None),
            };
        }
        match self.lookup(k)? {
            Lookup::Missing => Ok(None),
            Lookup::Found(e) => self.body_window(&e, 0, u64::MAX).map(Some),
        }
    }

    pub fn get(&self, k: &str, req: Request) -> Result<Option<Vec<u8>>> {
        if let Request::Range(s, e) = req {
            if s > e {
                return Err(Error::Request(format!("range start {s} > end {e}")));
            }
        }
        if k.starts_with(HIDDEN_PREFIX) {
            return Ok(None);
        }
        let e = match self.lookup(k)? {
            Lookup::Missing => return Ok(None),
            Lookup::Found(e) => e,
        };
        let Some((id, payload)) = &e.reference else {
            let (a, b) = req.window(e.usize);
            return self.body_window(&e, a, b).map(Some);
        };
        let ns = self.sources.len();
        let parts = if *id == 0x7A76 {
            vec![Range::decode(payload, ns).map_err(Error::Payload)?]
        } else {
            proto::decode_concat(payload, ns).map_err(Error::Payload)?
        };
        let n: u64 = parts.iter().map(|p| p.size()).sum();
        let (a, b) = req.window(n);
        let mut out = Vec::new();
        let mut start = 0u64;
        for p in &parts {
            let sz = p.size();
            let (ps, pe) = (start, start + sz);
            start = pe;
            let (oa, ob) = (a.max(ps), b.min(pe));
            if oa >= ob {
                continue;
            }
            let (i, j) = (oa - ps, ob - ps);
            match &p.data {
                Some(d) => out.extend_from_slice(&d[i as usize..j as usize]),
                None => {
                    let bytes = self.read_source(p.source as usize, p.offset + i, p.offset + j)?;
                    out.extend_from_slice(&bytes);
                }
            }
            if out.len() as u64 > MAX_REQUEST_BYTES {
                return Err(Error::Request(format!(
                    "request exceeds the reader's limit of {MAX_REQUEST_BYTES} bytes"
                )));
            }
        }
        Ok(Some(out))
    }

    /// Read `[a, b)` of a source value (§6, §8.3 step 3); errors are
    /// resolution errors (or request errors for the resource limit).
    fn read_source(&self, si: usize, a: u64, b: u64) -> Result<Vec<u8>> {
        let s = &self.sources[si];
        let res = |m: String| Error::Resolution(format!("source {si}: {m}"));
        match &s.kind {
            SourceKind::Data(d) => {
                if b > d.len() as u64 {
                    return Err(res(format!("data source has {} bytes, range needs {b}", d.len())));
                }
                Ok(d[a as usize..b as usize].to_vec())
            }
            SourceKind::Key(k) => {
                if k == SOURCES_KEY || k == INDEX_KEY {
                    return Err(res(format!("key source names format entry {k:?}")));
                }
                let e = match self.lookup(k) {
                    Err(e) => return Err(res(format!("key {k:?}: {e}"))),
                    Ok(Lookup::Missing) => return Err(res(format!("key {k:?} is missing"))),
                    Ok(Lookup::Found(e)) => e,
                };
                if e.reference.is_some() {
                    return Err(res(format!("key {k:?} is a reference entry")));
                }
                if b > e.usize {
                    return Err(res(format!("key {k:?} has {} bytes, range needs {b}", e.usize)));
                }
                if b - a > MAX_REQUEST_BYTES {
                    return Err(Error::Request("request exceeds the reader's limit".into()));
                }
                match self.body_window(&e, a, b) {
                    Ok(v) => Ok(v),
                    Err(Error::Request(m)) => Err(Error::Request(m)),
                    Err(e) => Err(res(format!("key {k:?}: {e}"))),
                }
            }
            SourceKind::Url(u) => {
                let r = uri::parse_uri_reference(u).map_err(|e| res(format!("invalid URL {u:?}: {e}")))?;
                let t = uri::resolve(&self.base_uri, &r);
                let scheme = t.scheme.clone().unwrap_or_default();
                if !scheme.eq_ignore_ascii_case("file") {
                    return Err(res(format!("unsupported URL scheme {scheme:?}")));
                }
                let path = uri::file_uri_to_path(&t).map_err(res)?;
                let f = File::open(&path).map_err(|e| res(format!("cannot open {}: {e}", path.display())))?;
                let md = f.metadata().map_err(|e| res(format!("cannot stat {}: {e}", path.display())))?;
                if let Some(sz) = s.size {
                    if md.len() != sz {
                        return Err(res(format!("size pin failed: file has {} bytes, pin says {sz}", md.len())));
                    }
                }
                if s.etag.is_some() {
                    return Err(res("etag pin cannot be checked for file: URLs".into()));
                }
                if let Some(m) = s.modified_not_after {
                    if md.mtime() > m {
                        return Err(res(format!(
                            "modified_not_after pin failed: mtime {} > {m}",
                            md.mtime()
                        )));
                    }
                }
                if b > md.len() {
                    return Err(res(format!("object has {} bytes, range needs {b}", md.len())));
                }
                if b - a > MAX_REQUEST_BYTES {
                    return Err(Error::Request("request exceeds the reader's limit".into()));
                }
                read_file_at(&f, a, b - a).map_err(|e| res(format!("cannot read {}: {e}", path.display())))
            }
        }
    }

    pub fn list(&self, prefix: &str) -> Result<Vec<String>> {
        let pre = prefix.as_bytes();
        let mut keys: Vec<String> = Vec::new();
        let keep = |k: &str| !k.starts_with(HIDDEN_PREFIX) && k.as_bytes().starts_with(pre);
        if let Some(idx) = &self.index {
            for p in &idx.pinned {
                if keep(&p.key) {
                    keys.push(p.key.clone());
                }
            }
            let upper = prefix_upper(pre);
            for i in 0..idx.pages.len() {
                let lo = idx.pages[i].first_key.as_bytes();
                let hi = idx.pages.get(i + 1).map(|p| p.first_key.as_bytes());
                // page key range [lo, hi) intersects [pre, upper)?
                let below_upper = match &upper {
                    Some(u) => lo < u.as_slice(),
                    None => true,
                };
                let above_pre = match hi {
                    Some(h) => h > pre,
                    None => true,
                };
                if !(below_upper && above_pre) {
                    continue;
                }
                let recs = self.page_records(i)?;
                for r in recs.iter() {
                    if r.name.is_empty() {
                        continue;
                    }
                    let Ok(name) = std::str::from_utf8(&r.name) else { continue };
                    if !keep(name) {
                        continue;
                    }
                    if self.select_page(name.as_bytes()) != Some(i) {
                        continue;
                    }
                    keys.push(name.to_string());
                }
            }
        } else {
            for (name, _) in self.records.as_ref().unwrap() {
                if keep(name) {
                    keys.push(name.clone());
                }
            }
        }
        keys.sort_unstable_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
        keys.dedup();
        Ok(keys)
    }

    pub fn file_size(&self) -> u64 {
        self.file_size
    }
    pub fn cd_range(&self) -> (u64, u64) {
        (self.cd_offset, self.cd_size)
    }
}

/// Smallest byte string greater than every string with prefix `p`, or None
/// if unbounded.
fn prefix_upper(p: &[u8]) -> Option<Vec<u8>> {
    let mut v = p.to_vec();
    while let Some(&last) = v.last() {
        if last == 0xff {
            v.pop();
        } else {
            *v.last_mut().unwrap() += 1;
            return Some(v);
        }
    }
    None
}

fn read_file_at(f: &File, off: u64, len: u64) -> std::io::Result<Vec<u8>> {
    let len = usize::try_from(len).map_err(|_| std::io::Error::other("length too large"))?;
    let mut buf = vec![0u8; len];
    f.read_exact_at(&mut buf, off)?;
    Ok(buf)
}
