//! A sans-IO parser of TIFF, BigTIFF and OME-TIFF files into the IR
//! (spec/virtualize.md §3, spec/virtualize/tiff.md): it never reads; it asks for batches
//! of ranges (`step`), is given their bytes (`feed`), and holds them only until
//! the step that uses them.
//!
//! Rounds: (1) the header; (2) the main chain, one IFD a round (each IFD names
//! the next); (3) the out-of-line values of the chain's IFDs that the profile
//! reads, or that are small; (4) the SubIFDs, a depth a round, with their values;
//! (5) the values the layout needs that were not read yet (each level's tile
//! tables), as the layout asks for them; (6) the IFDs other pointer tags lead to,
//! for the IR only. The schema (`schema/tiff.json`) is checked as the profile
//! meets each role: a file that fails is rejected there, and nowhere else.

use crate::check;
use crate::expr::V;
use crate::fed::{Fed, u16_at, u32_at, u64_at};
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, Vars, flag, int, num, text};
use crate::xml::{self, Ev};
use serde_json::{Value as J, json};
use std::collections::{BTreeMap, HashMap, HashSet};

const TABLE: [u16; 21] = [
    256, 257, 258, 259, 262, 266, 270, 277, 282, 283, 284, 296, 317, 322, 323, 324, 325, 330, 339, 347, 530,
];
const SCALARS: [u16; 13] = [256, 257, 259, 262, 266, 277, 282, 283, 284, 296, 317, 322, 323];
const LAYOUT: [u16; 11] = [273, 279, 288, 289, 324, 325, 513, 514, 519, 520, 521];
const ADOBE: [u8; 15] = [
    0xFF, 0xEE, 0x00, 0x0E, 0x41, 0x64, 0x6F, 0x62, 0x65, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00,
];
const WINDOW: u64 = 4096; // the bytes asked at an IFD, before its entry count is known

fn is_table(tag: u16) -> bool {
    TABLE.contains(&tag)
}

fn is_used(tag: u16) -> bool {
    is_table(tag) && tag != 270 && tag != 330
}

fn is_scalar(tag: u16) -> bool {
    SCALARS.contains(&tag)
}

pub fn type_size(t: u16) -> Option<u64> {
    Some(match t {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 | 16 | 17 | 18 => 8,
        _ => return None,
    })
}

fn unsigned(t: u16) -> bool {
    matches!(t, 1 | 3 | 4 | 13 | 16 | 18)
}

/// A pointer tag (spec/virtualize/tiff.md §5): it leads to IFDs the IR describes.
fn pointer(tag: u16, typ: u16) -> bool {
    match tag {
        330 | 34665 | 34853 | 40965 => unsigned(typ),
        400 => typ == 4,
        _ => matches!(typ, 13 | 18) && !is_table(tag) && !LAYOUT.contains(&tag),
    }
}

fn label(tag: u16) -> String {
    match tag {
        330 => "subifds".into(),
        34665 => "exif".into(),
        34853 => "gps".into(),
        40965 => "interoperability".into(),
        _ => format!("ifd_{tag}"),
    }
}

fn value_type(le: bool, typ: u16, count: u64) -> String {
    let e = if le { "<" } else { ">" };
    match typ {
        1 => format!("u1[{count}]"),
        6 => format!("i1[{count}]"),
        2 => format!("ascii[{count}]"),
        7 => format!("bytes[{count}]"),
        5 => format!("{e}u4[{count},2]"),
        10 => format!("{e}i4[{count},2]"),
        _ => {
            let t = match typ {
                3 => "u2",
                4 | 13 => "u4",
                8 => "i2",
                9 => "i4",
                11 => "f4",
                12 => "f8",
                16 | 18 => "u8",
                _ => "i8",
            };
            format!("{e}{t}[{count}]")
        }
    }
}

#[derive(Clone)]
struct Ent {
    tag: u16,
    typ: u16,
    count: u64,
    field: Vec<u8>,
    /// the value's bytes (count × type size), when the type is known
    nbytes: Option<u64>,
    inline: bool,
    /// the value's offset, when out of line
    off: u64,
    /// its value element (in line, or out of line within the file)
    val: Option<u32>,
    dup: bool,
}

#[derive(Clone)]
struct Ifd {
    offset: u64,
    el: u32,
    depth: u32,
    tree: bool,
    entries: Vec<Ent>,
    /// the first entry of each tag
    first: HashMap<u16, usize>,
    /// SubIFDs read, in order
    subs: Vec<usize>,
}

impl Ifd {
    fn field(&self, tag: u16) -> Option<&Ent> {
        self.first.get(&tag).map(|&k| &self.entries[k])
    }
    /// A tag of the table that passed the field checks (the profile's `fields`).
    fn has(&self, tag: u16) -> bool {
        self.field(tag).is_some()
    }
}

#[derive(PartialEq, Clone, Copy, Debug)]
enum Phase {
    Start,
    Header,
    Chain,
    ChainValues,
    SubPlan,
    SubRead,
    SubValues,
    Layout,
    Pointers,
    Done,
}

/// A loaded value, as the profile reads it.
#[derive(Clone, Debug)]
enum Loaded {
    Ints(Vec<u64>),
    Pairs(Vec<(u64, u64)>),
    Bytes(Vec<u8>),
}

enum Stop {
    Need(Vec<(u64, u64)>),
    Reject(String),
}

impl From<String> for Stop {
    fn from(s: String) -> Self {
        Stop::Reject(s)
    }
}

type Got<T> = Result<T, Stop>;

/// An IFD to read: (parent element, name, name index, offset, depth, in the tree, parent IFD)
#[derive(Clone)]
struct Pending {
    parent: u32,
    name: String,
    nidx: u64,
    offset: u64,
    depth: u32,
    tree: bool,
    from: Option<usize>,
}

pub struct Tiff {
    size: u64,
    phase: Phase,
    fed: Fed,
    pub ir: Ir,
    rules: &'static Rules,
    le: bool,
    big: bool,
    hs: u64,
    ifds: Vec<Ifd>,
    by_offset: HashMap<u64, usize>,
    chain: Vec<usize>,
    next: u64,
    pending: Vec<Pending>,
    bodies: bool,
    depth: u32,
    /// value extents read: (offset, length) -> the first element holding them
    values: HashMap<(u64, u64), u32>,
    /// value prefixes the layout read: (offset, length) -> bytes
    heads: HashMap<(u64, u64), Vec<u8>>,
    asked_values: Vec<(u64, u64, Vec<u32>)>,
    layout_asked: Vec<(u64, u64)>,
    chain_window: u64,
    last_window: (u64, u64),
    eager: u64,
    layout: Option<Layout>,
    /// IFD extents, for pointer targets: start -> end
    extents: BTreeMap<u64, u64>,
    tries: u64,
    todo: Vec<(usize, u16, String, u64)>,
    ptr_state: u8,
    made: Vec<usize>,
    data_keys: HashMap<String, u32>,
    pub requests: u64,
    pub requested: u64,
    pub rounds: u64,
}

struct Level {
    w: u64,
    h: u64,
    tw: u64,
    th: u64,
    ifds: Vec<usize>,
}

struct Layout {
    facts: J,
    /// (IFD, JPEG prefix) of every IFD a level uses
    prefixes: HashMap<usize, Vec<u8>>,
    levels: Vec<Level>,
    planes: Option<Vec<usize>>,
}

type Fmt = (u64, u64, u64, u64, u64, u64, Option<u64>);

