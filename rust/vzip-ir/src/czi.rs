//! A sans-IO parser of Zeiss CZI files into the IR (spec/virtualize.md §13,
//! spec/virtualize/czi.md): it never reads; it asks for batches of ranges (`step`), is
//! given their bytes (`feed`), and holds them only until the step that uses them.
//!
//! Rounds: (1) the file header; (2) the headers of the directory, the metadata
//! segment and the attachment directory; (3) the directory's entries (in windows
//! of up to 16 MiB), the metadata XML and the attachment entries; (4) the
//! subblocks, in groups: their headers, then their codec headers (growing within
//! the first 2^16 bytes of their data until each gives its coded size); (5) the
//! attachments' headers, then the data of those the IR describes by form; (6) the
//! walk over the segments nothing references, a window at a time. The schema
//! (`schema/czi.json`) is checked as the profile meets each role: a file that
//! fails is rejected there, and nowhere else. The layout (series, levels, tiles,
//! row bands) is derived at the end, and checked there.

use crate::check;
use crate::coding::{self, Head};
use crate::cxml;
use crate::fed::{Fed, i32le, i64le};
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, Vars, flag, num, text};
use serde_json::{Value as J, json};
use std::collections::{BTreeMap, HashMap};

const HEADER: &str = "{id:cstr[16],allocated_size:<i8,used_size:<i8}";
const FILE_HEADER: &str = "{major:<i4,minor:<i4,reserved:bytes[8],primary_file_guid:guid,file_guid:guid,file_part:<i4,directory_position:<i8,metadata_position:<i8,update_pending:<i4,attachment_directory_position:<i8}";
const ENTRY: &str = "{schema:ascii[2],pixel_type:<i4,file_position:<i8,file_part:<i4,compression:<i4,pyramid_type:u1,spare:bytes[5],dimension_count:<i4}";
const DIMENSION: &str = "{dimension:ascii[4],start:<i4,size:<i4,start_coordinate:<f4,stored_size:<i4}";
const ATT_ENTRY: &str = "{schema:ascii[2],spare:bytes[10],file_position:<i8,file_part:<i4,content_guid:guid,content_file_type:cstr[8],name:cstr[80]}";
const EVENT: &str = "{size:<i4,time:<f8,type:<i4,description_size:<i4}";
const DIR_WINDOW: u64 = 1 << 24;
const GROUP: usize = 16384; // subblocks a round reads the headers of
const LETTERS: &[u8] = b"XYZCTRSIHVBM";
const SERIES: &[u8] = b"SBHIRV";
const FIRST_HEAD: u64 = 1024; // the first bytes of a JPEG or JPEG XR subblock's data read for its codec header
const ZSTD_HEAD: u64 = 32;
const SEGMENT_HEAD: u64 = 2048; // the bytes read at a compressed subblock's segment: its header, and often its codec header

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    FileHeader,
    Heads,
    Bodies,
    Directory,
    SubHeads,
    SubLong,
    SubCodec,
    AttHeads,
    AttData,
    Walk,
    Done,
}

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    File,
    Directory,
    Subblock,
    Metadata,
    AttDir,
    Attachment,
}

impl Kind {
    fn id(&self) -> &'static str {
        match self {
            Kind::File => "ZISRAWFILE",
            Kind::Directory => "ZISRAWDIRECTORY",
            Kind::Subblock => "ZISRAWSUBBLOCK",
            Kind::Metadata => "ZISRAWMETADATA",
            Kind::AttDir => "ZISRAWATTDIR",
            Kind::Attachment => "ZISRAWATTACH",
        }
    }
}

/// A segment read: its struct, header fields, and what is known of it.
#[derive(Clone)]
struct Seg {
    el: u32,
    id: String,
    allocated: i64,
    used: i64,
}

#[derive(Clone, Default)]
struct Entry {
    pos: i64,
    pt: i32,
    comp: i32,
    /// (letter, start, size, stored)
    dims: Vec<(u8, i32, i32, i32)>,
    el: u32,
    dims_el: Option<u32>,
}

impl Entry {
    fn dim(&self, l: u8) -> Option<(i32, i32, i32)> {
        self.dims.iter().find(|d| d.0 == l).map(|d| (d.1, d.2, d.3))
    }
}

#[derive(Clone, Default)]
struct Sub {
    /// the subblock's struct, when this entry read it (not an alias)
    el: Option<u32>,
    length: u64,
    m: i64,
    a: i64,
    n: i64,
    agrees: bool,
    /// (coded width, coded height, hi-lo, header length)
    coded: Option<(u64, u64, bool, u64)>,
    data_el: Option<u32>,
    head: Vec<u8>,
    asked: u64,
    resolved: bool,
}

struct Placed {
    index: usize,
    series: Vec<(u8, i64)>,
    plane: (i64, i64, i64),
    x: i64,
    y: i64,
    wl: i64,
    hl: i64,
    w: i64,
    h: i64,
    cw: u64,
    ch: u64,
    form: (i32, i32, bool),
    layer: Option<(u32, u32)>,
    el: u32,
}

impl Placed {
    fn conforming(&self) -> bool {
        self.cw as i64 == self.w && self.ch as i64 == self.h
    }
}

struct Level {
    layer: (u32, u32),
    form: (i32, i32, bool),
    factor: i64,
    tile: (i64, i64),
    edge: (i64, i64),
    origin: (i64, i64),
    grid: (i64, i64),
    cells: Vec<(usize, i64, i64)>, // (placed, column, row)
}

pub struct Czi {
    size: u64,
    phase: Phase,
    fed: Fed,
    pub ir: Ir,
    rules: &'static Rules,
    fields: Option<(i32, i32, Vec<u8>, Vec<u8>, i32, i64, i64, i32, i64)>,
    segs: HashMap<u64, Seg>,
    dir: Option<(u64, u32, u64, i64)>, // (position, struct, end of its used bytes, count)
    dir_at: u64,
    entries: Vec<Entry>,
    subs: Vec<Sub>,
    group: (usize, usize),
    meta: Option<(u64, u32, i64, i64)>, // (position, struct, XmlSize, AttachmentSize)
    values: cxml::Values,
    att: Option<(u64, u32, i64)>,
    /// A1 attachment entries: (k, position, content file type)
    a1: Vec<(usize, i64, Vec<u8>)>,
    a1_asked: bool,
    att_pending: Vec<(u32, u64, u64, Vec<u8>, u64)>,
    /// subblock metadata to hold once fed: (value, offset, length)
    text_pending: Vec<(u32, u64, u64)>,
    /// the metadata segment's XML value
    meta_xml: Option<u32>,
    walk_at: u64,
    walk_count: u64,
    walk_k: u64,
    walk_prev: Option<u32>,
    window: u64,
    facts: Option<J>,
    /// data forms by (pixel type, compression, hi-lo, coded width, coded height)
    forms: HashMap<(i32, i32, bool, u64, u64), String>,
    pub requests: u64,
    pub requested: u64,
    pub rounds: u64,
    pub held_max: u64,
}

/// A segment's id as text: its bytes up to the first NUL when the rest are all NUL
/// (then it can equal an id of spec/virtualize/czi.md §2.1), else all 16 bytes, escaped.
fn seg_id(h: &[u8]) -> String {
    let n = h.iter().position(|&c| c == 0).unwrap_or(16);
    if h[n..16].iter().all(|&c| c == 0) {
        String::from_utf8_lossy(&h[..n]).into_owned()
    } else {
        h[..16].escape_ascii().to_string()
    }
}

/// `[A-Z0-9_]{1,16}\0*` over the 16 id bytes.
fn walkable_id(id: &[u8]) -> bool {
    let n = id.iter().position(|&c| c == 0).unwrap_or(16);
    n >= 1 && id[..n].iter().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || *c == b'_') && id[n..].iter().all(|&c| c == 0)
}

fn guid(b: &[u8]) -> String {
    let a = u32::from_le_bytes(b[0..4].try_into().unwrap());
    let b1 = u16::from_le_bytes(b[4..6].try_into().unwrap());
    let c = u16::from_le_bytes(b[6..8].try_into().unwrap());
    let hex = |x: &[u8]| x.iter().map(|v| format!("{v:02x}")).collect::<String>();
    format!("{a:08x}-{b1:04x}-{c:04x}-{}-{}", hex(&b[8..10]), hex(&b[10..16]))
}

