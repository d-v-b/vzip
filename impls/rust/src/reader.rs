//! vzip reader (spec §8).

use crate::error::{Result, VzError};
use crate::http;
use crate::proto::{self, CdIndex, Pinned, Range, Source, SourceKind};
use crate::uri::{self, UriRef};
use crate::zip::*;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::Path;

/// Largest value (or window) this reader will materialise in memory: 1 GiB.
/// Requests beyond it are request errors (spec §10).
pub const MAX_VALUE: u64 = 1 << 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Bytes,
    Reference,
    Missing,
}

impl Kind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Kind::Bytes => "bytes",
            Kind::Reference => "reference",
            Kind::Missing => "missing",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Request {
    Whole,
    Range(u64, u64),
    Offset(u64),
    Suffix(u64),
}

impl Request {
    fn check(&self) -> Result<()> {
        if let Request::Range(s, e) = self {
            if s > e {
                return Err(VzError::request(format!("range start {s} > end {e}")));
            }
        }
        Ok(())
    }
    /// The window `[a, b)` of a value of size `n`.
    pub fn window(&self, n: u64) -> (u64, u64) {
        match *self {
            Request::Whole => (0, n),
            Request::Range(s, e) => (s.min(n), e.min(n)),
            Request::Offset(s) => (s.min(n), n),
            Request::Suffix(c) => (n.saturating_sub(c), n),
        }
    }
}

/// Where an entry's body lives.
#[derive(Debug, Clone, Copy)]
struct Body {
    offset: u64,
    csize: u64,
    usize: u64,
    method: u16,
}

enum EntryKind {
    Bytes,
    Reference { concat: bool, payload: Vec<u8> },
}

struct Entry {
    kind: EntryKind,
    body: Body,
}

enum Found {
    Missing,
    Format(Vec<u8>),
    Pinned(Pinned),
    Record(CdRecord),
}

pub struct Archive {
    file: File,
    file_size: u64,
    base: UriRef,
    cd_offset: u64,
    sources: Vec<Source>,
    sources_raw: Vec<u8>,
    index_raw: Option<Vec<u8>>,
    index: Option<CdIndex>,
    unpaged: Option<BTreeMap<String, CdRecord>>,
    page_cache: RefCell<HashMap<usize, std::result::Result<std::rc::Rc<Vec<CdRecord>>, String>>>,
}

enum InflateErr {
    NotClean(String),
    TooLarge,
}

fn inflate_clean(input: &[u8], limit: u64) -> std::result::Result<Vec<u8>, InflateErr> {
    use flate2::{Decompress, FlushDecompress, Status};
    let mut d = Decompress::new(false);
    let mut out: Vec<u8> = Vec::with_capacity((input.len() * 2).min(limit as usize).max(64));
    loop {
        if out.len() == out.capacity() {
            if out.len() as u64 >= limit + 1 {
                return Err(InflateErr::TooLarge);
            }
            let add = out.len().max(1 << 16).min((limit + 1 - out.len() as u64) as usize);
            out.reserve_exact(add);
        }
        let in_before = d.total_in();
        let out_before = d.total_out();
        let status = d
            .decompress_vec(&input[d.total_in() as usize..], &mut out, FlushDecompress::None)
            .map_err(|e| InflateErr::NotClean(format!("invalid DEFLATE data: {e}")))?;
        if out.len() as u64 > limit {
            return Err(InflateErr::TooLarge);
        }
        match status {
            Status::StreamEnd => break,
            _ => {
                if d.total_in() == in_before && d.total_out() == out_before && out.len() < out.capacity() {
                    return Err(InflateErr::NotClean("DEFLATE stream is truncated".into()));
                }
            }
        }
    }
    if d.total_in() != input.len() as u64 {
        return Err(InflateErr::NotClean(format!(
            "{} trailing bytes after the DEFLATE stream",
            input.len() as u64 - d.total_in()
        )));
    }
    Ok(out)
}