impl Tiff {
    pub fn new(size: u64) -> Self {
        Tiff {
            size,
            phase: Phase::Start,
            fed: Fed::default(),
            ir: Ir::new(size),
            rules: rules::tiff(),
            le: true,
            big: false,
            hs: 8,
            ifds: Vec::new(),
            by_offset: HashMap::new(),
            chain: Vec::new(),
            next: 0,
            pending: Vec::new(),
            bodies: false,
            depth: 0,
            values: HashMap::new(),
            heads: HashMap::new(),
            asked_values: Vec::new(),
            layout_asked: Vec::new(),
            chain_window: WINDOW,
            last_window: (0, 0),
            eager: 0,
            layout: None,
            extents: BTreeMap::new(),
            tries: 0,
            todo: Vec::new(),
            ptr_state: 0,
            made: Vec::new(),
            data_keys: HashMap::new(),
            requests: 0,
            requested: 0,
            rounds: 0,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
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

    fn cs(&self) -> u64 {
        if self.big { 8 } else { 2 }
    }
    fn es(&self) -> u64 {
        if self.big { 20 } else { 12 }
    }
    fn fs(&self) -> u64 {
        if self.big { 8 } else { 4 }
    }
    fn word(&self, b: &[u8], at: usize) -> u64 {
        if self.big { u64_at(b, at, self.le) } else { u32_at(b, at, self.le) as u64 }
    }
    fn within(&self, o: u64, n: u64) -> bool {
        o.checked_add(n).is_some_and(|e| e <= self.size)
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::Header;
                    return Ok(self.ask(vec![(0, 16.min(self.size))]));
                }
                Phase::Header => {
                    let n = 16.min(self.size);
                    let h = self.fed.get(0, n).ok_or("internal: no header")?.to_vec();
                    self.fed.clear();
                    self.header(&h)?;
                    self.phase = Phase::Chain;
                }
                Phase::Chain => {
                    // the main chain, an IFD a round: each names the next
                    if let Some(s) = self.read_pending()? {
                        return Ok(s);
                    }
                    if self.next == 0 {
                        self.phase = Phase::ChainValues;
                        continue;
                    }
                    let o = self.next;
                    self.check_offset(o, true)?;
                    self.pending = vec![Pending {
                        parent: 0,
                        name: "ifds/".into(),
                        nidx: self.chain.len() as u64,
                        offset: o,
                        depth: 0,
                        tree: true,
                        from: None,
                    }];
                    self.bodies = false;
                    // the IFD may lie in the window the last one was read from (a chain
                    // written in order); else a window from it, growing while the chain
                    // keeps to the bytes just after the last window
                    if self.fed.get(o, self.cs()).is_some() {
                        continue;
                    }
                    let (a, b) = self.last_window;
                    self.chain_window = if o >= b && o < b + self.chain_window { (2 * self.chain_window).min(1 << 20) } else { WINDOW };
                    let _ = a;
                    let n = self.chain_window.min(self.size - o).max(self.cs());
                    self.last_window = (o, o + n);
                    self.fed.clear();
                    return Ok(self.ask(vec![(o, n)]));
                }
                Phase::ChainValues | Phase::SubValues => {
                    if let Some(s) = self.read_values()? {
                        return Ok(s);
                    }
                    self.phase = Phase::SubPlan;
                }
                Phase::SubPlan => {
                    // the SubIFDs of the tree's IFDs at this depth, checked before they are read
                    if self.depth >= 4 {
                        self.phase = Phase::Layout;
                        continue;
                    }
                    let next = self.plan_subs()?;
                    self.depth += 1;
                    if next.is_empty() {
                        self.phase = Phase::Layout;
                        continue;
                    }
                    self.pending = next;
                    self.bodies = false;
                    self.phase = Phase::SubRead;
                    let r = self.heads_of_pending();
                    return Ok(self.ask(r));
                }
                Phase::SubRead => {
                    if let Some(s) = self.read_pending()? {
                        return Ok(s);
                    }
                    self.phase = Phase::SubValues;
                }
                Phase::Layout => {
                    if !self.layout_asked.is_empty() {
                        let asked = std::mem::take(&mut self.layout_asked);
                        for (o, n) in asked {
                            let b = self.fed.get(o, n).ok_or("internal: a value the layout asked for was not fed")?.to_vec();
                            self.heads.insert((o, n), b);
                        }
                        self.fed.clear();
                    }
                    match self.derive() {
                        Ok(l) => {
                            self.layout = Some(l);
                            self.emit_data()?;
                            self.keep_tables();
                            self.heads.clear();
                            self.start_pointers();
                            self.phase = Phase::Pointers;
                        }
                        Err(Stop::Reject(m)) => return Err(m),
                        Err(Stop::Need(mut r)) => {
                            r.sort_unstable();
                            r.dedup();
                            if r.is_empty() || r.iter().any(|x| self.heads.contains_key(x)) {
                                return Err("internal: the layout asked again for bytes it has".into());
                            }
                            self.layout_asked = r.clone();
                            return Ok(self.ask(r));
                        }
                    }
                }
                Phase::Pointers => {
                    if let Some(s) = self.pointers()? {
                        return Ok(s);
                    }
                    self.phase = Phase::Done;
                }
                Phase::Done => return Ok(Step::Done),
            }
        }
    }

    /// The SubIFDs of the tree's IFDs at the current depth, in order, each checked
    /// as the profile reads it (its values, then its offset).
    fn plan_subs(&mut self) -> Res<Vec<Pending>> {
        let at: Vec<usize> = (0..self.ifds.len())
            .filter(|&k| self.ifds[k].tree && self.ifds[k].depth == self.depth)
            .collect();
        let mut next = Vec::new();
        let mut seen: HashSet<u64> = self.by_offset.keys().copied().collect();
        for k in at {
            let Some(e) = self.ifds[k].field(330).cloned() else { continue };
            let vals = self.ints(&e, false).ok_or("internal: SubIFDs not read")?;
            self.check(
                "values",
                rules::env([
                    ("tag", int(330)),
                    ("type", int(e.typ as i128)),
                    ("largest", num(vals.iter().copied().max().unwrap_or(0) as f64)),
                ]),
            )?;
            for (j, &o) in vals.iter().enumerate() {
                self.check(
                    "ifd_offset",
                    rules::env([
                        ("offset", num(o as f64)),
                        ("chain", flag(false)),
                        ("header_size", num(self.hs as f64)),
                        ("seen", flag(seen.contains(&o))),
                        ("read", num(seen.len() as f64)),
                        ("count_within", flag(self.within(o, self.cs()))),
                        ("within", flag(true)),
                    ]),
                )?;
                seen.insert(o);
                next.push(Pending {
                    parent: self.ifds[k].el,
                    name: "subifds/".into(),
                    nidx: j as u64,
                    offset: o,
                    depth: self.depth + 1,
                    tree: true,
                    from: Some(k),
                });
            }
        }
        Ok(next)
    }

    // ---- the header and IFDs

    fn header(&mut self, h: &[u8]) -> Res<()> {
        let order = match h.get(..2) {
            Some(b"II") => "II",
            Some(b"MM") => "MM",
            _ => "other",
        };
        self.le = order == "II";
        let magic = if h.len() >= 4 { u16_at(h, 2, self.le) } else { 0 };
        let (os, rs) = if h.len() >= 8 { (u16_at(h, 4, self.le), u16_at(h, 6, self.le)) } else { (0, 0) };
        self.check(
            "header",
            rules::env([
                ("length", int(h.len() as i128)),
                ("order", text(order)),
                ("magic", int(magic as i128)),
                ("offset_size", int(os as i128)),
                ("reserved", int(rs as i128)),
            ]),
        )?;
        self.big = magic == 43;
        let e = if self.le { "<" } else { ">" };
        self.hs = if self.big { 16 } else { 8 };
        let n = self.hs;
        let s = self.ir.struct_(0, "header", NO_INDEX, 0, Some((0, n)))?;
        self.ir.value(s, "byte_order", NO_INDEX, "ascii[2]", 0, (0, 2), Some(&h[..2]))?;
        self.ir.value(s, "magic", NO_INDEX, &format!("{e}u2"), 0, (2, 2), Some(&h[2..4]))?;
        if self.big {
            self.ir.value(s, "offset_size", NO_INDEX, &format!("{e}u2"), 0, (4, 2), Some(&h[4..6]))?;
            self.ir.value(s, "reserved", NO_INDEX, &format!("{e}u2"), 0, (6, 2), Some(&h[6..8]))?;
        }
        let w = if self.big { "u8" } else { "u4" };
        self.ir.value(s, "first_ifd", NO_INDEX, &format!("{e}{w}"), 0, (n / 2, n / 2), Some(&h[n as usize / 2..n as usize]))?;
        self.next = self.word(h, n as usize / 2);
        Ok(())
    }

    fn check_offset(&self, o: u64, chain: bool) -> Res<()> {
        self.check(
            "ifd_offset",
            rules::env([
                ("offset", num(o as f64)),
                ("chain", flag(chain)),
                ("header_size", num(self.hs as f64)),
                ("seen", flag(self.by_offset.contains_key(&o))),
                ("read", num(self.by_offset.len() as f64)),
                ("count_within", flag(self.within(o, self.cs()))),
                ("within", flag(true)),
            ]),
        )
    }

    /// The values the layout read whole (tile and strip tables past the eager budget)
    /// become their elements' values, as smaller ones are: the mirror reads the
    /// tiles' extents from them.
    fn keep_tables(&mut self) {
        let mut put = Vec::new();
        for f in &self.ifds {
            for e in &f.entries {
                let (Some(v), Some(nb)) = (e.val, e.nbytes) else { continue };
                if e.inline || self.ir.value_bytes(v).is_some() {
                    continue;
                }
                if let Some(b) = self.heads.get(&(e.off, nb)) {
                    put.push((v, b.clone()));
                }
            }
        }
        for (v, b) in put {
            self.ir.put_value(v, &b);
        }
        self.ir.sort_values();
    }

    /// The first bytes of each pending IFD: its entry count, and a window of entries.
    fn heads_of_pending(&self) -> Vec<(u64, u64)> {
        self.pending
            .iter()
            .filter(|p| self.within(p.offset, self.cs()))
            .map(|p| (p.offset, WINDOW.min(self.size - p.offset).max(self.cs())))
            .collect()
    }

    /// The IFD's length: entry count, entries, next offset (None past u64).
    fn ifd_len(&self, count: u64) -> Option<u64> {
        count.checked_mul(self.es())?.checked_add(self.cs() + self.fs())
    }

    /// Reads the pending IFDs (their counts, then whatever of their bodies the
    /// window did not hold); None when they are all read.
    fn read_pending(&mut self) -> Res<Option<Step>> {
        if self.pending.is_empty() {
            return Ok(None);
        }
        let mut more = Vec::new();
        if !self.bodies {
            for p in &self.pending {
                let Some(c) = self.fed.get(p.offset, self.cs()) else {
                    return Err(format!("internal: no entry count at {}", p.offset));
                };
                let count = self.word_n(c);
                let len = self.ifd_len(count);
                let within = len.is_some_and(|n| self.within(p.offset, n));
                if !within {
                    self.check(
                        "ifd_offset",
                        rules::env([
                            ("offset", num(p.offset as f64)),
                            ("chain", flag(p.tree && p.depth == 0)),
                            ("header_size", num(self.hs as f64)),
                            ("seen", flag(false)),
                            ("read", num(0.0)),
                            ("count_within", flag(true)),
                            ("within", flag(false)),
                        ]),
                    )?;
                }
                let n = len.unwrap();
                if self.fed.get(p.offset, n).is_none() {
                    more.push((p.offset, n));
                }
            }
            self.bodies = true;
            if !more.is_empty() {
                // the IFDs the window did not hold: their whole bodies
                return Ok(Some(self.ask(more)));
            }
        }
        let pending = std::mem::take(&mut self.pending);
        for p in pending {
            let c = self.fed.get(p.offset, self.cs()).ok_or("internal: no entry count")?;
            let count = self.word_n(c);
            let n = self.ifd_len(count).unwrap();
            let body = self.fed.get(p.offset, n).ok_or("internal: no IFD body")?.to_vec();
            let k = self.ifd(&p, &body)?;
            if p.tree && p.depth == 0 {
                self.chain.push(k);
                let f = self.fs() as usize;
                self.next = self.word(&body, body.len() - f);
            }
            if let Some(parent) = p.from {
                self.ifds[parent].subs.push(k);
            }
        }
        if self.phase != Phase::Chain {
            self.fed.clear(); // the chain keeps its window for the next IFD
        }
        Ok(None)
    }

    fn word_n(&self, b: &[u8]) -> u64 {
        if self.big { u64_at(b, 0, self.le) } else { u16_at(b, 0, self.le) as u64 }
    }

    /// Emits the IFD at `p.offset` from its bytes, checking its fields (tree IFDs).
    fn ifd(&mut self, p: &Pending, body: &[u8]) -> Res<usize> {
        let (cs, es, fs) = (self.cs() as usize, self.es() as usize, self.fs() as usize);
        let o = p.offset;
        let count = self.word_n(body);
        let end = o + body.len() as u64;
        let e = if self.le { "<" } else { ">" };
        let wt = if self.big { "u8" } else { "u4" };
        let s = self.ir.struct_(p.parent, &p.name, p.nidx, 0, Some((o, end - o)))?;
        let ct = format!("{e}{}", if self.big { "u8" } else { "u2" });
        self.ir.value(s, "entry_count", NO_INDEX, &ct, 0, (o, cs as u64), Some(&body[..cs]))?;
        let k = self.ifds.len();
        self.by_offset.insert(o, k);
        self.extents.insert(o, end);
        let mut ifd = Ifd {
            offset: o,
            el: s,
            depth: p.depth,
            tree: p.tree,
            entries: Vec::with_capacity(count as usize),
            first: HashMap::new(),
            subs: Vec::new(),
        };
        let mut dups: HashMap<u16, u32> = HashMap::new();
        for i in 0..count as usize {
            let at = cs + i * es;
            let raw = &body[at..at + es];
            let tag = u16_at(raw, 0, self.le);
            let typ = u16_at(raw, 2, self.le);
            let n = self.word(raw, 4);
            let field = raw[4 + fs..4 + 2 * fs].to_vec();
            let nbytes = type_size(typ).and_then(|z| z.checked_mul(n));
            let inline = nbytes.is_some_and(|b| b <= fs as u64);
            let off = if inline { 0 } else { self.word(&field, 0) };
            let dup = *dups.get(&tag).unwrap_or(&0);
            dups.insert(tag, dup + 1);
            let t = if dup == 0 {
                self.ir.struct_(s, "tags/", tag as u64, 0, Some((o + at as u64, es as u64)))?
            } else {
                self.ir.struct_(s, &format!("tags/{tag}~{dup}"), NO_INDEX, 0, Some((o + at as u64, es as u64)))?
            };
            let head = format!("{{tag:{e}u2,type:{e}u2,count:{e}{wt}");
            let ea = o + at as u64;
            let mut val = None;
            match nbytes {
                None => {
                    self.ir.value(t, "entry", NO_INDEX, &format!("{head},field:bytes[{fs}]}}"), 0, (ea, es as u64), Some(raw))?;
                }
                Some(nb) if inline => {
                    self.ir.value(t, "entry", NO_INDEX, &format!("{head}}}"), 0, (ea, 4 + fs as u64), Some(&raw[..4 + fs]))?;
                    if nb > 0 {
                        let vt = value_type(self.le, typ, n);
                        val = Some(self.ir.value(t, "value", NO_INDEX, &vt, 0, (ea + 4 + fs as u64, nb), Some(&field[..nb as usize]))?);
                    }
                }
                Some(nb) => {
                    self.ir.value(t, "entry", NO_INDEX, &format!("{head},offset:{e}{wt}}}"), 0, (ea, es as u64), Some(raw))?;
                    if self.within(off, nb) {
                        let vt = value_type(self.le, typ, n);
                        val = Some(self.ir.value(t, "value", NO_INDEX, &vt, 0, (off, nb), None)?);
                    }
                }
            }
            let ent = Ent { tag, typ, count: n, field, nbytes, inline, off, val, dup: dup > 0 };
            if dup == 0 && is_table(tag) && p.tree {
                let outside = !inline && !nbytes.is_some_and(|nb| self.within(off, nb));
                let mut env = rules::env([
                    ("tag", int(tag as i128)),
                    ("type", int(typ as i128)),
                    ("count", num(n as f64)),
                    ("outside", flag(outside)),
                ]);
                // the type is checked first: the value's place is known only for a known type
                self.rules.check("field", &mut env)?;
            }
            if dup == 0 {
                ifd.first.insert(tag, ifd.entries.len());
            }
            ifd.entries.push(ent);
        }
        let f0 = cs + count as usize * es;
        self.ir.value(s, "next_ifd", NO_INDEX, &format!("{e}{wt}"), 0, (o + f0 as u64, fs as u64), Some(&body[f0..f0 + fs]))?;
        self.ifds.push(ifd);
        Ok(k)
    }

    // ---- values

    /// An out-of-line value's bytes, when read (as an element's value, or as a head).
    fn bytes_of(&self, e: &Ent, n: u64) -> Option<Vec<u8>> {
        if e.inline {
            return Some(e.field[..n as usize].to_vec());
        }
        if let Some(v) = e.val.and_then(|v| self.ir.value_bytes(v)) {
            if n as usize <= v.len() {
                return Some(v[..n as usize].to_vec());
            }
        }
        self.heads.get(&(e.off, n)).cloned()
    }

    fn ints(&self, e: &Ent, first: bool) -> Option<Vec<u64>> {
        let z = type_size(e.typ)?;
        let n = if first { 1.min(e.count) } else { e.count };
        let b = self.bytes_of(e, n * z)?;
        Some(
            b.chunks(z as usize)
                .map(|c| match e.typ {
                    1 => c[0] as u64,
                    3 => u16_at(c, 0, self.le) as u64,
                    4 | 13 => u32_at(c, 0, self.le) as u64,
                    _ => u64_at(c, 0, self.le),
                })
                .collect(),
        )
    }

    /// Asks for the out-of-line values of the IFDs read since the last time: those
    /// the profile reads whatever their size, and others up to 64 KiB (within the
    /// eager budget). None when there are none to read, or once they are read.
    fn read_values(&mut self) -> Res<Option<Step>> {
        if !self.asked_values.is_empty() {
            let asked = std::mem::take(&mut self.asked_values);
            for (o, n, els) in asked {
                let Some(b) = self.fed.get(o, n).map(|b| b.to_vec()) else {
                    return Err(format!("internal: value [{o}, {}) not fed", o + n));
                };
                if els.is_empty() {
                    self.heads.insert((o, n), b);
                    continue;
                }
                self.ir.put_value(els[0], &b);
                self.values.insert((o, n), els[0]);
                for &e in &els[1..] {
                    self.ir.put_value_of(e, els[0]);
                }
            }
            self.fed.clear();
            self.ir.sort_values();
        }
        let max_decode = self.rules.limit("max_decode") as u64;
        let eager_limit = self.rules.limit("eager_decode") as u64;
        let mut want: BTreeMap<(u64, u64), Vec<u32>> = BTreeMap::new();
        for k in 0..self.ifds.len() {
            let f = &self.ifds[k];
            if f.entries.iter().all(|e| e.val.is_none() || e.inline) {
                continue;
            }
            for e in &f.entries {
                let Some(v) = e.val else { continue };
                if e.inline || self.ir.value_bytes(v).is_some() {
                    continue;
                }
                let n = e.nbytes.unwrap();
                if let Some(&first) = self.values.get(&(e.off, n)) {
                    if self.ir.value_bytes(first).is_some() {
                        self.ir.put_value_of(v, first);
                        continue;
                    }
                }
                let required = e.tag == 330 && f.tree && f.depth < 4 && !e.dup;
                let describe = !e.dup && (is_table(e.tag) || LAYOUT.contains(&e.tag) || pointer(e.tag, e.typ));
                let eager = describe && n <= max_decode && (want.contains_key(&(e.off, n)) || self.eager + n <= eager_limit);
                // every numeric value of at most 1024 bytes is held, whatever its tag: the
                // mirror's view shows it (conventions §8.7)
                let shown = n <= crate::mirror::VIEW_VALUE && e.typ != 7 && type_size(e.typ).is_some();
                if required || eager || shown {
                    let w = want.entry((e.off, n)).or_default();
                    if w.is_empty() && !required && eager {
                        self.eager += n;
                    }
                    w.push(v);
                }
            }
        }
        self.ir.sort_values();
        if want.is_empty() {
            return Ok(None);
        }
        // a value already asked for in this batch is read once
        self.asked_values = want.into_iter().map(|((o, n), v)| (o, n, v)).collect();
        let r: Vec<(u64, u64)> = self.asked_values.iter().map(|x| (x.0, x.1)).collect();
        Ok(Some(self.ask(r)))
    }
}