impl Czi {
    pub fn new(size: u64) -> Self {
        Czi {
            size,
            phase: Phase::Start,
            fed: Fed::default(),
            ir: Ir::new(size),
            rules: rules::czi(),
            fields: None,
            segs: HashMap::new(),
            dir: None,
            dir_at: 0,
            entries: Vec::new(),
            subs: Vec::new(),
            group: (0, 0),
            meta: None,
            values: cxml::Values::default(),
            att: None,
            a1: Vec::new(),
            a1_asked: false,
            att_pending: Vec::new(),
            text_pending: Vec::new(),
            meta_xml: None,
            walk_at: 0,
            walk_count: 0,
            walk_k: 0,
            walk_prev: None,
            window: 1 << 16,
            facts: None,
            forms: HashMap::new(),
            requests: 0,
            requested: 0,
            rounds: 0,
            held_max: 0,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
        self.held_max = self.held_max.max(self.fed.held());
    }

    fn ask(&mut self, mut r: Vec<(u64, u64)>) -> Step {
        r.retain(|x| x.1 > 0);
        r.sort_unstable();
        r.dedup();
        self.requests += r.len() as u64;
        self.requested += r.iter().map(|x| x.1).sum::<u64>();
        self.rounds += 1;
        Step::Read(r)
    }

    fn check(&self, role: &str, mut env: Vars<'static>) -> Res<()> {
        self.rules.check(role, &mut env)
    }

    fn within(&self, o: i64, n: i64) -> bool {
        o >= 0 && n >= 0 && (o as u64).checked_add(n as u64).is_some_and(|e| e <= self.size)
    }

    fn within_check(&self, what: &str, o: i64, n: i64) -> Res<()> {
        self.check("within", rules::env([("what", text(what)), ("within", flag(self.within(o, n)))]))
    }

    /// The segment at `pos` in the role `what` (an alias when read before): checks
    /// its position, header and sizes. Its 32-byte header must be fed (or known).
    fn segment(&mut self, parent: u32, name: &str, nidx: u64, what: &str, kind: Kind, pos: i64, header_len: i64) -> Res<(u32, bool)> {
        let known = if pos >= 0 { self.segs.get(&(pos as u64)).cloned() } else { None };
        let within = self.within(pos, header_len);
        let (id, a, u) = match &known {
            Some(s) => (s.id.clone(), s.allocated, s.used),
            None if pos >= 0 && within => {
                let h = self.fed.get(pos as u64, 32).ok_or_else(|| format!("internal: no segment header at {pos}"))?;
                (seg_id(&h[..16]), i64le(h, 16), i64le(h, 24))
            }
            None => (String::new(), 0, 0),
        };
        self.check(
            "segment",
            rules::env([
                ("what", text(what)),
                ("position", num(pos as f64)),
                ("within", flag(within)),
                ("id", text(&id)),
                ("expected", text(kind.id())),
                ("allocated", num(a as f64)),
                ("used", num(u as f64)),
            ]),
        )?;
        if let Some(s) = known {
            let e = self.ir.alias(parent, name, nidx, s.el)?;
            return Ok((e, true));
        }
        let p = pos as u64;
        let h = self.fed.get(p, 32).unwrap().to_vec();
        let env = if self.within(pos + 32, a) { 32 + a as u64 } else { 32 };
        let s = self.ir.struct_(parent, name, nidx, 0, Some((p, env)))?;
        self.ir.value(s, "header", NO_INDEX, HEADER, 0, (p, 32), Some(&h))?;
        self.segs.insert(p, Seg { el: s, id, allocated: a, used: u });
        Ok((s, false))
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::FileHeader;
                    return Ok(self.ask(vec![(0, 112.min(self.size))]));
                }
                Phase::FileHeader => {
                    let (s, _) = self.segment(0, "file_header", NO_INDEX, "the file header", Kind::File, 0, 32)?;
                    self.within_check("the file header", 32, 80)?;
                    let d = self.fed.get(32, 80).unwrap().to_vec();
                    self.ir.value(s, "fields", NO_INDEX, FILE_HEADER, 0, (32, 80), Some(&d))?;
                    let (major, minor) = (i32le(&d, 0), i32le(&d, 4));
                    let file_part = i32le(&d, 48);
                    self.check(
                        "file_header",
                        rules::env([("major", num(major)), ("minor", num(minor)), ("file_part", num(file_part))]),
                    )?;
                    let (dir, meta, upd, att) = (i64le(&d, 52), i64le(&d, 60), i32le(&d, 68), i64le(&d, 72));
                    self.fields = Some((major, minor, d[16..32].to_vec(), d[32..48].to_vec(), file_part, dir, meta, upd, att));
                    self.fed.clear();
                    // the directory's, the metadata segment's and the attachment directory's headers
                    let mut r = Vec::new();
                    for (p, n) in [(dir, 160i64), (meta, 40), (att, 36)] {
                        if p > 0 && self.within(p, 32) {
                            r.push((p as u64, (n as u64).min(self.size - p as u64)));
                        }
                    }
                    if dir == 0 && self.within(0, 160) {
                        r.push((0, 160));
                    }
                    self.phase = Phase::Heads;
                    return Ok(self.ask(r));
                }
                Phase::Heads => {
                    let (.., dir, meta, _, att) = self.fields.clone().unwrap();
                    let mut r = Vec::new();
                    // the directory
                    let (s, _) = self.segment(0, "directory", NO_INDEX, "the subblock directory", Kind::Directory, dir, 32)?;
                    self.within_check("the directory's EntryCount", dir + 32, 4)?;
                    let p = dir as u64;
                    let c = self.fed.get(p + 32, 4).unwrap().to_vec();
                    self.ir.value(s, "entry_count", NO_INDEX, "<i4", 0, (p + 32, 4), Some(&c))?;
                    let count = i32le(&c, 0) as i64;
                    self.check(
                        "entry_count",
                        rules::env([("what", text("the directory")), ("count", num(count as f64)), ("maximum", num(self.rules.limit("max_entries")))]),
                    )?;
                    let seg = &self.segs[&p];
                    let used = if seg.used != 0 { seg.used } else { seg.allocated };
                    let end = p + 32 + used as u64;
                    self.dir = Some((p, s, end, count));
                    self.dir_at = p + 160;
                    // the metadata segment
                    if meta != 0 {
                        let (m, alias) = self.segment(0, "metadata", NO_INDEX, "the metadata segment", Kind::Metadata, meta, 32)?;
                        self.within_check("the metadata segment's sizes", meta + 32, 8)?;
                        let mp = meta as u64;
                        let b = match self.fed.get(mp + 32, 8) {
                            Some(b) => b.to_vec(),
                            None => return Err("internal: the metadata segment's sizes were not read".into()),
                        };
                        let (x, ab) = (i32le(&b, 0) as i64, i32le(&b, 4) as i64);
                        self.check("metadata_sizes", rules::env([("xml", num(x as f64)), ("attachment", num(ab as f64))]))?;
                        self.within_check("the metadata segment's parts", meta + 288, x + ab)?;
                        if !alias {
                            self.ir.value(m, "sizes", NO_INDEX, "{xml_size:<i4,attachment_size:<i4}", 0, (mp + 32, 8), Some(&b))?;
                            if x > 0 {
                                let v = self.ir.value(m, "xml", NO_INDEX, &format!("ascii[{x}]"), 0, (mp + 288, x as u64), None)?;
                                self.meta_xml = Some(v);
                            }
                            if ab > 0 {
                                self.ir.value(m, "attachment", NO_INDEX, &format!("bytes[{ab}]"), 0, (mp + 288 + x as u64, ab as u64), None)?;
                            }
                        }
                        self.meta = Some((mp, m, x, ab));
                        if x > 0 && x as f64 <= self.rules.limit("max_xml") {
                            r.push((mp + 288, x as u64));
                        }
                    }
                    // the attachment directory
                    if att != 0 {
                        let (a, alias) = self.segment(0, "attachment_directory", NO_INDEX, "the attachment directory", Kind::AttDir, att, 32)?;
                        self.within_check("the attachment directory's EntryCount", att + 32, 4)?;
                        let ap = att as u64;
                        let c = self.fed.get(ap + 32, 4).ok_or("internal: no attachment count")?.to_vec();
                        let k = i32le(&c, 0) as i64;
                        self.check(
                            "entry_count",
                            rules::env([("what", text("the attachment directory")), ("count", num(k as f64)), ("maximum", num(self.rules.limit("max_attachments")))]),
                        )?;
                        self.within_check("the attachment entries", att + 288, 128 * k)?;
                        if !alias {
                            self.ir.value(a, "entry_count", NO_INDEX, "<i4", 0, (ap + 32, 4), Some(&c))?;
                        }
                        self.att = Some((ap, a, k));
                        r.push((ap + 288, 128 * k as u64));
                    }
                    // the first window of the directory's entries
                    if let Some(w) = self.dir_window() {
                        r.push(w);
                    }
                    self.fed.clear();
                    self.phase = Phase::Bodies;
                    return Ok(self.ask(r));
                }
                Phase::Bodies => {
                    // the metadata XML's values, streamed; the attachment entries
                    if let Some((mp, _, x, _)) = self.meta {
                        if x > 0 && x as f64 <= self.rules.limit("max_xml") {
                            let data = self.fed.get(mp + 288, x as u64).ok_or("internal: no XML")?;
                            self.values = cxml::values(data, self.rules.limit("max_xml") as usize);
                            // short metadata XML is shown (conventions §8.7)
                            if let (Some(v), true) = (self.meta_xml, x as u64 <= crate::mirror::VIEW_VALUE) {
                                let b = data.to_vec();
                                self.ir.put_value(v, &b);
                                self.ir.sort_values();
                            }
                        }
                    }
                    if let Some((ap, a, k)) = self.att {
                        let raw = self.fed.get(ap + 288, 128 * k as u64).ok_or("internal: no attachment entries")?.to_vec();
                        let alias = self.ir.kind[a as usize] == ALIAS;
                        for i in 0..k as usize {
                            let e = &raw[128 * i..128 * i + 128];
                            let a1 = &e[..2] == b"A1";
                            if !alias {
                                self.ir.value(a, "entries/", i as u64, if a1 { ATT_ENTRY } else { "bytes[128]" }, 0, (ap + 288 + 128 * i as u64, 128), Some(e))?;
                            }
                            if a1 {
                                let n = e[40..48].iter().position(|&c| c == 0).unwrap_or(8);
                                self.a1.push((i, i64le(e, 12), e[40..40 + n].to_vec()));
                                self.check("attachment_entry", rules::env([("k", num(i as f64)), ("file_part", num(i32le(e, 20)))]))?;
                            }
                        }
                    }
                    self.phase = Phase::Directory;
                }
                Phase::Directory => {
                    if let Some(s) = self.directory()? {
                        return Ok(s);
                    }
                    self.subs = vec![Sub::default(); self.entries.len()];
                    self.group = (0, 0);
                    self.phase = Phase::SubHeads;
                    if let Some(s) = self.next_group()? {
                        return Ok(s);
                    }
                }
                Phase::SubHeads => {
                    if let Some(s) = self.sub_heads()? {
                        return Ok(s);
                    }
                }
                Phase::SubLong => {
                    if let Some(s) = self.sub_long()? {
                        return Ok(s);
                    }
                }
                Phase::SubCodec => {
                    if let Some(s) = self.sub_codec()? {
                        return Ok(s);
                    }
                }
                Phase::AttHeads => {
                    if let Some(s) = self.att_heads()? {
                        return Ok(s);
                    }
                }
                Phase::AttData => {
                    self.att_data()?;
                    self.phase = Phase::Walk;
                }
                Phase::Walk => {
                    if let Some(s) = self.walk()? {
                        return Ok(s);
                    }
                    self.facts = Some(self.layout()?);
                    self.phase = Phase::Done;
                }
                Phase::Done => return Ok(Step::Done),
            }
        }
    }

    // ---- the directory

    fn dir_window(&self) -> Option<(u64, u64)> {
        let (_, _, end, _) = self.dir?;
        let stop = end.min(self.size);
        (self.dir_at < stop).then(|| (self.dir_at, DIR_WINDOW.min(stop - self.dir_at)))
    }

    /// Parses the entries the fed windows hold; asks for the next window, or None when done.
    fn directory(&mut self) -> Res<Option<Step>> {
        let (_, s, end, count) = self.dir.unwrap();
        while (self.entries.len() as i64) < count {
            let i = self.entries.len();
            let at = self.dir_at;
            let head = self.fed.get(at, 32).map(|b| b.to_vec());
            let ends = at + 32 <= end;
            if ends && at + 32 <= self.size && head.is_none() {
                self.fed.clear();
                let w = self.dir_window().unwrap();
                return Ok(Some(self.ask(vec![w])));
            }
            let mut env = rules::env([
                ("i", num(i as f64)),
                ("ends", flag(ends)),
                ("within", flag(at + 32 <= self.size)),
                ("schema", text("")),
                ("file_part", num(0)),
                ("pixel_type", num(-1)),
                ("compression", num(-1)),
                ("dimension_count", num(0)),
            ]);
            if let Some(h) = &head {
                env.insert("schema", text(&String::from_utf8_lossy(&h[..2])));
                env.insert("file_part", num(i32le(h, 14)));
                env.insert("pixel_type", num(i32le(h, 2)));
                env.insert("compression", num(i32le(h, 18)));
                env.insert("dimension_count", num(i32le(h, 28)));
            }
            self.rules.check("entry", &mut env)?;
            let h = head.unwrap();
            let d = i32le(&h, 28) as u64;
            let body_at = at + 32;
            let bends = body_at + 20 * d <= end;
            let body = self.fed.get(body_at, 20 * d).map(|b| b.to_vec());
            if bends && body_at + 20 * d <= self.size && body.is_none() {
                self.fed.clear();
                self.dir_at = at; // read the entry again, whole
                let w = self.dir_window().unwrap();
                return Ok(Some(self.ask(vec![w])));
            }
            self.check(
                "entry",
                rules::env([
                    ("i", num(i as f64)),
                    ("ends", flag(bends)),
                    ("within", flag(body_at + 20 * d <= self.size)),
                    ("schema", text("DV")),
                    ("file_part", num(0)),
                    ("pixel_type", num(i32le(&h, 2))),
                    ("compression", num(i32le(&h, 18))),
                    ("dimension_count", num(d as f64)),
                ]),
            )?;
            let body = body.unwrap();
            let mut dims = Vec::new();
            for k in 0..d as usize {
                let b = &body[20 * k..20 * k + 20];
                let letter = if b[1..4] == [0, 0, 0] && b[0].is_ascii_uppercase() { Some(b[0]) } else { None };
                let shown = match letter {
                    Some(l) => (l as char).to_string(),
                    None => format!("{:?}", &b[..4]),
                };
                let repeated = letter.is_some_and(|l| dims.iter().any(|x: &(u8, i32, i32, i32)| x.0 == l));
                self.check(
                    "dimension",
                    rules::env([
                        ("i", num(i as f64)),
                        ("letter", if letter.is_some_and(|l| LETTERS.contains(&l)) { text(&shown) } else { text(&format!("'{shown}'")) }),
                        ("repeated", flag(repeated)),
                        ("size", num(i32le(b, 8))),
                        ("stored", num(i32le(b, 16))),
                    ]),
                )?;
                dims.push((letter.unwrap(), i32le(b, 4), i32le(b, 8), i32le(b, 16)));
            }
            let has = |l: u8| dims.iter().any(|x| x.0 == l);
            self.check("entry_dimensions", rules::env([("i", num(i as f64)), ("x", flag(has(b'X'))), ("y", flag(has(b'Y')))]))?;
            let el = self.ir.value(s, "entries/", i as u64, ENTRY, 0, (at, 32), Some(&h))?;
            let dims_el = if d > 0 {
                Some(self.ir.value(s, "dimensions/", i as u64, &format!("{DIMENSION}[{d}]"), 0, (body_at, 20 * d), Some(&body))?)
            } else {
                None
            };
            self.entries.push(Entry { pos: i64le(&h, 6), pt: i32le(&h, 2), comp: i32le(&h, 18), dims, el, dims_el });
            self.dir_at = body_at + 20 * d;
        }
        self.fed.clear();
        Ok(None)
    }

    // ---- the subblocks, a group at a time

    /// Asks for the next group's subblock headers; None when there are no more groups.
    fn next_group(&mut self) -> Res<Option<Step>> {
        let start = self.group.1;
        if start >= self.entries.len() {
            self.phase = Phase::AttHeads;
            return Ok(None);
        }
        let end = (start + GROUP).min(self.entries.len());
        self.group = (start, end);
        let mut r = Vec::new();
        let mut asked = std::collections::HashSet::new();
        for i in start..end {
            let o = self.entries[i].pos;
            if o >= 0 && !self.segs.contains_key(&(o as u64)) && self.within(o, 288) && asked.insert(o) {
                // a compressed subblock's codec header usually follows its header and
                // metadata closely: read them in one range (as today's profile does); an
                // uncompressed one's header and its metadata when short (the view shows it)
                let n = if self.entries[i].comp == coding::UNCOMPRESSED { 320 + crate::mirror::VIEW_VALUE } else { SEGMENT_HEAD };
                r.push((o as u64, n.min(self.size - o as u64)));
            }
        }
        self.phase = Phase::SubHeads;
        Ok(Some(self.ask(r)))
    }

    /// Checks and emits the group's subblock headers; asks for the longer ones whole.
    fn sub_heads(&mut self) -> Res<Option<Step>> {
        let (g0, g1) = self.group;
        let mut long = Vec::new();
        for i in g0..g1 {
            let o = self.entries[i].pos;
            let what = format!("subblock {i}");
            let (s, alias) = self.segment(0, "subblocks/", i as u64, &what, Kind::Subblock, o, 288)?;
            let p = o as u64;
            let h = if alias {
                // the header another entry read: its fields are in the IR
                let first = self.ir.target(s);
                self.sub_fields(first)?
            } else {
                self.fed.get(p, 288).ok_or("internal: no subblock header")?.to_vec()
            };
            let (m, a, n) = (i32le(&h, 32) as i64, i32le(&h, 36) as i64, i64le(&h, 40));
            let d = i32le(&h, 76) as i64;
            self.check(
                "subblock",
                rules::env([
                    ("i", num(i as f64)),
                    ("metadata", num(m as f64)),
                    ("attachment", num(a as f64)),
                    ("data", num(n as f64)),
                    ("schema", text(&String::from_utf8_lossy(&h[48..50]))),
                    ("dimension_count", num(d as f64)),
                ]),
            )?;
            let length = 256.max(48 + 20 * d) as u64;
            self.within_check(&format!("subblock {i}'s header"), o, 32 + length as i64)?;
            self.within_check(&format!("subblock {i}'s parts"), o + 32 + length as i64, m + n + a)?;
            let sub = &mut self.subs[i];
            sub.length = length;
            sub.m = m;
            sub.a = a;
            sub.n = n;
            if !alias {
                sub.el = Some(s);
                self.ir.value(s, "sizes", NO_INDEX, "{metadata_size:<i4,attachment_size:<i4,data_size:<i8}", 0, (p + 32, 16), Some(&h[32..48]))?;
                self.ir.value(s, "entry", NO_INDEX, ENTRY, 0, (p + 48, 32), Some(&h[48..80]))?;
                // a long header, and the metadata after it when the view shows it
                let meta = if m > 0 && m as u64 <= crate::mirror::VIEW_VALUE { m as u64 } else { 0 };
                if self.fed.get(p, 32 + length + meta).is_none() && (32 + length > 288 || meta > 0) {
                    long.push((p, 32 + length + meta));
                }
            }
        }
        if !long.is_empty() {
            self.phase = Phase::SubLong;
            return Ok(Some(self.ask(long)));
        }
        self.phase = Phase::SubLong;
        self.sub_long()
    }

    /// The bytes of a subblock header another entry read (its fields, from the IR):
    /// 288 bytes whose sizes, copy and dimension count are the first reader's.
    fn sub_fields(&self, s: u32) -> Res<Vec<u8>> {
        let mut h = vec![0u8; 288];
        let kids: Vec<u32> = (s + 1..(s + 8).min(self.ir.len() as u32)).filter(|&c| self.ir.parent[c as usize] == s).collect();
        for c in kids {
            let name = self.ir.name_of(c);
            if let Some(b) = self.ir.value_bytes(c) {
                match name.as_str() {
                    "header" => h[..32].copy_from_slice(b),
                    "sizes" => h[32..48].copy_from_slice(b),
                    "entry" => h[48..80].copy_from_slice(b),
                    _ => {}
                }
            }
        }
        Ok(h)
    }

    /// Each subblock's copy of its entry, its parts, and the codec heads to read.
    fn sub_long(&mut self) -> Res<Option<Step>> {
        let (g0, g1) = self.group;
        let mut r = Vec::new();
        for i in g0..g1 {
            let o = self.entries[i].pos as u64;
            let sub = self.subs[i].clone();
            // an entry whose subblock another entry read first names it again: it is
            // not placed again (its checks were made), so its copy is not compared
            let Some(s) = sub.el else { continue };
            let d = i32le(self.fed.get(o, 80).ok_or("internal: no subblock header")?, 76) as u64;
            let copy = self.fed.get(o + 48, 32 + 20 * d).ok_or("internal: no subblock copy")?.to_vec();
            let agrees = copy == self.entry_bytes(i);
            self.subs[i].agrees = agrees;
            let d = (copy.len() as u64 - 32) / 20;
            if d > 0 {
                self.ir.value(s, "dimensions", NO_INDEX, &format!("{DIMENSION}[{d}]"), 0, (o + 80, 20 * d), Some(&copy[32..]))?;
            }
            let p = o + 32 + sub.length;
            if sub.m > 0 {
                let v = self.ir.value(s, "metadata", NO_INDEX, &format!("ascii[{}]", sub.m), 0, (p, sub.m as u64), None)?;
                // short metadata is shown (conventions §8.7): read with the codec heads
                if sub.m as u64 <= crate::mirror::VIEW_VALUE {
                    match self.fed.get(p, sub.m as u64) {
                        Some(b) => {
                            let b = b.to_vec();
                            self.ir.put_value(v, &b);
                        }
                        None => {
                            r.push((p, sub.m as u64));
                            self.text_pending.push((v, p, sub.m as u64));
                        }
                    }
                }
            }
            if sub.a > 0 {
                let at = p + sub.m as u64 + sub.n as u64;
                self.ir.value(s, "attachment", NO_INDEX, &format!("bytes[{}]", sub.a), 0, (at, sub.a as u64), None)?;
            }
            let e = &self.entries[i];
            let start = p + sub.m as u64;
            if agrees && e.comp != coding::UNCOMPRESSED && sub.n > 0 {
                let bound = (sub.n as u64).min(self.rules.limit("max_header") as u64);
                // the head the segment's first range already holds, if any
                let held = (o + SEGMENT_HEAD).min(self.size).saturating_sub(start).min(bound);
                if held > 0 {
                    if let Some(b) = self.fed.get(start, held) {
                        self.subs[i].head = b.to_vec();
                        self.subs[i].asked = held;
                        continue;
                    }
                }
                let first = if matches!(e.comp, coding::ZSTD0 | coding::ZSTD1) { ZSTD_HEAD } else { FIRST_HEAD };
                r.push((start, first.min(bound)));
                self.subs[i].asked = first.min(bound);
            }
        }
        self.fed.clear();
        self.phase = Phase::SubCodec;
        if !r.is_empty() {
            return Ok(Some(self.ask(r)));
        }
        self.sub_codec()
    }

    fn entry_bytes(&self, i: usize) -> Vec<u8> {
        let e = &self.entries[i];
        let mut out = self.ir.value_bytes(e.el).unwrap().to_vec();
        if let Some(d) = e.dims_el {
            out.extend_from_slice(self.ir.value_bytes(d).unwrap());
        }
        out
    }

    /// The codec heads: each subblock's coded size, reading more of its first
    /// 2^16 bytes until its codec header gives one (or not); then its data elements.
    fn sub_codec(&mut self) -> Res<Option<Step>> {
        for (v, p, m) in std::mem::take(&mut self.text_pending) {
            let b = self.fed.get(p, m).ok_or("internal: subblock metadata was not fed")?.to_vec();
            self.ir.put_value(v, &b);
        }
        self.ir.sort_values();
        let (g0, g1) = self.group;
        let max_header = self.rules.limit("max_header") as u64;
        let mut more = Vec::new();
        for i in g0..g1 {
            let sub = &self.subs[i];
            let e = &self.entries[i];
            if sub.resolved || sub.el.is_none() || !sub.agrees {
                continue;
            }
            let o = e.pos as u64;
            let start = o + 32 + sub.length + sub.m as u64;
            let n = sub.n as u64;
            let (Some(x), Some(y)) = (e.dim(b'X'), e.dim(b'Y')) else { continue };
            let bound = n.min(max_header);
            let mut head = std::mem::take(&mut self.subs[i].head);
            let asked = self.subs[i].asked;
            if asked > head.len() as u64 {
                head = self.fed.get(start, asked).ok_or("internal: a codec head was not fed")?.to_vec();
            }
            let h = Head { have: &head, bound };
            match coding::coded_size(e.pt, e.comp, x.2 as u64, y.2 as u64, n, &h) {
                Err(need) => {
                    let want = bound.min(need.max(2 * head.len() as u64).max(64));
                    more.push((start, want));
                    self.subs[i].head = head;
                    self.subs[i].asked = want;
                }
                Ok(c) => {
                    let sub = &mut self.subs[i];
                    sub.coded = c;
                    sub.resolved = true;
                }
            }
        }
        if !more.is_empty() {
            self.fed.clear();
            return Ok(Some(self.ask(more)));
        }
        self.fed.clear();
        // the data of each subblock this group read
        for i in g0..g1 {
            self.emit_data(i)?;
        }
        self.next_group()
    }

    fn emit_data(&mut self, i: usize) -> Res<()> {
        let sub = self.subs[i].clone();
        let Some(s) = sub.el else { return Ok(()) };
        let e = self.entries[i].clone();
        let o = e.pos as u64;
        let start = o + 32 + sub.length + sub.m as u64;
        let n = sub.n as u64;
        let placed = if sub.agrees { sub.coded } else { None };
        let Some((cw, ch, hilo, skip)) = placed else {
            if n > 0 {
                self.ir.value(s, "data", NO_INDEX, &format!("bytes[{n}]"), 0, (start, n), None)?;
            }
            return Ok(());
        };
        let (dtype, p, q) = coding::pixel_type(e.pt).unwrap();
        let length = if e.comp == coding::UNCOMPRESSED { cw * ch * q } else { n - skip };
        if e.comp == coding::ZSTD1 {
            self.ir.value(s, "zstd1_header", NO_INDEX, &format!("bytes[{skip}]"), 0, (start, skip), None)?;
        }
        let key = (e.pt, e.comp, hilo, cw, ch);
        let form = match self.forms.get(&key) {
            Some(f) => f.clone(),
            None => {
                let mut shape = vec![json!(ch), json!(cw)];
                if p > 1 {
                    shape.push(json!(p));
                }
                let f = json!({
                    "geometry": {"shape": shape, "dtype": dtype, "pixel_type": e.pt, "compression": e.comp},
                    "codec": coding::codec_chain(e.pt, e.comp, hilo),
                    "recipe": [["src", 0, null]],
                })
                .to_string();
                self.forms.insert(key, f.clone());
                f
            }
        };
        if length > 0 {
            let d = self.ir.data(s, "data", NO_INDEX, 0, (start + skip, length), &form)?;
            self.subs[i].data_el = Some(d);
        }
        if e.comp == coding::UNCOMPRESSED && n > length {
            self.ir.value(s, "trailing", NO_INDEX, &format!("bytes[{}]", n - length), 0, (start + length, n - length), None)?;
        }
        Ok(())
    }

    // ---- the attachments

    fn att_heads(&mut self) -> Res<Option<Step>> {
        if self.att.is_none() || self.a1.is_empty() {
            self.phase = Phase::AttData;
            return Ok(None);
        }
        // first visit: ask for the A1 segments' headers
        if !self.a1_asked {
            self.a1_asked = true;
            let mut r = Vec::new();
            for &(_, pos, _) in &self.a1 {
                if pos >= 0 && !self.segs.contains_key(&(pos as u64)) && self.within(pos, 288) {
                    r.push((pos as u64, 288));
                }
            }
            if !r.is_empty() {
                return Ok(Some(self.ask(r)));
            }
        }
        let (_, ad, _) = self.att.unwrap();
        let _ = ad;
        let mut data = Vec::new();
        let a1 = self.a1.clone();
        for (k, pos, kind) in a1 {
            let what = format!("attachment {k}");
            let (s, alias) = self.segment(0, "attachments/", k as u64, &what, Kind::Attachment, pos, 288)?;
            let p = pos as u64;
            let h = if alias {
                let first = self.ir.target(s);
                let mut h = vec![0u8; 288];
                for c in (first + 1..(first + 4).min(self.ir.len() as u32)).filter(|&c| self.ir.parent[c as usize] == first) {
                    if self.ir.name_of(c) == "data_size" {
                        h[32..40].copy_from_slice(self.ir.value_bytes(c).unwrap());
                    }
                }
                h
            } else {
                self.fed.get(p, 288).ok_or("internal: no attachment header")?.to_vec()
            };
            let n = i64le(&h, 32);
            self.check("attachment", rules::env([("k", num(k as f64)), ("data_size", num(n as f64))]))?;
            self.within_check(&format!("attachment {k}'s data"), pos + 288, n)?;
            if alias {
                continue;
            }
            self.ir.value(s, "data_size", NO_INDEX, "<i8", 0, (p + 32, 8), Some(&h[32..40]))?;
            let a1 = &h[48..50] == b"A1";
            self.ir.value(s, "entry", NO_INDEX, if a1 { ATT_ENTRY } else { "bytes[128]" }, 0, (p + 48, 128), Some(&h[48..176]))?;
            let at = p + 288;
            let n = n as u64;
            if n == 0 {
                continue;
            }
            let read = match kind.as_slice() {
                b"CZTIMS" | b"CZFOC" => n.min(8 + self.rules.limit("max_decode") as u64),
                b"CZEVL" if n as f64 <= self.rules.limit("max_event_bytes") => n,
                _ => 0,
            };
            data.push((s, at, n, kind, read));
        }
        self.fed.clear();
        self.att_pending = data;
        let r: Vec<(u64, u64)> = self.att_pending.iter().filter(|x| x.4 > 0).map(|x| (x.1, x.4)).collect();
        self.phase = Phase::AttData;
        if !r.is_empty() {
            return Ok(Some(self.ask(r)));
        }
        Ok(None)
    }

    /// The attachments' data by form (spec/virtualize/czi.md §5.6), for the IR.
    fn att_data(&mut self) -> Res<()> {
        let pending = std::mem::take(&mut self.att_pending);
        for (s, at, n, kind, read) in pending {
            let b = if read > 0 { self.fed.get(at, read).map(|b| b.to_vec()) } else { None };
            let max_decode = self.rules.limit("max_decode") as u64;
            if let (Some(b), true) = (&b, n >= 8) {
                let c = i32le(b, 4) as i64;
                if (kind == b"CZTIMS" || kind == b"CZFOC") && c >= 0 && n == 8 + 8 * c as u64 {
                    let d = self.ir.struct_(s, "data", NO_INDEX, 0, Some((at, n)))?;
                    self.ir.value(d, "size", NO_INDEX, "<i4", 0, (at, 4), Some(&b[..4]))?;
                    self.ir.value(d, "count", NO_INDEX, "<i4", 0, (at + 4, 4), Some(&b[4..8]))?;
                    if c > 0 {
                        let raw = (8 * c as u64 <= max_decode && b.len() as u64 >= n).then(|| &b[8..n as usize]);
                        self.ir.value(d, "values", NO_INDEX, &format!("<f8[{c}]"), 0, (at + 8, 8 * c as u64), raw)?;
                    }
                    continue;
                }
                if kind == b"CZEVL" && c >= 0 && c as f64 <= self.rules.limit("max_events") && b.len() as u64 == n && self.events(s, at, b, c as u64)? {
                    continue;
                }
            }
            if kind == b"Zip-Comp" || kind == b"ZIP" {
                self.ir.derived(s, "data", 0, (at, n), &format!("{{\"transform\":\"{}\"}}", crate::transform::Transform::Gzip.name()))?;
            } else {
                self.ir.value(s, "data", NO_INDEX, &format!("bytes[{n}]"), 0, (at, n), None)?;
            }
        }
        self.fed.clear();
        Ok(())
    }

    /// An event list, when its events fill its data exactly.
    fn events(&mut self, s: u32, at: u64, data: &[u8], c: u64) -> Res<bool> {
        let n = data.len();
        let mut found = Vec::new();
        let mut pos = 8usize;
        for _ in 0..c {
            if pos + 20 > n {
                return Ok(false);
            }
            let e = i32le(data, pos) as i64;
            let u = i32le(data, pos + 16) as i64;
            if u < 0 || e != 20 + u || pos + 20 + u as usize > n {
                return Ok(false);
            }
            found.push((pos, u as usize));
            pos += 20 + u as usize;
        }
        if pos != n {
            return Ok(false);
        }
        let d = self.ir.struct_(s, "data", NO_INDEX, 0, Some((at, n as u64)))?;
        self.ir.value(d, "size", NO_INDEX, "<i4", 0, (at, 4), Some(&data[..4]))?;
        self.ir.value(d, "count", NO_INDEX, "<i4", 0, (at + 4, 4), Some(&data[4..8]))?;
        let max_decode = self.rules.limit("max_decode") as usize;
        if c > 0 && found.iter().all(|f| f.1 == 0) {
            let raw = (20 * c as usize <= max_decode).then(|| &data[8..]);
            self.ir.value(d, "events", NO_INDEX, &format!("{EVENT}[{c}]"), 0, (at + 8, 20 * c), raw)?;
            return Ok(true);
        }
        for (k, (p, u)) in found.into_iter().enumerate() {
            self.ir.value(d, "events/", k as u64, EVENT, 0, (at + p as u64, 20), Some(&data[p..p + 20]))?;
            if u > 0 {
                self.ir.value(d, "descriptions/", k as u64, &format!("ascii[{u}]"), 0, (at + p as u64 + 20, u as u64), Some(&data[p + 20..p + 20 + u]))?;
            }
        }
        Ok(true)
    }

    // ---- the walk (spec/virtualize.md §13.2 step 6)

    /// The segments nothing references, in file order, from windows read ahead;
    /// then the tail. None when done.
    fn walk(&mut self) -> Res<Option<Step>> {
        let size = self.size;
        let max_walk = self.rules.limit("max_walk") as u64;
        loop {
            let o = self.walk_at;
            if o >= size || self.walk_count >= max_walk {
                break;
            }
            let (a, known) = match self.segs.get(&o) {
                Some(s) => (s.allocated, true),
                None => {
                    if o + 32 > size {
                        break;
                    }
                    let Some(h) = self.fed.get(o, 32).map(|b| b.to_vec()) else {
                        self.fed.clear();
                        let n = self.window.min(size - o);
                        self.window = (self.window * 2).min(1 << 24);
                        return Ok(Some(self.ask(vec![(o, n)])));
                    };
                    let (a, u) = (i64le(&h, 16), i64le(&h, 24));
                    if !walkable_id(&h[..16]) || a < 0 || u < 0 || !self.within(o as i64 + 32, a) {
                        break;
                    }
                    let k = self.walk_k;
                    let s = self.ir.struct_(0, "segments/", k, 0, Some((o, 32 + a as u64)))?;
                    self.ir.value(s, "header", NO_INDEX, HEADER, 0, (o, 32), Some(&h))?;
                    let n = if u != 0 { u.min(a) } else { a };
                    if n > 0 {
                        self.ir.value(s, "data", NO_INDEX, &format!("bytes[{n}]"), 0, (o + 32, n as u64), None)?;
                    }
                    self.walk_k += 1;
                    match self.walk_prev {
                        Some(p) if self.ir.fold(p, s) => {}
                        _ => self.walk_prev = Some(s),
                    }
                    (a, false)
                }
            };
            if known {
                self.walk_prev = None;
            }
            if a < 0 || !self.within(o as i64 + 32, a) {
                break;
            }
            self.walk_at = o + 32 + a as u64;
            self.walk_count += 1;
        }
        let o = self.walk_at.min(size);
        if size > o {
            self.ir.value(0, "tail", NO_INDEX, &format!("bytes[{}]", size - o), 0, (o, size - o), None)?;
        }
        self.fed.clear();
        Ok(None)
    }
}