/// Does any string with prefix `p` fall in `[lo, hi)` (hi = None: unbounded)?
fn page_may_hold_prefix(lo: &str, hi: Option<&str>, p: &str) -> bool {
    let cand = if lo.as_bytes() <= p.as_bytes() {
        p
    } else if lo.starts_with(p) {
        lo
    } else {
        return false;
    };
    match hi {
        None => true,
        Some(h) => cand.as_bytes() < h.as_bytes(),
    }
}

impl Archive {
    /// Open a local archive; the base URI is its `file:` URI (§6).
    pub fn open_path(path: &Path) -> Result<Archive> {
        let base = uri::base_uri_for_path(path)
            .map_err(|e| VzError::archive(format!("cannot determine base URI: {e}")))?;
        Self::open_with_base(path, &base)
    }

    pub fn open_with_base(path: &Path, base: &str) -> Result<Archive> {
        let base = uri::parse_uri_reference(base)
            .ok_or_else(|| VzError::archive(format!("invalid base URI {base}")))?;
        let file = File::open(path).map_err(|e| VzError::archive(format!("cannot open {}: {e}", path.display())))?;
        let file_size = file
            .metadata()
            .map_err(|e| VzError::archive(format!("cannot stat archive: {e}")))?
            .len();
        let mut a = Archive {
            file,
            file_size,
            base,
            cd_offset: 0,
            sources: Vec::new(),
            sources_raw: Vec::new(),
            index_raw: None,
            index: None,
            unpaged: None,
            page_cache: RefCell::new(HashMap::new()),
        };
        a.open_checks()?;
        Ok(a)
    }

    fn read_exact_at(&self, off: u64, len: u64) -> std::io::Result<Vec<u8>> {
        let mut buf = vec![0u8; len as usize];
        self.file.read_exact_at(&mut buf, off)?;
        Ok(buf)
    }

    fn within_file(&self, off: u64, len: u64) -> bool {
        off.checked_add(len).map(|e| e <= self.file_size).unwrap_or(false)
    }