// ---- the layout (spec/virtualize.md §3.2–§3.4), as the profile reads it

type Vals = HashMap<u16, Loaded>;

/// The OME-XML of spec/virtualize/tiff.md §3: the first image's name, its Pixels'
/// attributes, its TiffData elements, and its first Plane at (0, 0, 0).
#[derive(Default, Debug)]
struct Ome {
    name: Option<String>,
    pixels: Vec<(String, String)>,
    tiff_data: Vec<(Vec<(String, String)>, Option<String>)>,
    plane: Option<Vec<(String, String)>>,
}

fn get<'a>(attrs: &'a [(String, String)], k: &str) -> Option<&'a str> {
    attrs.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str())
}

fn decoded_attrs(t: &xml::TagRef, x: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for (k, v, _) in t.attrs(x) {
        if !out.iter().any(|(n, _)| *n == k) {
            out.push((k, xml::decode_refs(&v)));
        }
    }
    out
}

/// Whether `x` has an OME start tag, and if so its OME-XML, in one pass with
/// no tree. A Plane's TheZ, TheC and TheT are read as integers in order until
/// one is not 0 or one Plane is at (0, 0, 0): the first that is not an integer
/// rejects.
fn read_ome(x: &str, rules: &Rules) -> Res<Option<Ome>> {
    let mut ome = Ome::default();
    let mut is_ome = false;
    let mut image_seen = false;
    // 0: before Pixels, 1: inside, 2: after
    let mut state = 0;
    let mut searching: Option<usize> = None; // the TiffData whose UUID is looked for
    let mut text_from: Option<(usize, usize)> = None; // (TiffData, end of its UUID start tag)
    let mut skips: Vec<(usize, usize)> = Vec::new();
    let mut plane_error: Option<String> = None;
    let mut plane_done = false;
    for ev in xml::scan(x) {
        let t = match ev {
            Ev::Skip(a, b) => {
                if text_from.is_some() {
                    skips.push((a, b));
                }
                continue;
            }
            Ev::Tag(t) => t,
        };
        if let Some((k, from)) = text_from.take() {
            let mut s = String::new();
            let mut pos = from;
            for &(a, b) in &skips {
                s.push_str(&x[pos..a]);
                pos = b;
            }
            s.push_str(&x[pos..t.start]);
            skips.clear();
            ome.tiff_data[k].1 = Some(xml::decode_refs(&s).trim_matches([' ', '\t', '\r', '\n']).to_string());
        }
        if !t.is_end && t.name == "OME" {
            is_ome = true;
        }
        if !t.is_end && t.name == "Image" && !image_seen {
            image_seen = true;
            ome.name = t.attr(x, "Name").map(|v| v.0);
        }
        match state {
            0 => {
                if !t.is_end && t.name == "Pixels" {
                    ome.pixels = decoded_attrs(&t, x);
                    state = if t.self_closing { 2 } else { 1 };
                }
            }
            1 => {
                if t.is_end && t.name == "Pixels" {
                    state = 2;
                    searching = None;
                    continue;
                }
                if let Some(k) = searching {
                    if t.name == "TiffData" {
                        searching = None;
                    } else if !t.is_end && t.name == "UUID" {
                        searching = None;
                        let attrs = decoded_attrs(&t, x);
                        if let Some(f) = get(&attrs, "FileName") {
                            ome.tiff_data[k].1 = Some(f.to_string());
                        } else if t.self_closing {
                            ome.tiff_data[k].1 = Some(String::new());
                        } else {
                            text_from = Some((k, t.end));
                        }
                    }
                }
                if !t.is_end && t.name == "TiffData" {
                    ome.tiff_data.push((decoded_attrs(&t, x), None));
                    if !t.self_closing {
                        searching = Some(ome.tiff_data.len() - 1);
                    }
                }
                if !t.is_end && t.name == "Plane" && !plane_done && plane_error.is_none() {
                    let attrs = decoded_attrs(&t, x);
                    let mut zero = true;
                    for k in ["TheZ", "TheC", "TheT"] {
                        match int_attr(rules, &attrs, k, 0, 0) {
                            Ok(0) => {}
                            Ok(_) => {
                                zero = false;
                                break;
                            }
                            Err(m) => {
                                plane_error = Some(m);
                                zero = false;
                                break;
                            }
                        }
                    }
                    if zero {
                        ome.plane = Some(attrs);
                        plane_done = true;
                    }
                }
            }
            _ => {}
        }
    }
    if let Some((k, from)) = text_from {
        let mut s = String::new();
        let mut pos = from;
        for &(a, b) in &skips {
            s.push_str(&x[pos..a]);
            pos = b;
        }
        s.push_str(&x[pos..]);
        ome.tiff_data[k].1 = Some(xml::decode_refs(&s).trim_matches([' ', '\t', '\r', '\n']).to_string());
    }
    if !is_ome {
        return Ok(None);
    }
    if let Some(m) = plane_error {
        return Err(m);
    }
    Ok(Some(ome))
}