// ---- the layout (spec/virtualize/czi.md §3.2–§4.4): placement, series, layers, levels, tiles

const LAYERS_2: [(f64, f64, u32); 10] = [
    (2.0, 0.1, 1), (4.0, 0.2, 2), (8.0, 0.4, 3), (16.0, 0.8, 4), (32.0, 1.0, 5), (64.0, 1.0, 6), (128.0, 1.0, 7),
    (256.0, 2.0, 8), (512.0, 4.0, 9), (1024.0, 10.0, 10),
];
const LAYERS_3: [(f64, f64, u32); 7] =
    [(3.0, 0.1, 1), (9.0, 0.2, 2), (27.0, 0.8, 3), (81.0, 1.5, 4), (243.0, 2.0, 5), (729.0, 5.0, 6), (2187.0, 15.0, 7)];

/// A subblock's layer from its logical and stored sizes (libCZI's pyramid tables).
fn layer(wl: i64, hl: i64, w: i64, h: i64) -> Option<(u32, u32)> {
    if wl == w && hl == h {
        return Some((1, 0));
    }
    let f = if w > h { wl as f64 / w as f64 } else { hl as f64 / h as f64 };
    for (table, base) in [(&LAYERS_2[..], 2), (&LAYERS_3[..], 3)] {
        for &(v, delta, n) in table {
            if v - delta <= f && f <= v + delta {
                return Some((base, n));
            }
        }
    }
    None
}

