//! vzip reader (§3, §4, §7, §8).

use crate::proto::{self, CdIndex, Range, Source, SourceKind};
use crate::uri::{self, Uri};
use crate::{inflate_clean, is_strong_etag, Error, ErrorClass, INDEX_KEY, MEMORY_LIMIT, RESERVED_PREFIX, SOURCES_KEY};
use std::collections::{BTreeSet, HashMap};
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Bytes,
    Reference,
    Missing,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Request {
    Whole,
    Range(u64, u64),
    Offset(u64),
    Suffix(u64),
}

impl Request {
    /// The window `[a, b)` of a value of size `n` (§8.2).
    pub fn window(self, n: u64) -> (u64, u64) {
        match self {
            Request::Whole => (0, n),
            Request::Range(s, e) => (s.min(n), e.min(n)),
            Request::Offset(s) => (s.min(n), n),
            Request::Suffix(c) => (n.saturating_sub(c), n),
        }
    }
}

fn err(c: ErrorClass, m: impl Into<String>) -> Error {
    Error::new(c, m)
}
fn archive(m: impl Into<String>) -> Error {
    err(ErrorClass::Archive, m)
}

fn u16le(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}
fn u32le(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn u64le(b: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

/// A central directory record, as stored.
#[derive(Debug, Clone)]
struct Record {
    name: Vec<u8>,
    flags: u16,
    method: u16,
    csize: u32,
    usize: u32,
    lho: u32,
    extra: Vec<u8>,
}

/// Parses a sequence of whole central directory records exactly filling `buf`.
fn parse_records(buf: &[u8]) -> Result<Vec<Record>, String> {
    let mut v = Vec::new();
    let mut i = 0usize;
    while i < buf.len() {
        if buf.len() - i < 46 {
            return Err(format!("truncated central directory record at {i}"));
        }
        if u32le(buf, i) != 0x02014b50 {
            return Err(format!("bad central directory signature at {i}"));
        }
        let n = u16le(buf, i + 28) as usize;
        let m = u16le(buf, i + 30) as usize;
        let k = u16le(buf, i + 32) as usize;
        let end = i + 46 + n + m + k;
        if end > buf.len() {
            return Err(format!("central directory record at {i} extends past its region"));
        }
        v.push(Record {
            name: buf[i + 46..i + 46 + n].to_vec(),
            flags: u16le(buf, i + 8),
            method: u16le(buf, i + 10),
            csize: u32le(buf, i + 20),
            usize: u32le(buf, i + 24),
            lho: u32le(buf, i + 42),
            extra: buf[i + 46 + n..i + 46 + n + m].to_vec(),
        });
        i = end;
    }
    Ok(v)
}

fn valid_name(n: &[u8]) -> bool {
    !n.is_empty() && std::str::from_utf8(n).is_ok()
}

#[derive(Debug, Clone, Copy)]
struct Body {
    off: u64,
    csize: u64,
    usize: u64,
    method: u16,
}

#[derive(Debug, Clone)]
enum Entry {
    Bytes(Body),
    Reference { id: u16, payload: Vec<u8>, body: Body },
}

/// Classifies a record (§4.1, §3.2, §8.4 entry errors).
fn analyze(r: &Record) -> Result<Entry, Error> {
    let e = |m: String| err(ErrorClass::Entry, m);
    if r.flags & 1 != 0 {
        return Err(e("entry is encrypted (bit 0 set)".into()));
    }
    if r.method != 0 && r.method != 8 {
        return Err(e(format!("unsupported compression method {}", r.method)));
    }
    // §3.2: both size fields 0xFFFFFFFF mark a large entry; exactly one is an error.
    let large = r.csize == 0xFFFF_FFFF && r.usize == 0xFFFF_FFFF;
    if !large && (r.csize == 0xFFFF_FFFF || r.usize == 0xFFFF_FFFF) {
        return Err(e("exactly one size field is 0xFFFFFFFF".into()));
    }
    let x = &r.extra;
    let mut i = 0;
    let mut refs: Vec<(u16, &[u8])> = Vec::new();
    let mut z64: Vec<&[u8]> = Vec::new();
    while i < x.len() {
        if x.len() - i < 4 {
            return Err(e("extra field does not parse".into()));
        }
        let id = u16le(x, i);
        let sz = u16le(x, i + 2) as usize;
        if x.len() - i - 4 < sz {
            return Err(e("extra field does not parse".into()));
        }
        let d = &x[i + 4..i + 4 + sz];
        match id {
            crate::EXTRA_RANGE | crate::EXTRA_CONCAT => refs.push((id, d)),
            0x0001 => z64.push(d),
            _ => {}
        }
        i += 4 + sz;
    }
    if z64.len() > 1 {
        return Err(e("more than one ZIP64 extra block".into()));
    }
    // §3.2: the ZIP64 block holds a large entry's sizes (uncompressed, then
    // compressed), then the offset if its field is 0xFFFFFFFF.
    let big_lho = r.lho == 0xFFFF_FFFF;
    let need = if large { 16 } else { 0 } + if big_lho { 8 } else { 0 };
    let z = match z64.first() {
        _ if need == 0 => &[][..],
        Some(d) if d.len() >= need => *d,
        _ => return Err(e("ZIP64 extra block missing or too short".into())),
    };
    let (usize, csize) = if large { (u64le(z, 0), u64le(z, 8)) } else { (r.usize as u64, r.csize as u64) };
    let lho = if big_lho { u64le(z, if large { 16 } else { 0 }) } else { r.lho as u64 };
    if refs.len() > 1 {
        return Err(e("more than one reference extra block".into()));
    }
    let body = Body {
        off: lho
            .saturating_add(30)
            .saturating_add(r.name.len() as u64)
            .saturating_add(if large { 20 } else { 0 }),
        csize,
        usize,
        method: r.method,
    };
    if large && r.method != 0 {
        return Err(e(format!("large entry uses method {}", r.method)));
    }
    match refs.first() {
        None => Ok(Entry::Bytes(body)),
        Some((id, d)) => {
            if r.method == 8 {
                return Err(e("reference entry uses method 8".into()));
            }
            if large {
                return Err(e("reference entry is large".into()));
            }
            Ok(Entry::Reference { id: *id, payload: d.to_vec(), body })
        }
    }
}

enum Store {
    Unpaged { records: Vec<Record>, by_name: HashMap<Vec<u8>, usize> },
    Paged { index: CdIndex, index_raw: Vec<u8>, pinned: HashMap<Vec<u8>, usize> },
}

pub struct Archive {
    file: File,
    size: u64,
    base: Uri,
    cd_offset: u64,

    sources: Vec<Source>,
    sources_raw: Vec<u8>,
    store: Store,
}

fn is_hidden(k: &[u8]) -> bool {
    k.starts_with(RESERVED_PREFIX)
}

impl Archive {
    /// Opens an archive from a local path (§8.1).
    pub fn open(path: impl AsRef<Path>) -> Result<Archive, Error> {
        let path = path.as_ref();
        let base = uri::file_base_uri(path).map_err(|e| archive(format!("cannot build base URI: {e}")))?;
        let file = File::open(path).map_err(|e| archive(format!("cannot open {}: {e}", path.display())))?;
        let size = file.metadata().map_err(|e| archive(format!("cannot stat: {e}")))?.len();
        Self::from_file(file, size, base)
    }

    fn read_at(file: &File, off: u64, len: u64) -> std::io::Result<Vec<u8>> {
        let mut v = vec![0u8; len as usize];
        file.read_exact_at(&mut v, off)?;
        Ok(v)
    }

    fn within(size: u64, off: u64, len: u64) -> bool {
        off.checked_add(len).is_some_and(|e| e <= size)
    }

    pub fn from_file(file: File, size: u64, base: Uri) -> Result<Archive, Error> {
        let rd = |off: u64, len: u64| Self::read_at(&file, off, len).map_err(|e| archive(format!("read failed: {e}")));
        // §3.4: locate the end of central directory record.
        let mut eocd_at = None;
        if size >= 60 {
            let b = rd(size - 60, 22)?;
            if u32le(&b, 0) == 0x06054b50 && u16le(&b, 20) == 38 {
                eocd_at = Some(size - 60);
            }
        }
        if eocd_at.is_none() && size >= 44 {
            let b = rd(size - 44, 22)?;
            if u32le(&b, 0) == 0x06054b50 && u16le(&b, 20) == 22 {
                eocd_at = Some(size - 44);
            }
        }
        let eocd_at = eocd_at.ok_or_else(|| archive("not a vzip archive: no end of central directory record with a vzip comment"))?;
        let eocd = rd(eocd_at, size - eocd_at)?;
        let comment = &eocd[22..];
        if !comment.starts_with(b"vzip/") {
            return Err(archive("not a vzip archive: comment does not start with vzip/"));
        }
        if comment[5] != b'0' {
            return Err(archive(format!("unsupported vzip version {:?}", comment[5] as char)));
        }
        let sources_loc = (u64le(comment, 6), u64le(comment, 14));
        let index_loc = if comment.len() == 38 { Some((u64le(comment, 22), u64le(comment, 30))) } else { None };

        // §3.2: the directory's size and offset always come from the zip64 record; the end
        // record's own counts, size and offset are ignored.
        if eocd_at < 20 {
            return Err(archive("zip64 end of central directory locator missing"));
        }
        let loc = rd(eocd_at - 20, 20)?;
        if u32le(&loc, 0) != 0x07064b50 {
            return Err(archive("zip64 end of central directory locator missing"));
        }
        let z_off = u64le(&loc, 8);
        if !Self::within(size, z_off, 56) {
            return Err(archive("zip64 end of central directory record lies outside the file"));
        }
        let z = rd(z_off, 56)?;
        if u32le(&z, 0) != 0x06064b50 {
            return Err(archive("bad zip64 end of central directory signature"));
        }
        if u64le(&z, 4) != 44 {
            return Err(archive("zip64 end of central directory record size is not 44"));
        }
        let (cd_size, cd_offset) = (u64le(&z, 40), u64le(&z, 48));
        if !Self::within(size, cd_offset, cd_size) {
            return Err(archive("central directory lies outside the file"));
        }

        // Format entries.
        let load = |(off, len): (u64, u64), what: &str| -> Result<Vec<u8>, Error> {
            if !Self::within(size, off, len) {
                return Err(archive(format!("{what} body lies outside the file")));
            }
            let b = rd(off, len)?;
            inflate_clean(&b, MEMORY_LIMIT).map_err(|e| archive(format!("{what}: {e}")))
        };
        let sources_raw = load(sources_loc, "__vz__/sources")?;
        let sources = proto::decode_source_table(&sources_raw)
            .map_err(|e| archive(format!("source table is malformed: {e}")))?;
        for (i, s) in sources.iter().enumerate() {
            match &s.kind {
                None => return Err(archive(format!("source {i} has no kind"))),
                Some(SourceKind::Url(u)) if u.is_empty() => return Err(archive(format!("source {i}: empty url"))),
                Some(SourceKind::Key(k)) if k.is_empty() => return Err(archive(format!("source {i}: empty key"))),
                Some(SourceKind::Url(_)) => {}
                Some(_) => {
                    if s.size.is_some() || s.etag.is_some() || s.modified_not_after.is_some() {
                        return Err(archive(format!("source {i}: pin on a non-url source")));
                    }
                }
            }
            if let Some(e) = &s.etag {
                if !is_strong_etag(e) {
                    return Err(archive(format!("source {i}: etag {e:?} is not a strong entity tag")));
                }
            }
        }

        let store = match index_loc {
            Some(loc) => {
                let index_raw = load(loc, "__vz__/index")?;
                let index = proto::decode_cd_index(&index_raw)
                    .map_err(|e| archive(format!("page index does not decode: {e}")))?;
                let mut expect = 0u64;
                for (i, p) in index.pages.iter().enumerate() {
                    if p.length == 0 {
                        return Err(archive(format!("page {i} has length 0")));
                    }
                    if !p.offset.checked_add(p.length).is_some_and(|e| e <= cd_size) {
                        return Err(archive(format!("page {i} lies outside the central directory")));
                    }
                    if p.offset != expect {
                        return Err(archive(format!("page {i} is not contiguous")));
                    }
                    expect = p.offset + p.length;
                    if p.first_key.is_empty() {
                        return Err(archive(format!("page {i} has an empty first_key")));
                    }
                    if i > 0 && index.pages[i - 1].first_key.as_bytes() >= p.first_key.as_bytes() {
                        return Err(archive("page first_key values do not strictly increase"));
                    }
                }
                let mut pinned = HashMap::new();
                for (i, p) in index.pinned.iter().enumerate() {
                    let k = p.key.as_bytes();
                    if k.is_empty() {
                        return Err(archive("pinned key is empty"));
                    }
                    if k == SOURCES_KEY || k == INDEX_KEY {
                        return Err(archive("pinned key is a format entry"));
                    }
                    if pinned.insert(k.to_vec(), i).is_some() {
                        return Err(archive(format!("pinned key {:?} listed twice", p.key)));
                    }
                    if p.method != 0 && p.method != 8 {
                        return Err(archive(format!("pinned method {} is not 0 or 8", p.method)));
                    }
                    if p.method != 0 && (p.size >= 0xFFFF_FFFF || p.csize >= 0xFFFF_FFFF) {
                        return Err(archive(format!("pinned large entry {:?} is not STORED", p.key)));
                    }
                    if !Self::within(size, p.data_offset, p.csize) {
                        return Err(archive(format!("pinned body of {:?} lies outside the file", p.key)));
                    }
                }
                Store::Paged { index, index_raw, pinned }
            }
            None => {
                let cd = rd(cd_offset, cd_size)?;
                let records = parse_records(&cd).map_err(|e| archive(format!("central directory: {e}")))?;
                let mut by_name = HashMap::new();
                for (i, r) in records.iter().enumerate() {
                    if r.name == INDEX_KEY {
                        return Err(archive("__vz__/index entry in an archive without a page index"));
                    }
                    if valid_name(&r.name) {
                        // Duplicate names are unspecified (§8.6); first record wins.
                        by_name.entry(r.name.clone()).or_insert(i);
                    }
                }
                Store::Unpaged { records, by_name }
            }
        };
        Ok(Archive { file, size, base, cd_offset, sources, sources_raw, store })
    }

    pub fn sources(&self) -> &[Source] {
        &self.sources
    }

    fn rd(&self, off: u64, len: u64, class: ErrorClass) -> Result<Vec<u8>, Error> {
        Self::read_at(&self.file, off, len).map_err(|e| err(class, format!("read failed: {e}")))
    }

    /// Reads and parses page `i`. Failure is an entry error.
    fn read_page(&self, index: &CdIndex, i: usize) -> Result<Vec<Record>, Error> {
        let p = &index.pages[i];
        let buf = self.rd(self.cd_offset + p.offset, p.length, ErrorClass::Entry)?;
        parse_records(&buf).map_err(|e| err(ErrorClass::Entry, format!("page {i} cannot be parsed: {e}")))
    }

    /// Looks up a non-format key's entry (§7.2). `Ok(None)` = missing.
    fn lookup(&self, key: &[u8]) -> Result<Option<Entry>, Error> {
        match &self.store {
            Store::Unpaged { records, by_name } => match by_name.get(key) {
                None => Ok(None),
                Some(&i) => analyze(&records[i]).map(Some),
            },
            Store::Paged { index, pinned, .. } => {
                if let Some(&i) = pinned.get(key) {
                    let p = &index.pinned[i];
                    return Ok(Some(Entry::Bytes(Body {
                        off: p.data_offset,
                        csize: p.csize,
                        usize: p.size,
                        method: p.method as u16,
                    })));
                }
                let Some(pi) = index.pages.iter().rposition(|p| p.first_key.as_bytes() <= key) else {
                    return Ok(None);
                };
                let recs = self.read_page(index, pi)?;
                match recs.iter().find(|r| r.name == key) {
                    None => Ok(None),
                    Some(r) => analyze(r).map(Some),
                }
            }
        }
    }

    /// Reads the window of a bytes entry's value (body errors, §8.4).
    fn body_value(&self, b: &Body, req: Request, raw_ref: bool) -> Result<Vec<u8>, Error> {
        let be = |m: String| err(ErrorClass::Body, m);
        if !Self::within(self.size, b.off, b.csize) {
            return Err(be("entry body lies outside the file".into()));
        }
        if b.method == 8 && !raw_ref {
            if b.usize > MEMORY_LIMIT {
                return Err(err(ErrorClass::Request, "entry exceeds the 1 GiB memory limit"));
            }
            let c = self.rd(b.off, b.csize, ErrorClass::Body)?;
            let v = inflate_clean(&c, b.usize).map_err(|e| be(format!("DEFLATE body: {e}")))?;
            if v.len() as u64 != b.usize {
                return Err(be(format!("body inflates to {} bytes, record says {}", v.len(), b.usize)));
            }
            let (a, z) = req.window(b.usize);
            Ok(v[a as usize..z as usize].to_vec())
        } else {
            if b.csize != b.usize {
                return Err(be("STORED entry's compressed and uncompressed sizes differ".into()));
            }
            let (a, z) = req.window(b.usize);
            if z - a > MEMORY_LIMIT {
                return Err(err(ErrorClass::Request, "window exceeds the 1 GiB memory limit"));
            }
            self.rd(b.off + a, z - a, ErrorClass::Body)
        }
    }

    pub fn classify(&self, key: &str) -> Result<Kind, Error> {
        let k = key.as_bytes();
        if is_hidden(k) {
            return Ok(Kind::Missing);
        }
        Ok(match self.lookup(k)? {
            None => Kind::Missing,
            Some(Entry::Bytes(_)) => Kind::Bytes,
            Some(Entry::Reference { .. }) => Kind::Reference,
        })
    }

    pub fn get(&self, key: &str, req: Request) -> Result<Option<Vec<u8>>, Error> {
        if let Request::Range(s, e) = req {
            if s > e {
                return Err(err(ErrorClass::Request, format!("range start {s} > end {e}")));
            }
        }
        let k = key.as_bytes();
        if is_hidden(k) {
            return Ok(None);
        }
        match self.lookup(k)? {
            None => Ok(None),
            Some(Entry::Bytes(b)) => self.body_value(&b, req, false).map(Some),
            Some(Entry::Reference { id, payload, .. }) => self.resolve_reference(id, &payload, req).map(Some),
        }
    }

    pub fn raw(&self, key: &str) -> Result<Option<Vec<u8>>, Error> {
        let k = key.as_bytes();
        if k == SOURCES_KEY {
            return Ok(Some(self.sources_raw.clone()));
        }
        if k == INDEX_KEY {
            return Ok(match &self.store {
                Store::Paged { index_raw, .. } => Some(index_raw.clone()),
                Store::Unpaged { .. } => None,
            });
        }
        match self.lookup(k)? {
            None => Ok(None),
            Some(Entry::Bytes(b)) => self.body_value(&b, Request::Whole, false).map(Some),
            Some(Entry::Reference { body, .. }) => self.body_value(&body, Request::Whole, true).map(Some),
        }
    }

    pub fn list(&self, prefix: &str) -> Result<Vec<String>, Error> {
        let pre = prefix.as_bytes();
        let mut out: BTreeSet<Vec<u8>> = BTreeSet::new();
        let want = |n: &[u8]| valid_name(n) && !is_hidden(n) && n.starts_with(pre);
        match &self.store {
            Store::Unpaged { records, .. } => {
                for r in records {
                    if want(&r.name) {
                        out.insert(r.name.clone());
                    }
                }
            }
            Store::Paged { index, pinned, .. } => {
                for k in pinned.keys() {
                    if want(k) {
                        out.insert(k.clone());
                    }
                }
                let pages = &index.pages;
                for i in 0..pages.len() {
                    let lo = pages[i].first_key.as_bytes();
                    let hi = pages.get(i + 1).map(|p| p.first_key.as_bytes());
                    let read = hi.is_none_or(|h| pre < h) && (lo <= pre || lo.starts_with(pre));
                    if !read {
                        continue;
                    }
                    for r in self.read_page(index, i)? {
                        let n = r.name.as_slice();
                        if want(n) && lo <= n && hi.is_none_or(|h| n < h) {
                            out.insert(r.name.clone());
                        }
                    }
                }
            }
        }
        Ok(out.into_iter().map(|k| String::from_utf8(k).unwrap()).collect())
    }

    /// §8.3.
    fn resolve_reference(&self, id: u16, payload: &[u8], req: Request) -> Result<Vec<u8>, Error> {
        let pe = |m: String| err(ErrorClass::Payload, m);
        if payload.len() > crate::MAX_PAYLOAD {
            return Err(pe(format!("reference payload is {} bytes (max 65519)", payload.len())));
        }
        let parts: Vec<Range> = if id == crate::EXTRA_RANGE {
            vec![proto::decode_range(payload).map_err(|e| pe(format!("malformed Range: {e}")))?]
        } else {
            proto::decode_concat(payload).map_err(|e| pe(format!("malformed Concat: {e}")))?
        };
        let mut total: u64 = 0;
        for (i, p) in parts.iter().enumerate() {
            if p.data.is_some() {
                if p.source != 0 || p.offset != 0 || p.length != 0 {
                    return Err(pe(format!("literal range {i} has non-zero source/offset/length")));
                }
            } else {
                if p.source as usize >= self.sources.len() {
                    return Err(pe(format!("range {i}: source {} out of bounds", p.source)));
                }
                if p.offset.checked_add(p.length).is_none() {
                    return Err(pe(format!("range {i}: offset + length exceeds 2^64-1")));
                }
            }
            total = total.checked_add(p.size()).ok_or_else(|| pe("reference size exceeds 2^64-1".into()))?;
        }
        let (a, b) = req.window(total);
        if b - a > MEMORY_LIMIT {
            return Err(err(ErrorClass::Request, "window exceeds the 1 GiB memory limit"));
        }
        let mut out = Vec::with_capacity((b - a) as usize);
        let mut pos: u64 = 0;
        for p in &parts {
            let sz = p.size();
            let (s, e) = (a.max(pos), b.min(pos + sz));
            if s < e {
                let (i, j) = (s - pos, e - pos);
                match &p.data {
                    Some(d) => out.extend_from_slice(&d[i as usize..j as usize]),
                    None => out.extend(self.read_source(p.source as usize, p.offset + i, p.offset + j)?),
                }
            }
            pos += sz;
        }
        Ok(out)
    }

    /// Reads bytes `[from, to)` of a source value (resolution errors).
    fn read_source(&self, idx: usize, from: u64, to: u64) -> Result<Vec<u8>, Error> {
        let re = |m: String| err(ErrorClass::Resolution, m);
        let s = &self.sources[idx];
        match s.kind.as_ref().unwrap() {
            SourceKind::Data(d) => {
                if to > d.len() as u64 {
                    return Err(re(format!("data source {idx} has {} bytes, need {to}", d.len())));
                }
                Ok(d[from as usize..to as usize].to_vec())
            }
            SourceKind::Key(k) => {
                let kb = k.as_bytes();
                if kb == SOURCES_KEY || kb == INDEX_KEY {
                    return Err(re(format!("key source {k:?} names a format entry")));
                }
                let e = self.lookup(kb).map_err(|e| re(format!("key source {k:?}: {e}")))?;
                match e {
                    None => Err(re(format!("key source {k:?} is missing"))),
                    Some(Entry::Reference { .. }) => Err(re(format!("key source {k:?} is a reference"))),
                    Some(Entry::Bytes(b)) => {
                        if to > b.usize {
                            return Err(re(format!("key source {k:?} has {} bytes, need {to}", b.usize)));
                        }
                        self.body_value(&b, Request::Range(from, to), false).map_err(|e| {
                            if e.class == ErrorClass::Request { e } else { re(format!("key source {k:?}: {e}")) }
                        })
                    }
                }
            }
            SourceKind::Url(u) => {
                let r = uri::parse_reference(u).map_err(re)?;
                let target = uri::resolve(&self.base, &r);
                let scheme = target.scheme.as_deref().unwrap_or("").to_ascii_lowercase();
                let pins = crate::http::Pins {
                    size: s.size,
                    etag: s.etag.as_deref(),
                    modified_not_after: s.modified_not_after,
                };
                match scheme.as_str() {
                    "file" => read_file_range(&target, from, to, &pins),
                    "http" | "https" => crate::http::read_range(&target, from, to, &pins, MEMORY_LIMIT),
                    other => Err(re(format!("unsupported URL scheme {other:?}"))),
                }
            }
        }
    }
}

fn read_file_range(u: &Uri, from: u64, to: u64, pins: &crate::http::Pins) -> Result<Vec<u8>, Error> {
    use std::os::unix::fs::MetadataExt;
    let re = |m: String| err(ErrorClass::Resolution, m);
    let path = uri::file_uri_to_path(u).map_err(re)?;
    if pins.etag.is_some() {
        return Err(re("etag pin cannot be checked for file: URLs".into()));
    }
    let f = File::open(&path).map_err(|e| re(format!("cannot open {}: {e}", path.display())))?;
    let md = f.metadata().map_err(|e| re(format!("cannot stat {}: {e}", path.display())))?;
    if !md.is_file() {
        return Err(re(format!("{} is not a regular file", path.display())));
    }
    let len = md.len();
    if let Some(p) = pins.size {
        if p != len {
            return Err(re(format!("file size {len} does not match size pin {p}")));
        }
    }
    if let Some(p) = pins.modified_not_after {
        let m = md.mtime(); // whole seconds, rounded down (st_mtim.tv_sec)
        if m > p {
            return Err(re(format!("file modified at {m}, after modified_not_after pin {p}")));
        }
    }
    if to > len {
        return Err(re(format!("{} has {len} bytes, need {to}", path.display())));
    }
    let mut v = vec![0u8; (to - from) as usize];
    f.read_exact_at(&mut v, from).map_err(|e| re(format!("read failed: {e}")))?;
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FF: u32 = 0xFFFF_FFFF;

    fn rec(csize: u32, usize: u32, lho: u32, extra: Vec<u8>) -> Record {
        Record { name: b"k".to_vec(), flags: 0x800, method: 0, csize, usize, lho, extra }
    }
    fn z64(vals: &[u64]) -> Vec<u8> {
        let mut x = vec![1, 0];
        x.extend_from_slice(&((vals.len() * 8) as u16).to_le_bytes());
        vals.iter().for_each(|v| x.extend_from_slice(&v.to_le_bytes()));
        x
    }
    fn body(r: &Record) -> (u64, u64, u64) {
        match analyze(r).unwrap() {
            Entry::Bytes(b) => (b.off, b.csize, b.usize),
            Entry::Reference { .. } => panic!("not bytes"),
        }
    }
    fn entry_err(r: Record) {
        assert_eq!(analyze(&r).unwrap_err().class, ErrorClass::Entry);
    }
    fn range_extra() -> Vec<u8> {
        let p = proto::encode_range(&Range { source: 0, offset: 0, length: 0, data: Some(b"a".to_vec()) });
        let mut x = crate::EXTRA_RANGE.to_le_bytes().to_vec();
        x.extend_from_slice(&(p.len() as u16).to_le_bytes());
        x.extend_from_slice(&p);
        x
    }

    /// §3.2 decoding: sizes and offset come from the record or its ZIP64 block.
    #[test]
    fn analyze_sizes_and_offsets() {
        let big = 5u64 << 30;
        // (record, expected (body offset, csize, usize)); name length is 1.
        let cases = [
            (rec(3, 4, 100, vec![]), (131, 3, 4)),
            (rec(FF, FF, 100, z64(&[big, big + 1])), (151, big + 1, big)),
            (rec(3, 4, FF, z64(&[big])), (big + 31, 3, 4)),
            (rec(FF, FF, FF, z64(&[big, big, 7 << 32])), ((7 << 32) + 51, big, big)),
            // bytes beyond what is needed are ignored
            (rec(3, 4, FF, z64(&[big, 9])), (big + 31, 3, 4)),
            (rec(FF, FF, 100, z64(&[big, big, 9])), (151, big, big)),
            // a block on a record that needs nothing is ignored
            (rec(3, 4, 100, z64(&[1, 2, 3])), (131, 3, 4)),
        ];
        for (r, want) in cases {
            assert_eq!(body(&r), want, "{r:?}");
        }
        // A small reference entry with a ZIP64 offset block is fine.
        let mut x = range_extra();
        x.extend(z64(&[big]));
        assert!(matches!(analyze(&rec(0, 0, FF, x)).unwrap(), Entry::Reference { .. }));
    }

    #[test]
    fn analyze_err_one_size_field_all_ones() {
        entry_err(rec(FF, 4, 100, z64(&[1, 2])));
        entry_err(rec(3, FF, 100, z64(&[1, 2])));
    }

    #[test]
    fn analyze_err_zip64_block_missing() {
        entry_err(rec(FF, FF, 100, vec![]));
        entry_err(rec(3, 4, FF, vec![]));
    }

    #[test]
    fn analyze_err_zip64_block_too_short() {
        entry_err(rec(FF, FF, 100, z64(&[1])));
        entry_err(rec(FF, FF, FF, z64(&[1, 2])));
        entry_err(rec(3, 4, FF, vec![1, 0, 4, 0, 0, 0, 0, 0]));
    }

    #[test]
    fn analyze_err_duplicate_zip64_block() {
        let mut x = z64(&[1]);
        x.extend(z64(&[1]));
        entry_err(rec(3, 4, 100, x));
    }

    #[test]
    fn analyze_err_large_deflate_entry() {
        let mut r = rec(FF, FF, 100, z64(&[1 << 32, 7]));
        r.method = 8;
        entry_err(r);
    }

    #[test]
    fn analyze_err_large_reference_entry() {
        let mut x = range_extra();
        x.extend(z64(&[1 << 32, 1 << 32]));
        entry_err(rec(FF, FF, 100, x));
    }
}