    fn open_checks(&mut self) -> Result<()> {
        let arch = |m: String| VzError::archive(m);
        let io = |e: std::io::Error| VzError::archive(format!("I/O error: {e}"));
        let fs = self.file_size;
        // §3.4: locate the end of central directory record.
        let mut eocd_off = None;
        if fs >= 60 {
            let b = self.read_exact_at(fs - 60, 22).map_err(io)?;
            if le32(&b, 0) == SIG_EOCD && le16(&b, 20) == 38 {
                eocd_off = Some(fs - 60);
            }
        }
        if eocd_off.is_none() && fs >= 44 {
            let b = self.read_exact_at(fs - 44, 22).map_err(io)?;
            if le32(&b, 0) == SIG_EOCD && le16(&b, 20) == 22 {
                eocd_off = Some(fs - 44);
            }
        }
        let eocd_off = eocd_off.ok_or_else(|| arch("not a vzip archive: no end of central directory record with a vzip comment".into()))?;
        let eocd = self.read_exact_at(eocd_off, fs - eocd_off).map_err(io)?;
        let comment = &eocd[22..];
        if !comment.starts_with(b"vzip/") {
            return Err(arch("not a vzip archive: comment does not start with vzip/".into()));
        }
        if comment[5] != b'0' {
            return Err(arch(format!("unsupported vzip format version {:?}", comment[5] as char)));
        }
        let sources_offset = le64(comment, 6);
        let sources_size = le64(comment, 14);
        let paged = comment.len() == 38;

        // §3.2: zip64.
        let mut n_disk = le16(&eocd, 8) as u64;
        let mut n_total = le16(&eocd, 10) as u64;
        let mut cd_size = le32(&eocd, 12) as u64;
        let mut cd_offset = le32(&eocd, 16) as u64;
        let _ = (n_disk, n_total);
        if n_disk == 0xFFFF || n_total == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_offset == 0xFFFF_FFFF {
            if eocd_off < 20 {
                return Err(arch("zip64 end of central directory locator missing".into()));
            }
            let loc = self.read_exact_at(eocd_off - 20, 20).map_err(io)?;
            if le32(&loc, 0) != SIG_ZIP64_LOC {
                return Err(arch("zip64 end of central directory locator missing".into()));
            }
            let z_off = le64(&loc, 8);
            if !self.within_file(z_off, 56) {
                return Err(arch("zip64 end of central directory record lies outside the file".into()));
            }
            let z = self.read_exact_at(z_off, 56).map_err(io)?;
            if le32(&z, 0) != SIG_ZIP64_EOCD {
                return Err(arch("bad zip64 end of central directory record signature".into()));
            }
            if le64(&z, 4) != 44 {
                return Err(arch("zip64 end of central directory record size field is not 44".into()));
            }
            n_disk = le64(&z, 24);
            n_total = le64(&z, 32);
            cd_size = le64(&z, 40);
            cd_offset = le64(&z, 48);
            let _ = (n_disk, n_total);
        }
        if !self.within_file(cd_offset, cd_size) {
            return Err(arch("central directory lies outside the file".into()));
        }
        self.cd_offset = cd_offset;

        // Format entries.
        let read_format = |this: &Archive, off: u64, size: u64, what: &str| -> Result<Vec<u8>> {
            if !this.within_file(off, size) {
                return Err(arch(format!("{what} body lies outside the file")));
            }
            let raw = this.read_exact_at(off, size).map_err(io)?;
            inflate_clean(&raw, MAX_VALUE).map_err(|e| match e {
                InflateErr::NotClean(m) => arch(format!("{what}: {m}")),
                InflateErr::TooLarge => arch(format!("{what} exceeds the reader's size limit")),
            })
        };
        self.sources_raw = read_format(self, sources_offset, sources_size, SOURCES_KEY)?;
        let sources = proto::decode_source_table(&self.sources_raw)
            .map_err(|m| arch(format!("malformed source table: {m}")))?;
        for (i, s) in sources.iter().enumerate() {
            match &s.kind {
                None => return Err(arch(format!("source {i} has no kind"))),
                Some(SourceKind::Url(u)) => {
                    if u.is_empty() {
                        return Err(arch(format!("source {i} has an empty url")));
                    }
                    if let Some(e) = &s.etag {
                        if !is_strong_etag(e) {
                            return Err(arch(format!("source {i} etag pin {e:?} is not a strong entity tag")));
                        }
                    }
                }
                Some(_) => {
                    if s.size.is_some() || s.etag.is_some() || s.modified_not_after.is_some() {
                        return Err(arch(format!("source {i}: pin on a key or data source")));
                    }
                }
            }
        }
        self.sources = sources;

        if paged {
            let index_offset = le64(comment, 22);
            let index_size = le64(comment, 30);
            let raw = read_format(self, index_offset, index_size, INDEX_KEY)?;
            let idx = proto::decode_cd_index(&raw).map_err(|m| arch(format!("malformed page index: {m}")))?;
            let mut expect = 0u64;
            for (i, p) in idx.pages.iter().enumerate() {
                if p.length == 0 {
                    return Err(arch(format!("page index: page {i} has length 0")));
                }
                let end = p.offset.checked_add(p.length);
                if end.map(|e| e > cd_size).unwrap_or(true) {
                    return Err(arch(format!("page index: page {i} lies outside the central directory")));
                }
                if p.offset != expect {
                    return Err(arch(format!("page index: page {i} is not contiguous")));
                }
                expect = end.unwrap();
                if p.first_key.is_empty() {
                    return Err(arch(format!("page index: page {i} has an empty first_key")));
                }
                if i > 0 && idx.pages[i - 1].first_key.as_bytes() >= p.first_key.as_bytes() {
                    return Err(arch("page index: first_key values do not strictly increase".into()));
                }
            }
            let mut seen = BTreeSet::new();
            for p in &idx.pinned {
                if p.key.is_empty() {
                    return Err(arch("page index: empty pinned key".into()));
                }
                if p.key == SOURCES_KEY || p.key == INDEX_KEY {
                    return Err(arch(format!("page index: format entry {} is pinned", p.key)));
                }
                if !seen.insert(p.key.clone()) {
                    return Err(arch(format!("page index: key {:?} pinned twice", p.key)));
                }
                if p.method != 0 && p.method != 8 {
                    return Err(arch(format!("page index: pinned method {} is not 0 or 8", p.method)));
                }
                if !self.within_file(p.data_offset, p.csize) {
                    return Err(arch(format!("page index: pinned body of {:?} lies outside the file", p.key)));
                }
            }
            self.index_raw = Some(raw);
            self.index = Some(idx);
        } else {
            let cd = self.read_exact_at(cd_offset, cd_size).map_err(io)?;
            let recs = parse_records(&cd).map_err(|m| arch(format!("central directory: {m}")))?;
            let mut map = BTreeMap::new();
            for r in recs {
                if r.name == INDEX_KEY.as_bytes() {
                    return Err(arch("archive without a page index has an __vz__/index entry".into()));
                }
                if let Some(k) = r.key() {
                    let k = k.to_string();
                    map.entry(k).or_insert(r);
                }
            }
            self.unpaged = Some(map);
        }
        Ok(())
    }