/// The level `b` is when it is regular (spec/virtualize/czi.md §3.5), else None.
fn classify(b: &[usize], ps: &[Placed]) -> Option<Level> {
    let f0 = ps[b[0]].form;
    if b.iter().any(|&i| ps[i].form != f0) {
        return None;
    }
    let wl = b.iter().map(|&i| ps[i].wl).max()?;
    let hl = b.iter().map(|&i| ps[i].hl).max()?;
    let w = b.iter().map(|&i| ps[i].w).max()?;
    let h = b.iter().map(|&i| ps[i].h).max()?;
    if wl % w != 0 || hl % h != 0 || wl / w != hl / h {
        return None;
    }
    let x0 = b.iter().map(|&i| ps[i].x).min()?;
    let y0 = b.iter().map(|&i| ps[i].y).min()?;
    let mut cells = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for &i in b {
        let s = &ps[i];
        if (s.x - x0) % wl != 0 || (s.y - y0) % hl != 0 {
            return None;
        }
        let (c, r) = ((s.x - x0) / wl, (s.y - y0) / hl);
        if !seen.insert((s.plane, c, r)) {
            return None;
        }
        cells.push((i, c, r));
    }
    let m = cells.iter().map(|c| c.1).max()? + 1;
    let r = cells.iter().map(|c| c.2).max()? + 1;
    let (mut last_col, mut last_row) = (Vec::new(), Vec::new());
    for &(i, c, j) in &cells {
        let s = &ps[i];
        if c < m - 1 && (s.wl, s.w) != (wl, w) {
            return None;
        }
        if j < r - 1 && (s.hl, s.h) != (hl, h) {
            return None;
        }
        if c == m - 1 && !last_col.contains(&(s.wl, s.w)) {
            last_col.push((s.wl, s.w));
        }
        if j == r - 1 && !last_row.contains(&(s.hl, s.h)) {
            last_row.push((s.hl, s.h));
        }
    }
    if last_col.len() != 1 || last_row.len() != 1 {
        return None;
    }
    Some(Level {
        layer: ps[b[0]].layer.unwrap(),
        form: f0,
        factor: wl / w,
        tile: (w, h),
        edge: (last_col[0].1, last_row[0].1),
        origin: (x0, y0),
        grid: (m, r),
        cells,
    })
}