/// An OME-XML attribute read as an integer (the schema's `integer_attribute`).
fn int_attr(rules: &Rules, attrs: &[(String, String)], key: &str, default: u64, minimum: u64) -> Res<u64> {
    let Some(v) = get(attrs, key) else {
        return Ok(default);
    };
    let s = v.trim_matches([' ', '\t', '\r', '\n']);
    let digits = !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit());
    let value = if digits {
        s.bytes().fold(0u128, |a, c| a.saturating_mul(10).saturating_add((c - b'0') as u128))
    } else {
        0
    };
    let mut env = rules::env([
        ("key", text(key)),
        ("text", text(v)),
        ("digits", flag(digits)),
        ("value", num(value as f64)),
        ("minimum", num(minimum as f64)),
    ]);
    rules.check("integer_attribute", &mut env)?;
    Ok(value as u64)
}

/// The `name = value` fields of an Aperio ImageDescription (spec/virtualize/tiff.md §4.4), or None.
fn aperio_fields(d: &str) -> Option<Vec<(String, String)>> {
    if !d.starts_with("Aperio") {
        return None;
    }
    let mut out: Vec<(String, String)> = Vec::new();
    for part in d.split('|') {
        if let Some((n, v)) = part.split_once('=') {
            let n = n.trim_matches([' ', '\t', '\r', '\n']).to_string();
            if !out.iter().any(|(k, _)| *k == n) {
                out.push((n, v.trim_matches([' ', '\t', '\r', '\n']).to_string()));
            }
        }
    }
    Some(out)
}

fn first_int(m: &Vals, tag: u16) -> Option<u64> {
    match m.get(&tag) {
        Some(Loaded::Ints(v)) => v.first().copied(),
        _ => None,
    }
}