    fn is_format_key(&self, key: &str) -> bool {
        key == SOURCES_KEY || (self.index.is_some() && key == INDEX_KEY)
    }

    fn load_page(&self, i: usize) -> std::result::Result<std::rc::Rc<Vec<CdRecord>>, String> {
        if let Some(r) = self.page_cache.borrow().get(&i) {
            return r.clone();
        }
        let p = &self.index.as_ref().unwrap().pages[i];
        let res = self
            .read_exact_at(self.cd_offset + p.offset, p.length)
            .map_err(|e| format!("I/O error: {e}"))
            .and_then(|b| parse_records(&b))
            .map(std::rc::Rc::new)
            .map_err(|m| format!("page {i} cannot be parsed: {m}"));
        self.page_cache.borrow_mut().insert(i, res.clone());
        res
    }

    /// §7.2 / unpaged lookup. Visible view (hidden keys included).
    fn lookup(&self, key: &str) -> Result<Found> {
        if key == SOURCES_KEY {
            return Ok(Found::Format(self.sources_raw.clone()));
        }
        if let Some(idx) = &self.index {
            if key == INDEX_KEY {
                return Ok(Found::Format(self.index_raw.clone().unwrap()));
            }
            if let Some(p) = idx.pinned.iter().find(|p| p.key == key) {
                return Ok(Found::Pinned(p.clone()));
            }
            let pi = match idx.pages.iter().rposition(|p| p.first_key.as_bytes() <= key.as_bytes()) {
                Some(i) => i,
                None => return Ok(Found::Missing),
            };
            let recs = self.load_page(pi).map_err(VzError::entry)?;
            for r in recs.iter() {
                if r.name == key.as_bytes() {
                    return Ok(Found::Record(r.clone()));
                }
            }
            Ok(Found::Missing)
        } else {
            match self.unpaged.as_ref().unwrap().get(key) {
                Some(r) => Ok(Found::Record(r.clone())),
                None => Ok(Found::Missing),
            }
        }
    }

    /// Entry errors of a record (§8.4).
    fn analyze(rec: &CdRecord) -> Result<Entry> {
        let blocks = parse_extra(&rec.extra).ok_or_else(|| VzError::entry("extra field does not parse"))?;
        let refs: Vec<_> = blocks.iter().filter(|(id, _)| *id == ID_RANGE || *id == ID_CONCAT).collect();
        if refs.len() > 1 {
            return Err(VzError::entry("more than one reference block"));
        }
        if rec.method != 0 && rec.method != 8 {
            return Err(VzError::entry(format!("unsupported compression method {}", rec.method)));
        }
        if rec.flags & 1 != 0 {
            return Err(VzError::entry("entry is encrypted"));
        }
        let kind = match refs.first() {
            Some((id, data)) => {
                if rec.method == 8 {
                    return Err(VzError::entry("reference entry uses method 8"));
                }
                EntryKind::Reference { concat: *id == ID_CONCAT, payload: data.to_vec() }
            }
            None => EntryKind::Bytes,
        };
        let mut usize = rec.usize as u64;
        let mut csize = rec.csize as u64;
        let mut lho = rec.lho as u64;
        if rec.lho == 0xFFFF_FFFF {
            let z: Vec<_> = blocks.iter().filter(|(id, _)| *id == ID_ZIP64).collect();
            if z.is_empty() {
                return Err(VzError::entry("local header offset is 0xFFFFFFFF but there is no ZIP64 block"));
            }
            if z.len() > 1 {
                return Err(VzError::entry("more than one ZIP64 extra block"));
            }
            let data = z[0].1;
            let need = 8 * (1 + (rec.usize == 0xFFFF_FFFF) as usize + (rec.csize == 0xFFFF_FFFF) as usize);
            if data.len() < need {
                return Err(VzError::entry("ZIP64 extra block too short"));
            }
            let mut p = 0;
            if rec.usize == 0xFFFF_FFFF {
                usize = le64(data, p);
                p += 8;
            }
            if rec.csize == 0xFFFF_FFFF {
                csize = le64(data, p);
                p += 8;
            }
            lho = le64(data, p);
        }
        let offset = lho
            .checked_add(30 + rec.name.len() as u64)
            .ok_or_else(|| VzError::body("body offset overflows"))?;
        Ok(Entry { kind, body: Body { offset, csize, usize, method: rec.method } })
    }