fn gcd(a: u128, b: u128) -> u128 {
    if b == 0 { a } else { gcd(b, a % b) }
}

/// The rows of a band of an uncompressed tile of w × h (edge height h2) of q-byte
/// pixels: h when the tile is at most max_band bytes, else the largest divisor of
/// gcd(h, h2) whose band is at most max_band bytes, or 1.
fn row_band(h: u128, h2: u128, w: u128, q: u128, max_band: u128) -> u128 {
    if w * h * q <= max_band {
        return h;
    }
    let g = gcd(h, h2);
    let mut best = 1;
    let mut d = 1;
    while d * d <= g {
        if g % d == 0 {
            for v in [d, g / d] {
                if v * w * q <= max_band && v > best {
                    best = v;
                }
            }
        }
        d += 1;
    }
    best
}

/// The y chunk lengths of `r` tile rows of height h (the last h2), and the bands:
/// (rows a band holds, bands per tile row). Divisor bands (spec/virtualize/czi.md §4.3)
/// when they hold at least band_floor bytes; else bands of the most rows within
/// max_band bytes, the last of each tile row shorter (a rectilinear grid), so that
/// a prime height cannot make a chunk per row.
#[allow(clippy::too_many_arguments)]
pub fn bands(h: u64, h2: u64, r: u64, w: u64, q: u64, uncompressed: bool, max_band: u64, floor: u64) -> (J, u64, u64) {
    let (hb, wb, qb) = (h as u128, w as u128, q as u128);
    if !uncompressed || wb * hb * qb <= max_band as u128 {
        let y = if h2 == h {
            json!(h)
        } else if r > 1 {
            json!([[h, r - 1], h2])
        } else {
            json!([h2])
        };
        return (y, h, 1);
    }
    let b = row_band(hb, h2 as u128, wb, qb, max_band as u128) as u64;
    if b as u128 * wb * qb >= floor as u128 || b == h {
        return (json!(b), b, h / b);
    }
    let big = 1u64.max((max_band as u128 / (wb * qb)) as u64);
    let per = h.div_ceil(big);
    let mut runs: Vec<(u64, u64)> = Vec::new();
    let mut push = |v: u64, n: u64| {
        if n == 0 {
            return;
        }
        match runs.last_mut() {
            Some(l) if l.0 == v => l.1 += n,
            _ => runs.push((v, n)),
        }
    };
    for j in 0..r {
        let hh = if j < r - 1 { h } else { h2 };
        push(big, hh / big);
        if hh % big != 0 {
            push(hh % big, 1);
        }
    }
    let y: Vec<J> = runs.iter().map(|&(v, n)| if n == 1 { json!(v) } else { json!([v, n]) }).collect();
    (J::Array(y), big, per)
}