impl Tiff {
    fn view_ints(&self, b: &[u8], typ: u16) -> Vec<u64> {
        let z = type_size(typ).unwrap_or(1) as usize;
        b.chunks(z)
            .map(|c| match typ {
                1 => c[0] as u64,
                3 => u16_at(c, 0, self.le) as u64,
                4 | 13 => u32_at(c, 0, self.le) as u64,
                _ => u64_at(c, 0, self.le),
            })
            .collect()
    }

    /// The profile's `load`: the values of the table's tags an IFD's layout reads
    /// (all of them, or all but its tile tables), and IFD 0's ImageDescription.
    /// Asks for what was not read yet, for every IFD of `ks` at once.
    fn load(&self, ks: &[usize], tiles: bool, description: bool) -> Got<Vec<Vals>> {
        let mut need = Vec::new();
        let mut out = Vec::new();
        for &k in ks {
            let f = &self.ifds[k];
            let mut m = Vals::new();
            let mut tags: Vec<(u16, usize)> = f.first.iter().map(|(&t, &i)| (t, i)).collect();
            tags.sort_unstable_by_key(|x| x.1);
            for (tag, i) in tags {
                let e = &f.entries[i];
                let used = (is_used(tag) && (tiles || !(tag == 324 || tag == 325)))
                    || (tag == 270 && description && e.typ == 2);
                if !used {
                    continue;
                }
                let z = type_size(e.typ).unwrap();
                let n = match tag {
                    270 | 347 => e.count * z,
                    282 | 283 => z,
                    _ if is_scalar(tag) => z,
                    _ => e.count * z,
                };
                let n = if e.count == 0 { 0 } else { n };
                match self.bytes_of(e, n) {
                    None => need.push((e.off, n)),
                    Some(b) => {
                        let v = match tag {
                            270 | 347 => Loaded::Bytes(b),
                            282 | 283 => Loaded::Pairs(vec![(
                                u32_at(&b, 0, self.le) as u64,
                                u32_at(&b, 4, self.le) as u64,
                            )]),
                            _ => Loaded::Ints(self.view_ints(&b, e.typ)),
                        };
                        if let Loaded::Ints(v) = &v {
                            let mut env = rules::env([
                                ("tag", int(tag as i128)),
                                ("type", int(e.typ as i128)),
                                ("largest", num(v.iter().copied().max().unwrap_or(0) as f64)),
                            ]);
                            if need.is_empty() {
                                self.rules.check("values", &mut env)?;
                            }
                        }
                        m.insert(tag, v);
                    }
                }
            }
            out.push(m);
        }
        if need.is_empty() { Ok(out) } else { Err(Stop::Need(need)) }
    }

    fn req(&self, k: usize, m: &Vals, tag: u16, default: Option<u64>) -> Got<u64> {
        if let Some(v) = first_int(m, tag) {
            return Ok(v);
        }
        if let Some(d) = default {
            return Ok(d);
        }
        self.check(
            "required",
            rules::env([("offset", num(self.ifds[k].offset as f64)), ("tag", int(tag as i128)), ("present", flag(false))]),
        )?;
        Ok(0)
    }

    fn reqs(&self, k: usize, m: &Vals, tag: u16) -> Got<Vec<u64>> {
        match m.get(&tag) {
            Some(Loaded::Ints(v)) => Ok(v.clone()),
            _ => {
                self.check(
                    "required",
                    rules::env([("offset", num(self.ifds[k].offset as f64)), ("tag", int(tag as i128)), ("present", flag(false))]),
                )?;
                Ok(vec![])
            }
        }
    }