    /// Read the body window `[a, b)` (of the uncompressed value), with body checks.
    /// Returns body errors (or request errors for resource limits).
    fn read_body(&self, body: &Body, window: Option<(u64, u64)>) -> Result<Vec<u8>> {
        if !self.within_file(body.offset, body.csize) {
            return Err(VzError::body("entry body lies outside the file"));
        }
        if body.method == 8 {
            if body.usize > MAX_VALUE {
                return Err(VzError::request(format!("value of {} bytes exceeds the reader's limit", body.usize)));
            }
            let raw = self
                .read_exact_at(body.offset, body.csize)
                .map_err(|e| VzError::body(format!("I/O error: {e}")))?;
            let out = inflate_clean(&raw, body.usize).map_err(|e| match e {
                InflateErr::NotClean(m) => VzError::body(m),
                InflateErr::TooLarge => VzError::body("inflated size exceeds the record's uncompressed size"),
            })?;
            if out.len() as u64 != body.usize {
                return Err(VzError::body(format!(
                    "inflated to {} bytes, record says {}",
                    out.len(),
                    body.usize
                )));
            }
            Ok(match window {
                None => out,
                Some((a, b)) => out[a as usize..b as usize].to_vec(),
            })
        } else {
            if body.csize != body.usize {
                return Err(VzError::body("STORED entry's compressed and uncompressed sizes differ"));
            }
            let (a, b) = window.unwrap_or((0, body.csize));
            if b - a > MAX_VALUE {
                return Err(VzError::request(format!("window of {} bytes exceeds the reader's limit", b - a)));
            }
            self.read_exact_at(body.offset + a, b - a)
                .map_err(|e| VzError::body(format!("I/O error: {e}")))
        }
    }

    fn pinned_body(p: &Pinned) -> Body {
        Body { offset: p.data_offset, csize: p.csize, usize: p.size, method: p.method as u16 }
    }

    // ------------------------------------------------------------------
    // Operations (§8.2)

    pub fn classify(&self, key: &str) -> Result<Kind> {
        if is_hidden(key) {
            return Ok(Kind::Missing);
        }
        match self.lookup(key)? {
            Found::Missing => Ok(Kind::Missing),
            Found::Format(_) => Ok(Kind::Missing),
            Found::Pinned(_) => Ok(Kind::Bytes),
            Found::Record(r) => Ok(match Self::analyze(&r)?.kind {
                EntryKind::Bytes => Kind::Bytes,
                EntryKind::Reference { .. } => Kind::Reference,
            }),
        }
    }