/// The axes of an array over its subblocks' planes, the least t, c, z, and the extent along each.
fn plane_axes(planes: &[(i64, i64, i64)], p: u64) -> (Vec<&'static str>, [i64; 3], [i64; 3]) {
    let lo = [0, 1, 2].map(|k| planes.iter().map(|pl| [pl.0, pl.1, pl.2][k]).min().unwrap());
    let hi = [0, 1, 2].map(|k| planes.iter().map(|pl| [pl.0, pl.1, pl.2][k]).max().unwrap());
    let mut axes = Vec::new();
    if hi[0] > lo[0] {
        axes.push("t");
    }
    if hi[1] > lo[1] || p > 1 {
        axes.push("c");
    }
    if hi[2] > lo[2] {
        axes.push("z");
    }
    axes.extend(["y", "x"]);
    (axes, lo, [0, 1, 2].map(|k| hi[k] - lo[k] + 1))
}

impl Czi {
    fn extent(&self, v: i128) -> Res<i128> {
        self.check("extent", rules::env([("extent", num(v as f64))]))?;
        Ok(v)
    }

    fn dims_json(series: &[(u8, i64)]) -> serde_json::Map<String, J> {
        SERIES
            .iter()
            .zip(series)
            .filter(|(_, k)| k.0 == 1)
            .map(|(l, k)| ((*l as char).to_string(), json!(k.1)))
            .collect()
    }