    fn fmt(&self, k: usize, m: &Vals) -> Got<Fmt> {
        let bits = self.reqs(k, m, 258)?;
        let formats = match m.get(&339) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![1],
        };
        let spp = self.req(k, m, 277, Some(1))?;
        let planar = if spp > 1 { self.req(k, m, 284, Some(1))? } else { 1 };
        let num_list = |v: &[u64]| V::List(v.iter().map(|&x| V::Num(x as f64)).collect());
        self.check(
            "format",
            rules::env([
                ("bits", num_list(&bits)),
                ("formats", num_list(&formats)),
                ("spp", num(spp as f64)),
                ("planar", num(planar as f64)),
            ]),
        )?;
        let compression = self.req(k, m, 259, Some(1))?;
        let photometric = if compression == 7 { first_int(m, 262) } else { None };
        Ok((bits[0], spp, formats[0], planar, compression, self.req(k, m, 317, Some(1))?, photometric))
    }

    fn tiled(&self, k: usize) -> bool {
        self.ifds[k].has(322) && self.ifds[k].has(324)
    }

    fn check_size(&self, k: usize, m: &Vals) -> Got<()> {
        let w = self.req(k, m, 256, None)?;
        let h = self.req(k, m, 257, None)?;
        let tiled = self.tiled(k);
        let has_counts = self.ifds[k].has(325);
        let (mut tw, mut th) = (1, 1);
        if tiled && has_counts && w >= 1 && h >= 1 {
            tw = self.req(k, m, 322, None)?;
            th = self.req(k, m, 323, None)?;
        }
        self.check(
            "size",
            rules::env([
                ("offset", num(self.ifds[k].offset as f64)),
                ("width", num(w as f64)),
                ("height", num(h as f64)),
                ("tiled", flag(tiled)),
                ("has_counts", flag(has_counts)),
                ("tile_width", num(tw as f64)),
                ("tile_height", num(th as f64)),
            ]),
        )?;
        Ok(())
    }

    fn check_samples(&self, k: usize, m: &Vals) -> Got<()> {
        let compression = self.req(k, m, 259, Some(1))?;
        let fill = self.req(k, m, 266, Some(1))?;
        let photometric = self.req(k, m, 262, Some(0))?;
        let sub = match m.get(&530) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![2, 2],
        };
        self.check(
            "samples",
            rules::env([
                ("offset", num(self.ifds[k].offset as f64)),
                ("compression", num(compression as f64)),
                ("fill_order", num(fill as f64)),
                ("photometric", num(photometric as f64)),
                ("subsampling", V::List(sub.iter().map(|&x| V::Num(x as f64)).collect())),
            ]),
        )?;
        Ok(())
    }

    /// A level of the IFDs `ks` (one per plane): each loaded and checked.
    fn level(&self, ks: &[usize], vals: &mut HashMap<usize, Vals>) -> Got<Level> {
        let mut uniq: Vec<usize> = ks.to_vec();
        uniq.sort_unstable();
        uniq.dedup();
        let loaded = self.load(&uniq, true, false)?;
        for (k, m) in uniq.iter().zip(loaded) {
            let mut m = m;
            if let Some(old) = vals.get(k) {
                if let Some(d) = old.get(&270) {
                    m.insert(270, d.clone());
                }
            }
            vals.insert(*k, m);
        }
        for &k in ks {
            let m = &vals[&k];
            self.check_size(k, m)?;
            self.fmt(k, m)?;
            self.check_samples(k, m)?;
        }
        for &k in ks {
            self.check(
                "level_ifd",
                rules::env([("offset", num(self.ifds[k].offset as f64)), ("tiled", flag(self.tiled(k))), ("same", flag(true))]),
            )?;
        }
        let k0 = ks[0];
        let m0 = &vals[&k0];
        let dims = |k: usize, m: &Vals| -> Got<(u64, u64, u64, u64)> {
            Ok((
                self.req(k, m, 256, None)?,
                self.req(k, m, 257, None)?,
                self.req(k, m, 322, None)?,
                self.req(k, m, 323, None)?,
            ))
        };
        let d0 = dims(k0, m0)?;
        let f0 = self.fmt(k0, m0)?;
        for &k in ks {
            let same = dims(k, &vals[&k])? == d0 && self.fmt(k, &vals[&k])? == f0;
            self.check(
                "level_ifd",
                rules::env([("offset", num(self.ifds[k].offset as f64)), ("tiled", flag(true)), ("same", flag(same))]),
            )?;
        }
        Ok(Level { w: d0.0, h: d0.1, tw: d0.2, th: d0.3, ifds: ks.to_vec() })
    }

    fn derive(&self) -> Got<Layout> {
        self.check("images", rules::env([("ifds", num(self.chain.len() as f64))]))?;
        let i0 = self.chain[0];
        let mut vals: HashMap<usize, Vals> = HashMap::new();
        let m0 = self.load(&[i0], false, true)?.pop().unwrap();
        vals.insert(i0, m0.clone());
        // IFD 0's ImageDescription: ASCII, up to its first NUL; OME-XML when it is UTF-8 with an OME start tag
        let mut description: Option<String> = None;
        let mut ome: Option<Ome> = None;
        if self.ifds[i0].field(270).is_some_and(|e| e.typ == 2) {
            if let Some(Loaded::Bytes(d)) = m0.get(&270) {
                let raw = d.split(|&c| c == 0).next().unwrap_or(&[]);
                if let Ok(t) = std::str::from_utf8(raw) {
                    ome = read_ome(t, self.rules)?;
                    description = Some(t.to_string());
                }
            }
        }
        let f0 = self.fmt(i0, &m0)?;
        let (bits, spp, sample_format, planar, compression, predictor, photometric) = f0;
        self.check("predictor", rules::env([("compression", num(compression as f64)), ("predictor", num(predictor as f64))]))?;
        // the planes (spec/virtualize/tiff.md §3, §4.1), in (t, c, z) order, as main-chain IFD indices
        let empty = vec![];
        let px = ome.as_ref().map(|o| &o.pixels).unwrap_or(&empty);
        let size_z = int_attr(self.rules, px, "SizeZ", 1, 1)?;
        let size_t = int_attr(self.rules, px, "SizeT", 1, 1)?;
        let mut size_c = int_attr(self.rules, px, "SizeC", spp, 1)?;
        let order = get(px, "DimensionOrder").unwrap_or("XYZCT").to_string();
        if ome.is_some() || (spp > 1 && size_c != spp) {
            self.check(
                "ome_pixels",
                rules::env([("order", text(if ome.is_some() { &order } else { "XYZCT" })), ("spp", num(spp as f64)), ("size_c", num(size_c as f64))]),
            )?;
        }
        if spp > 1 && size_c != spp {
            size_c = spp;
        }
        let plane_c = if spp > 1 { 1 } else { size_c };
        let count = (size_t as u128) * (plane_c as u128) * (size_z as u128);
        let files = ome.as_ref().map(|o| {
            let mut u: Vec<&String> = o.tiff_data.iter().filter_map(|t| t.1.as_ref()).collect();
            u.sort();
            u.dedup();
            u.len()
        });
        self.check("ome_planes", rules::env([("planes", num(count as f64)), ("files", num(0.0))]))?;
        let count = count as usize;
        let mut plane_ifd: Vec<i128> = vec![-1; count];
        let plane = |t: u64, c: u64, z: u64| ((t * plane_c + c) * size_z + z) as usize;
        match &ome {
            None => plane_ifd[0] = 0,
            Some(o) => {
                self.check("ome_planes", rules::env([("planes", num(count as f64)), ("files", num(files.unwrap() as f64))]))?;
                let sizes = |d: u8| match d {
                    b'Z' => size_z,
                    b'C' => plane_c,
                    _ => size_t,
                };
                let default = vec![(vec![], None)];
                let entries = if o.tiff_data.is_empty() { &default } else { &o.tiff_data };
                let bound = 4 * count as u128 + 1000;
                let mut total: u128 = 0;
                let ord = order.as_bytes();
                for (a, _) in entries.iter() {
                    let mut pos = [0u64; 3]; // Z, C, T
                    pos[0] = int_attr(self.rules, a, "FirstZ", 0, 0)?;
                    pos[1] = int_attr(self.rules, a, "FirstC", 0, 0)?;
                    pos[2] = int_attr(self.rules, a, "FirstT", 0, 0)?;
                    let inside = pos[0] < size_z && pos[1] < plane_c && pos[2] < size_t;
                    self.check(
                        "tiff_data",
                        rules::env([("inside", flag(inside)), ("total", num(0.0)), ("bound", num(bound as f64))]),
                    )?;
                    let mut ifd = int_attr(self.rules, a, "IFD", 0, 0)? as i128;
                    let dflt = if entries.len() == 1 && get(a, "IFD").is_none() { count as u64 } else { 1 };
                    let n = int_attr(self.rules, a, "PlaneCount", dflt, 1)? as u128;
                    let idx = |d: u8| match d {
                        b'Z' => 0,
                        b'C' => 1,
                        _ => 2,
                    };
                    let (d0, d1, d2) = (ord[2], ord[3], ord[4]);
                    let first = pos[idx(d0)] as u128
                        + sizes(d0) as u128 * (pos[idx(d1)] as u128 + sizes(d1) as u128 * pos[idx(d2)] as u128);
                    let mut n = n.min(count as u128 - first);
                    total += n;
                    self.check(
                        "tiff_data",
                        rules::env([("inside", flag(true)), ("total", num(total as f64)), ("bound", num(bound as f64))]),
                    )?;
                    'fill: while n > 0 {
                        n -= 1;
                        plane_ifd[plane(pos[2], pos[1], pos[0])] = ifd;
                        ifd += 1;
                        for &d in &ord[2..] {
                            let i = idx(d);
                            pos[i] += 1;
                            if pos[i] < sizes(d) {
                                continue 'fill;
                            }
                            pos[i] = 0;
                        }
                        break;
                    }
                }
            }
        }
        let mapped = plane_ifd.iter().all(|&i| i >= 0 && (i as usize) < self.chain.len());
        self.check("plane_map", rules::env([("mapped", flag(mapped))]))?;
        let planes: Vec<usize> = plane_ifd.iter().map(|&i| self.chain[i as usize]).collect();
        // the levels (spec/virtualize/tiff.md §4.2)
        let mut levels: Vec<Level> = Vec::new();
        let subs0 = self.ifds[i0].subs.clone();
        if !subs0.is_empty() {
            let s = subs0.len();
            for &p in &planes {
                self.check(
                    "subifd_levels",
                    rules::env([
                        ("offset", num(self.ifds[p].offset as f64)),
                        ("subs", num(self.ifds[p].subs.len() as f64)),
                        ("levels", num(s as f64)),
                    ]),
                )?;
            }
            levels.push(self.level(&planes, &mut vals)?);
            for k in 0..s {
                let ks: Vec<usize> = planes.iter().map(|&p| self.ifds[p].subs[k]).collect();
                levels.push(self.level(&ks, &mut vals)?);
            }
        } else {
            levels.push(self.level(&planes, &mut vals)?);
            if ome.is_none() {
                for &k in &self.chain[1..] {
                    let (pw, ph) = (levels.last().unwrap().w, levels.last().unwrap().h);
                    if !(self.tiled(k) && self.ifds[k].has(258)) {
                        continue;
                    }
                    let m = self.load(&[k], false, false)?.pop().unwrap();
                    self.check_size(k, &m)?;
                    let (w, h) = (self.req(k, &m, 256, None)?, self.req(k, &m, 257, None)?);
                    let f = self.fmt(k, &m)?;
                    vals.entry(k).or_insert(m);
                    if f == f0 && w < pw && h < ph {
                        levels.push(self.level(&[k], &mut vals)?);
                    }
                }
            }
        }
        for lv in &levels {
            for &k in &lv.ifds {
                let same = self.fmt(k, &vals[&k])? == f0;
                self.check("pyramid", rules::env([("same", flag(same))]))?;
            }
        }
        // data type and codecs (spec/virtualize/tiff.md §4.3)
        let mut env = rules::env([
            ("bits", num(bits as f64)),
            ("sample_format", num(sample_format as f64)),
            ("spp", num(spp as f64)),
            ("planar", num(planar as f64)),
            ("compression", num(compression as f64)),
            ("photometric", photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
        ]);
        self.rules.check("image", &mut env)?;
        // the tiles of every plane of every level
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        let jpeg = compression == 7;
        let mut prefixes = HashMap::new();
        for lv in &levels {
            let across = lv.w.div_ceil(lv.tw);
            let down = lv.h.div_ceil(lv.th);
            let per = across as u128 * down as u128;
            for &k in &lv.ifds {
                let m = &vals[&k];
                let offsets = self.reqs(k, m, 324)?;
                let counts = self.reqs(k, m, 325)?;
                let mut prefix = Vec::new();
                let (mut has_tables, mut tables_ok) = (false, true);
                if jpeg {
                    if spp == 3 {
                        prefix.extend_from_slice(&ADOBE);
                        prefix.push(if photometric == Some(2) { 0 } else { 1 });
                    }
                    if let Some(Loaded::Bytes(t)) = m.get(&347) {
                        has_tables = true;
                        tables_ok = t.len() >= 4 && t[..2] == [0xFF, 0xD8] && t[t.len() - 2..] == [0xFF, 0xD9];
                        if tables_ok {
                            prefix.extend_from_slice(&t[2..t.len() - 2]);
                        }
                    }
                }
                self.check(
                    "plane",
                    rules::env([
                        ("offset", num(self.ifds[k].offset as f64)),
                        ("jpeg", flag(jpeg)),
                        ("has_tables", flag(has_tables)),
                        ("tables_ok", flag(tables_ok)),
                        ("tiles", num(offsets.len() as f64)),
                        ("counts", num(counts.len() as f64)),
                        ("expected", num((samples as u128 * per) as f64)),
                    ]),
                )?;
                if prefixes.contains_key(&k) {
                    continue;
                }
                for (j, (&o, &c)) in offsets.iter().zip(&counts).enumerate() {
                    if c == 0 {
                        continue;
                    }
                    let within = (o as u128 + c as u128) <= self.size as u128;
                    self.check(
                        "tile",
                        rules::env([
                            ("k", num(j as f64)),
                            ("offset", num(self.ifds[k].offset as f64)),
                            ("within", flag(within)),
                            ("count", num(c as f64)),
                            ("jpeg", flag(jpeg)),
                        ]),
                    )?;
                }
                if jpeg {
                    prefixes.insert(k, prefix);
                } else {
                    prefixes.insert(k, Vec::new());
                }
            }
        }
        // pixel size and position (spec/virtualize/tiff.md §4.4)
        let unit_of = |u: &str| self.rules.table("units").get(u).and_then(|x| x.as_str()).map(|x| x.to_string());
        let length = |u: &str| self.rules.table("lengths").get(u).and_then(|x| x.as_f64());
        let mut units: BTreeMap<char, String> = BTreeMap::new();
        let mut sizes: HashMap<char, f64> = HashMap::new();
        let mut centre: Option<(f64, f64)> = None;
        let mut corner: Option<(f64, f64)> = None;
        if let Some(o) = &ome {
            for (d, a) in [("Z", 'z'), ("Y", 'y'), ("X", 'x')] {
                if let Some(v) = get(&o.pixels, &format!("PhysicalSize{d}")).and_then(|v| xml::decimal_value(v, true)) {
                    sizes.insert(a, v);
                    if let Some(u) = unit_of(get(&o.pixels, &format!("PhysicalSize{d}Unit")).unwrap_or("µm")) {
                        units.insert(a, u);
                    }
                }
            }
            let stage = o.plane.clone().unwrap_or_default();
            let pos = |d: &str| get(&stage, &format!("Position{d}")).and_then(|v| xml::decimal_value(v, false));
            let pos_unit = |d: &str| unit_of(get(&stage, &format!("Position{d}Unit")).unwrap_or(""));
            let mut c = [0.0; 2];
            let mut all = true;
            for (i, (d, a)) in [("X", 'x'), ("Y", 'y')].into_iter().enumerate() {
                match (pos(d), pos_unit(d).and_then(|u| length(&u)), units.get(&a).and_then(|u| length(u))) {
                    (Some(p), Some(lp), Some(lu)) => c[i] = p * (lp / lu),
                    _ => all = false,
                }
            }
            if all {
                centre = Some((c[0], c[1]));
            }
        } else {
            let fields = description.as_deref().and_then(aperio_fields);
            let mpp = fields.as_ref().and_then(|f| get(f, "MPP")).and_then(|v| xml::decimal_value(v, true));
            if let Some(mpp) = mpp {
                sizes.insert('x', mpp);
                sizes.insert('y', mpp);
                units.insert('x', "micrometer".into());
                units.insert('y', "micrometer".into());
                let f = fields.unwrap();
                if let (Some(l), Some(t)) = (get(&f, "Left").and_then(|v| xml::decimal_value(v, false)), get(&f, "Top").and_then(|v| xml::decimal_value(v, false))) {
                    corner = Some((l * 1000.0, t * 1000.0));
                }
            } else {
                let per_unit = self.rules.table("resolution_units").get(first_int(&vals[&i0], 296).unwrap_or(0).to_string()).and_then(|x| x.as_f64());
                for (tag, a) in [(282u16, 'x'), (283, 'y')] {
                    if let (Some(pu), Some(Loaded::Pairs(r))) = (per_unit, vals[&i0].get(&tag)) {
                        let (n, d) = r[0];
                        if n > 0 && d > 0 {
                            let pixel = pu / (n as f64 / d as f64);
                            if pixel < 25.4 {
                                sizes.insert(a, pixel);
                                units.insert(a, "micrometer".into());
                            }
                        }
                    }
                }
            }
        }
        let axes: Vec<char> = [('t', size_t > 1), ('c', size_c > 1), ('z', size_z > 1), ('y', true), ('x', true)]
            .into_iter()
            .filter(|x| x.1)
            .map(|x| x.0)
            .collect();
        let size_of = |a: char| sizes.get(&a).copied();
        let (bw, bh) = (levels[0].w as f64, levels[0].h as f64);
        let scales: Vec<Vec<J>> = levels
            .iter()
            .map(|lv| {
                axes.iter()
                    .map(|&a| match a {
                        't' | 'c' => json!(1),
                        'z' => size_of('z').map(|v| json!(v)).unwrap_or(json!(1)),
                        'y' => json!(size_of('y').unwrap_or(1.0) * (bh / lv.h as f64)),
                        _ => json!(size_of('x').unwrap_or(1.0) * (bw / lv.w as f64)),
                    })
                    .collect()
            })
            .collect();
        let mut translation = J::Null;
        if units.contains_key(&'x') && units.contains_key(&'y') {
            if let Some((cx, cy)) = centre {
                let (sx, sy) = (size_of('x').unwrap_or(1.0), size_of('y').unwrap_or(1.0));
                corner = Some((cx - bw * sx / 2.0, cy - bh * sy / 2.0));
            }
            if let Some((x, y)) = corner {
                self.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
                translation = J::Array(axes.iter().map(|&a| match a {
                    'x' => json!(x),
                    'y' => json!(y),
                    _ => json!(0),
                }).collect());
            }
        }
        let name = ome.as_ref().and_then(|o| o.name.clone()).filter(|n| !n.is_empty());
        let pair = |m: &Vals, t: u16| match m.get(&t) {
            Some(Loaded::Pairs(p)) => json!([p[0].0, p[0].1]),
            _ => J::Null,
        };
        let m0 = &vals[&i0];
        let facts = json!({
            "little": self.le,
            "bigtiff": self.big,
            "ifds": self.chain.len(),
            "bits": bits,
            "spp": spp,
            "sample_format": sample_format,
            "planar": planar,
            "compression": compression,
            "photometric": photometric,
            "sizes": {"t": size_t, "c": size_c, "z": size_z},
            "plane_c": plane_c,
            "ome": ome.as_ref().map(|o| json!({
                "name": o.name,
                "pixels": o.pixels.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>(),
                "plane": o.plane.as_ref().map(|p| p.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>()),
            })),
            "axes": axes.iter().map(|a| a.to_string()).collect::<Vec<_>>(),
            "units": units.iter().map(|(a, u)| (a.to_string(), J::String(u.clone()))).collect::<serde_json::Map<_, _>>(),
            "scales": scales,
            "translation": translation,
            "name": name,
            "resolution_unit": first_int(m0, 296).unwrap_or(0),
            "x_resolution": pair(m0, 282),
            "y_resolution": pair(m0, 283),
        });
        let _ = &vals;
        Ok(Layout { facts, prefixes, levels, planes: ome.as_ref().map(|_| planes.clone()) })
    }
}