    pub fn get(&self, key: &str, req: Request) -> Result<Option<Vec<u8>>> {
        req.check()?;
        if is_hidden(key) {
            return Ok(None);
        }
        let entry = match self.lookup(key)? {
            Found::Missing | Found::Format(_) => return Ok(None),
            Found::Pinned(p) => Entry { kind: EntryKind::Bytes, body: Self::pinned_body(&p) },
            Found::Record(r) => Self::analyze(&r)?,
        };
        match entry.kind {
            EntryKind::Bytes => {
                let n = entry.body.usize;
                let (a, b) = req.window(n);
                self.read_body(&entry.body, Some((a, b))).map(Some)
            }
            EntryKind::Reference { concat, payload } => {
                let parts = proto::decode_payload(&payload, concat, self.sources.len())
                    .map_err(|m| VzError::payload(format!("malformed reference payload: {m}")))?;
                let n: u64 = parts.iter().map(|p| p.size()).sum();
                let (a, b) = req.window(n);
                if b - a > MAX_VALUE {
                    return Err(VzError::request(format!("window of {} bytes exceeds the reader's limit", b - a)));
                }
                self.resolve_parts(&parts, a, b).map(Some)
            }
        }
    }

    pub fn raw(&self, key: &str) -> Result<Option<Vec<u8>>> {
        match self.lookup(key)? {
            Found::Missing => Ok(None),
            Found::Format(b) => Ok(Some(b)),
            Found::Pinned(p) => self.read_body(&Self::pinned_body(&p), None).map(Some),
            Found::Record(r) => {
                let e = Self::analyze(&r)?;
                self.read_body(&e.body, None).map(Some)
            }
        }
    }

    pub fn list(&self, prefix: &str) -> Result<Vec<String>> {
        let mut out = BTreeSet::new();
        let ok = |k: &str| k.starts_with(prefix) && !is_hidden(k);
        if let Some(idx) = &self.index {
            for p in &idx.pinned {
                if ok(&p.key) {
                    out.insert(p.key.clone());
                }
            }
            for (i, p) in idx.pages.iter().enumerate() {
                let hi = idx.pages.get(i + 1).map(|q| q.first_key.as_str());
                if !page_may_hold_prefix(&p.first_key, hi, prefix) {
                    continue;
                }
                let recs = self.load_page(i).map_err(VzError::entry)?;
                for r in recs.iter() {
                    if let Some(k) = r.key() {
                        let in_range = k.as_bytes() >= p.first_key.as_bytes()
                            && hi.map(|h| k.as_bytes() < h.as_bytes()).unwrap_or(true);
                        if in_range && ok(k) && !self.is_format_key(k) {
                            out.insert(k.to_string());
                        }
                    }
                }
            }
        } else {
            for k in self.unpaged.as_ref().unwrap().keys() {
                if ok(k) {
                    out.insert(k.clone());
                }
            }
        }
        Ok(out.into_iter().collect())
    }

    // ------------------------------------------------------------------
    // Resolution (§8.3)

    fn resolve_parts(&self, parts: &[Range], a: u64, b: u64) -> Result<Vec<u8>> {
        let mut out = Vec::with_capacity((b - a) as usize);
        let mut pos: u64 = 0;
        for p in parts {
            let size = p.size();
            let (ps, pe) = (pos, pos + size);
            pos = pe;
            let (os, oe) = (ps.max(a), pe.min(b));
            if os >= oe {
                continue;
            }
            let (i, j) = (os - ps, oe - ps);
            match &p.data {
                Some(d) => out.extend_from_slice(&d[i as usize..j as usize]),
                None => {
                    let bytes = self.read_source(p.source as usize, p.offset + i, p.offset + j)?;
                    out.extend_from_slice(&bytes);
                }
            }
        }
        Ok(out)
    }