    fn layout(&self) -> Res<J> {
        let max_band = self.rules.limit("max_band") as u64;
        let floor = self.rules.limit("band_floor") as u64;
        // placement (spec/virtualize/czi.md §3.2): an entry that read its subblock, whose copy agrees, with a coded size
        let mut ps: Vec<Placed> = Vec::new();
        for (i, e) in self.entries.iter().enumerate() {
            let sub = &self.subs[i];
            let (Some(el), true, Some((cw, ch, hilo, _))) = (sub.data_el, sub.agrees, sub.coded) else { continue };
            let (x, y) = (e.dim(b'X').unwrap(), e.dim(b'Y').unwrap());
            let series = SERIES.iter().map(|&l| e.dim(l).map(|d| (1, d.0 as i64)).unwrap_or((0, 0))).collect();
            let st = |l: u8| e.dim(l).map(|d| d.0 as i64).unwrap_or(0);
            ps.push(Placed {
                index: i,
                series,
                plane: (st(b'T'), st(b'C'), st(b'Z')),
                x: x.0 as i64,
                y: y.0 as i64,
                wl: x.1 as i64,
                hl: y.1 as i64,
                w: x.2 as i64,
                h: y.2 as i64,
                cw,
                ch,
                form: (e.pt, e.comp, hilo),
                layer: layer(x.1 as i64, y.1 as i64, x.2 as i64, y.2 as i64),
                el,
            });
        }
        // series, layers and levels (§3.3–§3.5)
        let mut by_series: BTreeMap<Vec<(u8, i64)>, Vec<usize>> = BTreeMap::new();
        for (k, p) in ps.iter().enumerate() {
            by_series.entry(p.series.clone()).or_default().push(k);
        }
        let mut images: Vec<(Vec<(u8, i64)>, Vec<Level>)> = Vec::new();
        let mut tiles: Vec<usize> = Vec::new();
        for (key, members) in &by_series {
            let mut layers: BTreeMap<(u32, u32), Vec<usize>> = BTreeMap::new();
            for &k in members {
                match ps[k].layer {
                    Some(l) if ps[k].conforming() => layers.entry(l).or_default().push(k),
                    _ => tiles.push(k),
                }
            }
            let mut levels = Vec::new();
            for b in layers.values() {
                match classify(b, &ps) {
                    Some(lv) => levels.push(lv),
                    None => tiles.extend(b),
                }
            }
            levels.sort_by_key(|lv| (lv.factor, lv.layer));
            if !levels.is_empty() {
                let pt = levels[0].form.0;
                let (kept, other): (Vec<Level>, Vec<Level>) = levels.into_iter().partition(|lv| lv.form.0 == pt);
                for lv in &other {
                    tiles.extend(lv.cells.iter().map(|c| c.0));
                }
                self.check("image", rules::env([("levels", num(kept.len() as f64))]))?;
                images.push((key.clone(), kept));
            }
        }
        self.check("images", rules::env([("images", num(images.len() as f64))]))?;
        // the images (§4.2, §4.3)
        let um = 1.0 / 1e-6;
        let v = &self.values;
        let px = v.px.map(|x| x * um);
        let py = v.py.map(|x| x * um);
        let mut images_json = Vec::new();
        for (key, levels) in &images {
            let (dtype, p, q) = coding::pixel_type(levels[0].form.0).unwrap();
            let planes: Vec<(i64, i64, i64)> = levels.iter().flat_map(|lv| lv.cells.iter().map(|c| ps[c.0].plane)).collect();
            let (axes, lo, extent) = plane_axes(&planes, p);
            let dims = Self::dims_json(key);
            let name = dims
                .get("S")
                .and_then(|s| v.scene(s.as_i64().unwrap()))
                .filter(|n| !n.is_empty() && n.len() <= 256)
                .map(|n| n.to_string());
            let mut units = serde_json::Map::new();
            for (a, has) in [("x", px.is_some()), ("y", py.is_some()), ("z", v.pz.is_some())] {
                if has {
                    units.insert(a.into(), json!("micrometer"));
                }
            }
            if v.inc.is_some() {
                units.insert("t".into(), json!("second"));
            }
            let mut scales = Vec::new();
            let mut translations = Vec::new();
            let mut finite = true;
            for lv in levels {
                let f = lv.factor as f64;
                let one = |a: &str| -> f64 {
                    match a {
                        "t" => v.inc.unwrap_or(1.0),
                        "c" => 1.0,
                        "z" => v.pz.map(|z| z * um).unwrap_or(1.0),
                        "y" => py.map(|y| y * f).unwrap_or(f),
                        _ => px.map(|x| x * f).unwrap_or(f),
                    }
                };
                let sx = (lv.origin.0 as f64 + (f - 1.0) / 2.0) * px.unwrap_or(1.0);
                let sy = (lv.origin.1 as f64 + (f - 1.0) / 2.0) * py.unwrap_or(1.0);
                let tr: Vec<f64> = axes.iter().map(|&a| if a == "x" { sx } else if a == "y" { sy } else { 0.0 }).collect();
                finite &= tr.iter().all(|t| t.is_finite());
                scales.push(axes.iter().map(|&a| json!(one(a))).collect::<Vec<_>>());
                translations.push(tr.into_iter().map(|t| json!(t)).collect::<Vec<_>>());
            }
            self.check("translation", rules::env([("all_finite", flag(finite))]))?;
            let mut lvs = Vec::new();
            for lv in levels {
                let ((w, h), (w2, h2), (m, r)) = (lv.tile, lv.edge, lv.grid);
                let (y_len, rows, per) = bands(h as u64, h2 as u64, r as u64, w as u64, q, lv.form.1 == coding::UNCOMPRESSED, max_band, floor);
                let size = [
                    ("t", self.extent(extent[0] as i128)?),
                    ("c", self.extent(extent[1] as i128 * p as i128)?),
                    ("z", self.extent(extent[2] as i128)?),
                    ("y", self.extent((r as i128 - 1) * h as i128 + h2 as i128)?),
                    ("x", self.extent((m as i128 - 1) * w as i128 + w2 as i128)?),
                ];
                let x_len = if w2 == w { json!(w) } else if m > 1 { json!([[w, m - 1], w2]) } else { json!([w2]) };
                let lens = |a: &str| match a {
                    "t" | "z" => json!(1),
                    "c" => json!(p),
                    "y" => y_len.clone(),
                    _ => x_len.clone(),
                };
                let cells: Vec<J> = lv
                    .cells
                    .iter()
                    .map(|&(k, c, j)| {
                        let s = &ps[k];
                        json!([s.el, s.plane.0 - lo[0], s.plane.1 - lo[1], s.plane.2 - lo[2], c, j, s.w, s.h])
                    })
                    .collect();
                lvs.push(json!({
                    "shape": axes.iter().map(|a| json!(size.iter().find(|x| x.0 == *a).unwrap().1 as i64)).collect::<Vec<_>>(),
                    "chunks": axes.iter().map(|a| lens(a)).collect::<Vec<_>>(),
                    "factor": lv.factor, "grid": [m, r],
                    "band": {"rows": rows, "per": per},
                    "cells": cells,
                }));
            }
            images_json.push(json!({
                "dims": dims, "name": name, "axes": axes, "lo": {"t": lo[0], "c": lo[1], "z": lo[2]},
                "extent": {"t": extent[0], "c": extent[1], "z": extent[2]},
                "form": [levels[0].form.0, levels[0].form.1, levels[0].form.2], "dtype": dtype,
                "units": units, "scales": scales, "translations": translations, "levels": lvs,
            }));
        }
        // tiles, one array per tile position and copy (§4.4)
        tiles.sort_by_key(|&k| ps[k].index);
        type G = (Vec<(u8, i64)>, i64, i64, i64, i64, i64, i64, u64, u64, (i32, i32, bool));
        let mut copies: HashMap<(G, (i64, i64, i64)), u64> = HashMap::new();
        let mut order: Vec<(G, u64)> = Vec::new();
        let mut groups: HashMap<(G, u64), Vec<usize>> = HashMap::new();
        for &k in &tiles {
            let p = &ps[k];
            let g: G = (p.series.clone(), p.x, p.y, p.wl, p.hl, p.w, p.h, p.cw, p.ch, p.form);
            let c = copies.entry((g.clone(), p.plane)).or_insert(0);
            let copy = *c;
            *c += 1;
            let key = (g, copy);
            if !groups.contains_key(&key) {
                order.push(key.clone());
            }
            groups.entry(key).or_default().push(k);
        }
        let mut tiles_json = Vec::new();
        for key in &order {
            let members = &groups[key];
            let first = &ps[members[0]];
            let (dtype, p, q) = coding::pixel_type(first.form.0).unwrap();
            let planes: Vec<(i64, i64, i64)> = members.iter().map(|&k| ps[k].plane).collect();
            let (axes, lo, extent) = plane_axes(&planes, p);
            let (y_len, rows, per) = bands(first.ch, first.ch, 1, first.cw, q, first.form.1 == coding::UNCOMPRESSED, max_band, floor);
            let size = [
                ("t", self.extent(extent[0] as i128)?),
                ("c", self.extent(extent[1] as i128 * p as i128)?),
                ("z", self.extent(extent[2] as i128)?),
                ("y", self.extent(first.ch as i128)?),
                ("x", self.extent(first.cw as i128)?),
            ];
            let lens = |a: &str| match a {
                "t" | "z" => json!(1),
                "c" => json!(p),
                "y" => y_len.clone(),
                _ => json!(first.cw),
            };
            tiles_json.push(json!({
                "dims": Self::dims_json(&first.series), "x": first.x, "y": first.y, "size": [first.wl, first.hl],
                "stored_size": [first.w, first.h], "planes": {"t": lo[0], "c": lo[1], "z": lo[2]}, "copy": key.1,
                "axes": axes, "form": [first.form.0, first.form.1, first.form.2], "dtype": dtype,
                "shape": axes.iter().map(|a| json!(size.iter().find(|x| x.0 == *a).unwrap().1 as i64)).collect::<Vec<_>>(),
                "chunks": axes.iter().map(|a| lens(a)).collect::<Vec<_>>(),
                "band": {"rows": rows, "per": per},
                "members": members.iter().map(|&k| {
                    let s = &ps[k];
                    json!([s.el, s.plane.0 - lo[0], s.plane.1 - lo[1], s.plane.2 - lo[2], 0, 0, s.w, s.h])
                }).collect::<Vec<_>>(),
            }));
        }
        let (major, minor, primary, file, file_part, _, _, update, _) = self.fields.clone().unwrap();
        Ok(json!({
            "root": {"version": [major, minor], "primary_file_guid": guid(&primary), "file_guid": guid(&file),
                     "file_part": file_part, "update_pending": update},
            "subblocks": self.entries.len(),
            "images": images_json,
            "tiles": tiles_json,
            "values": self.values.to_json(),
        }))
    }

    /// The IR, finished (aliases and gaps), and the facts the projection reads.
    pub fn finish(mut self) -> Result<(Ir, J), String> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        self.ir.sort_values();
        check::finish(&mut self.ir)?;
        let mut facts = self.facts.take().unwrap_or(J::Null);
        facts["requests"] = json!(self.requests);
        facts["requested"] = json!(self.requested);
        facts["rounds"] = json!(self.rounds);
        facts["held_max"] = json!(self.held_max);
        Ok((self.ir, facts))
    }
}