// ---- the strips and tiles, the planes, and the IFDs other pointer tags lead to

impl Tiff {
    /// An IFD's JPEG prefix by its own tags (for an IFD no level uses).
    fn own_prefix(&self, k: usize) -> Vec<u8> {
        let f = &self.ifds[k];
        let one = |t: u16| f.field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied());
        let mut out = Vec::new();
        if one(259).unwrap_or(1) != 7 {
            return out;
        }
        if one(277).unwrap_or(1) == 3 {
            out.extend_from_slice(&ADOBE);
            out.push(if one(262) == Some(2) { 0 } else { 1 });
        }
        if let Some(t) = f.field(347).and_then(|e| self.bytes_of(e, e.nbytes.unwrap_or(0))) {
            if t.len() >= 4 && t[..2] == [0xFF, 0xD8] && t[t.len() - 2..] == [0xFF, 0xD9] {
                out.extend_from_slice(&t[2..t.len() - 2]);
            }
        }
        out
    }

    /// The IFD's strips and tiles as `data` elements, with geometry,
    /// codec and recipe; an IFD whose tables and codec are an earlier IFD's names
    /// its tiles by alias. Returns the tiles struct (or what its alias names).
    fn emit_ifd_data(&mut self, k: usize, by_key: &mut HashMap<String, u32>) -> Res<Option<u32>> {
        let mut tiles = None;
        for (name, ot, ct) in [("tiles", 324u16, 325u16), ("strips", 273, 279)] {
            let f = &self.ifds[k];
            let (Some(eo), Some(ec)) = (f.field(ot).cloned(), f.field(ct).cloned()) else { continue };
            if !unsigned(eo.typ) || !unsigned(ec.typ) {
                continue;
            }
            let (Some(offsets), Some(counts)) = (self.ints(&eo, false), self.ints(&ec, false)) else { continue };
            let prefix = match self.layout.as_ref().and_then(|l| l.prefixes.get(&k)) {
                Some(p) => p.clone(),
                None => self.own_prefix(k),
            };
            let one = |t: u16| f.field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied());
            let (rows, cols) = if name == "tiles" {
                (one(323), one(322))
            } else {
                (one(278).or(one(257)), one(256))
            };
            let base = json!({
                "geometry": {"shape": [rows, cols], "samples": one(277).unwrap_or(1),
                             "bits": f.field(258).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied())},
                "codec": {"compression": one(259).unwrap_or(1)},
            });
            let at = |e: &Ent| if e.inline { format!("i{}", e.field.len()) } else { format!("{}+{}", e.off, e.nbytes.unwrap_or(0)) };
            let key = if eo.inline || ec.inline {
                format!("{k}/{name}") // in-line tables are the IFD's own
            } else {
                format!("{name}|{}|{}|{}|{}", at(&eo), at(&ec), base, prefix.iter().map(|b| format!("{b:02x}")).collect::<String>())
            };
            let parent = self.ifds[k].el;
            if let Some(&t) = by_key.get(&key) {
                self.ir.alias(parent, name, NO_INDEX, t)?;
                if name == "tiles" {
                    tiles = Some(t);
                }
                continue;
            }
            let s = self.ir.struct_(parent, name, NO_INDEX, 0, None)?;
            by_key.insert(key, s);
            let shared = if prefix.is_empty() { None } else { Some(self.ir.share(&prefix)) };
            let plain = {
                let mut j = base.clone();
                j["recipe"] = json!([["src", 0, null]]);
                j.to_string()
            };
            let framed = shared.map(|x| {
                let mut j = base.clone();
                j["recipe"] = json!([["src", 0, 2], ["shared", x], ["src", 2, null]]);
                j.to_string()
            });
            // one element a tile, not folded into runs: the mirror folds the tiles of a
            // table into one column run whose extents it reads from the table itself
            for (j, (&o, &c)) in offsets.iter().zip(&counts).enumerate() {
                if c == 0 || !self.within(o, c) {
                    continue;
                }
                let form = match &framed {
                    Some(fr) if c > 2 => fr.as_str(),
                    _ => plain.as_str(),
                };
                self.ir.data(s, "", j as u64, 0, (o, c), form)?;
            }
            if name == "tiles" {
                tiles = Some(s);
            }
        }
        Ok(tiles)
    }

    fn emit_data(&mut self) -> Res<()> {
        let mut by_key = HashMap::new();
        let mut tiles_of: HashMap<usize, Option<u32>> = HashMap::new();
        for k in 0..self.ifds.len() {
            let t = self.emit_ifd_data(k, &mut by_key)?;
            tiles_of.insert(k, t);
        }
        self.data_keys = by_key;
        let lay = self.layout.as_mut().unwrap();
        let levels: Vec<J> = lay
            .levels
            .iter()
            .map(|lv| {
                json!({"w": lv.w, "h": lv.h, "tw": lv.tw, "th": lv.th,
                       "planes": lv.ifds.iter().map(|k| tiles_of.get(k).copied().flatten()).collect::<Vec<_>>()})
            })
            .collect();
        lay.facts["levels"] = J::Array(levels);
        if let Some(planes) = lay.planes.clone() {
            let o = self.ir.struct_(0, "ome", NO_INDEX, 0, None)?;
            for (p, k) in planes.iter().enumerate() {
                let el = self.ifds[*k].el;
                self.ir.alias(o, "planes/", p as u64, el)?;
            }
        }
        Ok(())
    }

    /// The pointer tags of the IFDs read, in tree order (spec/virtualize/tiff.md §5).
    fn start_pointers(&mut self) {
        self.todo.clear();
        let mut ks: Vec<usize> = (0..self.ifds.len()).collect();
        ks.sort_by_key(|&k| self.ifds[k].el);
        self.queue_pointers(&ks);
    }

    fn queue_pointers(&mut self, ks: &[usize]) {
        for &k in ks {
            let f = &self.ifds[k];
            for e in &f.entries {
                if e.dup || !pointer(e.tag, e.typ) || (e.tag == 330 && f.tree) {
                    continue;
                }
                let Some(v) = self.ints(e, false) else { continue };
                let l = label(e.tag);
                for (j, &o) in v.iter().enumerate() {
                    let name = if v.len() > 1 { format!("{l}/{j}") } else { l.clone() };
                    self.todo.push((k, e.tag, name, o));
                }
            }
        }
    }

    /// The IFDs pointer tags lead to: an alias when read before; else, within
    /// depth 4 and 10,000 tries, the IFD at the offset when its entries lie within
    /// the file and it overlaps no IFD read. None when there are none left.
    fn pointers(&mut self) -> Res<Option<Step>> {
        loop {
            match self.ptr_state {
                0 => {
                    if self.todo.is_empty() {
                        return Ok(None);
                    }
                    let todo = std::mem::take(&mut self.todo);
                    let max_depth = self.rules.limit("max_depth") as u32;
                    let max_tries = self.rules.limit("max_tries") as u64;
                    let mut cands = Vec::new();
                    for (k, _tag, name, o) in todo {
                        let parent = self.ifds[k].el;
                        if let Some(&t) = self.by_offset.get(&o) {
                            let el = self.ifds[t].el;
                            self.ir.alias(parent, &name, NO_INDEX, el)?;
                        } else if self.ifds[k].depth < max_depth && self.tries < max_tries {
                            self.tries += 1;
                            if o >= self.hs && self.within(o, self.cs()) {
                                cands.push(Pending {
                                    parent,
                                    name,
                                    nidx: NO_INDEX,
                                    offset: o,
                                    depth: self.ifds[k].depth + 1,
                                    tree: false,
                                    from: None,
                                });
                            }
                        }
                    }
                    if cands.is_empty() {
                        continue;
                    }
                    self.pending = cands;
                    self.ptr_state = 1;
                    let r = self.heads_of_pending();
                    return Ok(Some(self.ask(r)));
                }
                1 => {
                    // keep the candidates whose entries are within the file and overlap no IFD read
                    let pending = std::mem::take(&mut self.pending);
                    let mut keep = Vec::new();
                    let mut more = Vec::new();
                    let mut accepted = HashSet::new();
                    for p in pending {
                        if accepted.contains(&p.offset) {
                            keep.push(p); // read in this batch: an alias of it
                            continue;
                        }
                        let Some(c) = self.fed.get(p.offset, self.cs()) else { continue };
                        let count = self.word_n(c);
                        let Some(n) = self.ifd_len(count) else { continue };
                        let end = p.offset.saturating_add(n);
                        if count == 0 || !self.within(p.offset, n) || self.by_offset.contains_key(&p.offset) {
                            continue;
                        }
                        if let Some((_, &e)) = self.extents.range(..end).next_back() {
                            if e > p.offset {
                                continue;
                            }
                        }
                        self.extents.insert(p.offset, end);
                        accepted.insert(p.offset);
                        if self.fed.get(p.offset, n).is_none() {
                            more.push((p.offset, n));
                        }
                        keep.push(p);
                    }
                    for o in &accepted {
                        self.extents.remove(o);
                    }
                    self.pending = keep;
                    self.ptr_state = 2;
                    if !more.is_empty() {
                        return Ok(Some(self.ask(more)));
                    }
                }
                2 => {
                    let pending = std::mem::take(&mut self.pending);
                    let mut made = Vec::new();
                    for p in pending {
                        if self.by_offset.contains_key(&p.offset) {
                            let el = self.ifds[self.by_offset[&p.offset]].el;
                            self.ir.alias(p.parent, &p.name, NO_INDEX, el)?;
                            continue;
                        }
                        let c = self.fed.get(p.offset, self.cs()).ok_or("internal: no pointer IFD count")?;
                        let n = self.ifd_len(self.word_n(c)).unwrap();
                        let body = self.fed.get(p.offset, n).ok_or("internal: no pointer IFD body")?.to_vec();
                        made.push(self.ifd(&p, &body)?);
                    }
                    self.fed.clear();
                    self.made = made;
                    self.ptr_state = 3;
                }
                _ => {
                    if let Some(s) = self.read_values()? {
                        return Ok(Some(s));
                    }
                    let made = std::mem::take(&mut self.made);
                    let mut by_key = std::mem::take(&mut self.data_keys);
                    for &k in &made {
                        self.emit_ifd_data(k, &mut by_key)?;
                    }
                    self.data_keys = by_key;
                    self.queue_pointers(&made);
                    self.ptr_state = 0;
                }
            }
        }
    }

    /// The IR, finished (aliases and gaps), and the facts the projection reads.
    pub fn finish(mut self) -> Result<(Ir, J), String> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        self.ir.sort_values();
        check::finish(&mut self.ir)?;
        let mut facts = self.layout.take().map(|l| l.facts).unwrap_or(J::Null);
        facts["requests"] = json!(self.requests);
        facts["requested"] = json!(self.requested);
        facts["rounds"] = json!(self.rounds);
        Ok((self.ir, facts))
    }
}