    /// Bytes `[s, e)` (s < e) of source `idx`'s value.
    fn read_source(&self, idx: usize, s: u64, e: u64) -> Result<Vec<u8>> {
        let src = &self.sources[idx];
        let res = |m: String| VzError::resolution(format!("source {idx}: {m}"));
        match src.kind.as_ref().unwrap() {
            SourceKind::Data(d) => {
                if e > d.len() as u64 {
                    return Err(res(format!("data source is {} bytes, range needs {e}", d.len())));
                }
                Ok(d[s as usize..e as usize].to_vec())
            }
            SourceKind::Key(k) => {
                if self.is_format_key(k) {
                    return Err(res(format!("key source names format entry {k}")));
                }
                let body = match self.lookup(k).map_err(|er| res(er.message))? {
                    Found::Missing | Found::Format(_) => return Err(res(format!("key {k:?} is missing"))),
                    Found::Pinned(p) => Self::pinned_body(&p),
                    Found::Record(r) => {
                        let ent = Self::analyze(&r).map_err(|er| res(format!("key {k:?}: {}", er.message)))?;
                        match ent.kind {
                            EntryKind::Reference { .. } => {
                                return Err(res(format!("key {k:?} is a reference entry")))
                            }
                            EntryKind::Bytes => ent.body,
                        }
                    }
                };
                if e > body.usize {
                    // Still check the body first? Body errors and shortness are both
                    // resolution errors here, so the order does not matter.
                    return Err(res(format!("key {k:?} value is {} bytes, range needs {e}", body.usize)));
                }
                self.read_body(&body, Some((s, e))).map_err(|er| res(format!("key {k:?}: {}", er.message)))
            }
            SourceKind::Url(u) => {
                let r = uri::parse_uri_reference(u)
                    .ok_or_else(|| res(format!("{u:?} is not a valid URI reference")))?;
                let target = uri::resolve(&self.base, &r);
                let pins = http::Pins {
                    size: src.size,
                    etag: src.etag.clone(),
                    modified_not_after: src.modified_not_after,
                };
                match target.scheme_lower().as_deref() {
                    Some("file") => {
                        let path = uri::file_uri_to_path(&target).map_err(res)?;
                        read_file_range(&path, s, e, &pins).map_err(res)
                    }
                    Some("http") | Some("https") => http::read_range(&target, s, e, &pins).map_err(res),
                    other => Err(res(format!("unsupported URL scheme {other:?}"))),
                }
            }
        }
    }
}

fn read_file_range(path: &Path, s: u64, e: u64, pins: &http::Pins) -> std::result::Result<Vec<u8>, String> {
    if pins.etag.is_some() {
        return Err("etag pin cannot be checked for a file: URL".into());
    }
    let f = File::open(path).map_err(|er| format!("cannot open {}: {er}", path.display()))?;
    let md = f.metadata().map_err(|er| format!("cannot stat {}: {er}", path.display()))?;
    if !md.is_file() {
        return Err(format!("{} is not a regular file", path.display()));
    }
    let size = md.len();
    if let Some(p) = pins.size {
        if p != size {
            return Err(format!("size pin failed: expected {p}, file is {size} bytes"));
        }
    }
    if let Some(p) = pins.modified_not_after {
        use std::os::unix::fs::MetadataExt;
        // mtime() is already floored seconds (mtime_nsec is non-negative).
        let m = md.mtime();
        if m > p {
            return Err(format!("modified_not_after pin failed: file modified at {m}, pin {p}"));
        }
    }
    if e > size {
        return Err(format!("{} is {size} bytes, range needs {e}", path.display()));
    }
    let mut buf = vec![0u8; (e - s) as usize];
    f.read_exact_at(&mut buf, s).map_err(|er| format!("read failed: {er}"))?;
    Ok(buf)
}

/// `DQUOTE *etagc DQUOTE`, etagc = %x21 / %x23-7E.
pub fn is_strong_etag(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() >= 2
        && b[0] == b'"'
        && b[b.len() - 1] == b'"'
        && b[1..b.len() - 1].iter().all(|&c| c == 0x21 || (0x23..=0x7e).contains(&c))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prefix_pages() {
        assert!(page_may_hold_prefix("a", Some("c"), "b"));
        assert!(!page_may_hold_prefix("a", Some("b"), "b"));
        assert!(page_may_hold_prefix("a", Some("b0"), "b"));
        assert!(page_may_hold_prefix("ba", None, "b"));
        assert!(!page_may_hold_prefix("c", None, "b"));
        assert!(page_may_hold_prefix("c", None, ""));
        assert!(is_strong_etag("\"abc\""));
        assert!(is_strong_etag("\"\""));
        assert!(!is_strong_etag("W/\"abc\""));
        assert!(!is_strong_etag("\"a b\""));
        assert!(!is_strong_etag("\""));
    }
}

impl std::fmt::Debug for Archive {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Archive")
            .field("file_size", &self.file_size)
            .field("base", &self.base.to_string())
            .field("sources", &self.sources.len())
            .field("paged", &self.index.is_some())
            .finish()
    }
}
