//! A sans-IO parser of ND2 files into the IR (spec/virtualize/nd2.md, spec/virtualize/nd2.md):
//! it never reads; it asks for batches of ranges (`step`), is given their bytes
//! (`feed`), and holds them only until the step that uses them.
//!
//! Rounds: (1) the signature and the tail; (2) the chunk map's header; (3) its
//! data; (4) every chunk's 16-byte header, in one batch; (5) the three chunks the
//! convention reads, and `CustomDataVar|CustomDataV2_0!`; (6) the chunks the
//! source metadata decodes, within its budget. The schema (`schema/nd2.json`) is
//! checked after round 5: a file that fails is rejected there, and nowhere else.

use crate::check;
use crate::ir::*;
use crate::lv::{self, Decoded};
use crate::schema::{self, FrameHead};
use crate::xml;
use std::collections::{BTreeMap, HashMap};

pub const FILE_SIGNATURE: &[u8] = b"ND2 FILE SIGNATURE CHUNK NAME01!";
pub const MAP_SIGNATURE: &[u8] = b"ND2 CHUNK MAP SIGNATURE 0000001!";
pub const FILEMAP_NAME: &[u8] = b"ND2 FILEMAP SIGNATURE NAME 0001!";
pub const DECLARATIONS: &[u8] = b"CustomDataVar|CustomDataV2_0!";
const PROFILE: [&[u8]; 3] = [
    b"ImageAttributesLV!",
    b"ImageMetadataLV!",
    b"ImageMetadataSeqLV|0!",
];
const STREAMS: [&str; 8] = [
    "AcqTimesCache",
    "AcqTimes2Cache",
    "X",
    "Y",
    "Z",
    "Z1",
    "Z2",
    "AcqFramesCache",
];
const HEADER: &str = "{magic:<u4,name_length:<u4,data_length:<u8}";

pub enum Step {
    Read(Vec<(u64, u64)>),
    Done,
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Head,
    MapHeader,
    MapName,
    MapData,
    Heads,
    Profile,
    Meta,
    Done,
}

/// How a chunk's data was decoded for the source metadata.
enum Tree {
    Lv(Decoded),
    Xml(xml::El),
}

pub struct Nd2 {
    size: u64,
    phase: Phase,
    asked: Vec<(u64, u64)>,
    got: BTreeMap<u64, Vec<u8>>,
    pub ir: Ir,
    map: (u64, u32, u64), // offset, name length, data length
    /// the map's chunks, in map order: (name, offset of its last record)
    chunks: Vec<(Vec<u8>, u64)>,
    index: HashMap<Vec<u8>, usize>,
    heads: Vec<Option<(u32, u32, u64)>>,
    profile: [Option<Decoded>; 3],
    facts: Option<schema::Facts>,
    trees: HashMap<usize, Tree>,
    /// the chunks the source metadata decodes, in map order: (chunk, element,
    /// records and byte-array bytes: a lower bound of its JSON's size)
    decoded: Vec<(usize, u32, u64)>,
    used: u64,
    declared: Vec<(String, &'static str, usize)>,
    pub requests: u64,
    pub requested: u64,
}

fn u32le(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}

fn u64le(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}

pub fn decode_text(b: &[u8]) -> String {
    match std::str::from_utf8(b) {
        Ok(s) => s.to_string(),
        Err(_) => b.iter().map(|&c| c as char).collect(),
    }
}

/// `ImageDataSeq|<f>!` with f decimal without leading zeros, at most 20 digits: f.
fn frame_index(name: &[u8]) -> Option<u128> {
    let d = name.strip_prefix(b"ImageDataSeq|")?.strip_suffix(b"!")?;
    if d.is_empty()
        || d.len() > 20
        || !d.iter().all(|c| c.is_ascii_digit())
        || (d.len() > 1 && d[0] == b'0')
    {
        return None;
    }
    Some(d.iter().fold(0u128, |a, &c| a * 10 + (c - b'0') as u128))
}

fn is_family(name: &[u8]) -> bool {
    let Some(rest) = name
        .strip_prefix(b"CustomDataSeq|")
        .and_then(|r| r.strip_suffix(b"!"))
    else {
        return false;
    };
    let Some(bar) = rest.iter().rposition(|&c| c == b'|') else {
        return false;
    };
    let (mid, d) = (&rest[..bar], &rest[bar + 1..]);
    !mid.is_empty()
        && !d.is_empty()
        && d.iter().all(|c| c.is_ascii_digit())
        && (d.len() == 1 || d[0] != b'0')
}

fn lv_named(name: &[u8]) -> bool {
    name.ends_with(b"LV!") || name.windows(3).any(|w| w == b"LV|")
}

fn max_safe() -> u64 {
    (1 << 53) - 1
}

impl Nd2 {
    pub fn new(size: u64) -> Self {
        Nd2 {
            size,
            phase: Phase::Start,
            asked: Vec::new(),
            got: BTreeMap::new(),
            ir: {
                // the records the convention's decode limits allow: its own chunks' and
                // the source metadata's (each LV record is at least 3 bytes)
                let mut ir = Ir::new(size);
                if let Some(b) = ir.budget.as_mut() {
                    b.records += schema::limit("profile_records") as u64
                        + schema::limit("source_budget") as u64 / 3;
                }
                ir
            },
            map: (0, 0, 0),
            chunks: Vec::new(),
            index: HashMap::new(),
            heads: Vec::new(),
            profile: [None, None, None],
            facts: None,
            trees: HashMap::new(),
            decoded: Vec::new(),
            used: 0,
            declared: Vec::new(),
            requests: 0,
            requested: 0,
        }
    }

    /// The bytes of a requested range (or of part of one).
    fn bytes(&self, o: u64, n: u64) -> Result<&[u8], String> {
        // the range may lie in a merged response that starts before a smaller one
        for (&s, b) in self.got.range(..=o).rev().take(64) {
            if o + n <= s + b.len() as u64 {
                return Ok(&b[(o - s) as usize..(o - s + n) as usize]);
            }
        }
        Err(format!("internal: bytes [{o}, {}) were not read", o + n))
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.got.insert(offset, data);
    }

    fn ask(&mut self, ranges: Vec<(u64, u64)>) -> Step {
        let ranges: Vec<(u64, u64)> = ranges.into_iter().filter(|r| r.1 > 0).collect();
        self.requests += ranges.len() as u64;
        self.requested += ranges.iter().map(|r| r.1).sum::<u64>();
        self.asked = ranges.clone();
        Step::Read(ranges)
    }

    fn outside(&self, o: u64, n: u64) -> bool {
        o.checked_add(n).is_none_or(|e| e > self.size)
    }

    /// The next batch of ranges to read, having used what was fed.
    pub fn step(&mut self) -> Result<Step, String> {
        for &(o, n) in &self.asked {
            self.bytes(o, n)?;
        }
        let r = self.advance();
        if r.is_err() {
            self.got.clear();
        }
        r
    }

    fn advance(&mut self) -> Result<Step, String> {
        let size = self.size;
        match self.phase {
            Phase::Start => {
                if size < 112 {
                    return Err("not an ND2 file (too short for its signature chunk)".into());
                }
                self.phase = Phase::Head;
                Ok(self.ask(vec![(0, 112), (size - 40, 40)]))
            }
            Phase::Head => {
                let h = self.bytes(0, 112)?.to_vec();
                let (magic, n, d) = (u32le(&h, 0), u32le(&h, 4), u64le(&h, 8));
                if magic != schema::limit("chunk_magic") as u32 {
                    return Err("no ND2 chunk at 0".into());
                }
                if &h[16..48] != FILE_SIGNATURE || n != 32 || d != 64 {
                    return Err("not an ND2 file (bad signature chunk)".into());
                }
                let ver = &h[48..112];
                let digits: Vec<u8> = ver
                    .iter()
                    .skip(3)
                    .take_while(|c| c.is_ascii_digit())
                    .copied()
                    .collect();
                let ok = ver.starts_with(b"Ver")
                    && !digits.is_empty()
                    && ver.get(3 + digits.len()) == Some(&b'.');
                let major = digits
                    .iter()
                    .fold(0u128, |a, &c| (a * 10 + (c - b'0') as u128).min(1 << 64));
                if !ok || major < 3 {
                    return Err(format!(
                        "unsupported ND2 version {:?}",
                        String::from_utf8_lossy(&ver[..8])
                    ));
                }
                let tail = self.bytes(size - 40, 40)?.to_vec();
                if &tail[..32] != MAP_SIGNATURE {
                    return Err("no ND2 chunk map signature".into());
                }
                let m = u64le(&tail, 32);
                let ir = &mut self.ir;
                let s = ir.struct_(0, "signature", NO_INDEX, 0, Some((0, 112)))?;
                ir.value(s, "header", NO_INDEX, HEADER, 0, (0, 16), Some(&h[..16]))?;
                ir.value(
                    s,
                    "name",
                    NO_INDEX,
                    "ascii[32]",
                    0,
                    (16, 32),
                    Some(&h[16..48]),
                )?;
                ir.value(
                    s,
                    "data",
                    NO_INDEX,
                    "ascii[64]",
                    0,
                    (48, 64),
                    Some(&h[48..112]),
                )?;
                ir.value(
                    0,
                    "tail",
                    NO_INDEX,
                    "{signature:ascii[32],offset:<u8}",
                    0,
                    (size - 40, 40),
                    Some(&tail),
                )?;
                if m > max_safe() {
                    return Err(format!("chunk offset {m} is too large"));
                }
                if self.outside(m, 16) {
                    return Err(format!(
                        "read of [{m}, {}) outside the {size}-byte file",
                        m + 16
                    ));
                }
                self.map.0 = m;
                self.phase = Phase::MapHeader;
                let n = (16 + 64).min(size - m);
                Ok(self.ask(vec![(m, n)]))
            }
            Phase::MapHeader | Phase::MapName => {
                let m = self.map.0;
                let h = self.bytes(m, 16)?.to_vec();
                let (magic, n, d) = (u32le(&h, 0), u32le(&h, 4), u64le(&h, 8));
                if magic != schema::limit("chunk_magic") as u32 {
                    return Err(format!("no ND2 chunk at {m}"));
                }
                if d > max_safe() {
                    return Err("chunk length too large".into());
                }
                if self.outside(m + 16, n as u64) {
                    return Err("the chunk map's name lies outside the file".into());
                }
                if self.bytes(m + 16, n as u64).is_err() {
                    self.phase = Phase::MapName;
                    return Ok(self.ask(vec![(m + 16, n as u64)]));
                }
                let name = self.bytes(m + 16, n as u64)?;
                let name = name.split(|&c| c == 0).next().unwrap_or(&[]);
                if name != FILEMAP_NAME {
                    return Err("bad ND2 chunk map chunk".into());
                }
                if self.outside(m + 16 + n as u64, d) {
                    return Err("the chunk map's data lies outside the file".into());
                }
                self.map = (m, n, d);
                self.phase = Phase::MapData;
                Ok(self.ask(vec![(m + 16 + n as u64, d)]))
            }
            Phase::MapData => self.map_data(),
            Phase::Heads => self.heads_done(),
            Phase::Profile => self.profile_done(),
            Phase::Meta => {
                self.meta_done()?;
                self.phase = Phase::Done;
                self.got.clear();
                Ok(Step::Done)
            }
            Phase::Done => Ok(Step::Done),
        }
    }

    fn map_data(&mut self) -> Result<Step, String> {
        let (m, n, d) = self.map;
        let start = m + 16 + n as u64;
        let data = self.bytes(start, d)?.to_vec();
        let head = self.bytes(m, 16)?.to_vec();
        let name = self.bytes(m + 16, n as u64)?.to_vec();
        let mut records: Vec<(usize, usize)> = Vec::new(); // (name start, record end) of each record
        let mut pos = 0usize;
        let end_at;
        loop {
            let Some(e) = data[pos..].iter().position(|&c| c == b'!').map(|k| pos + k) else {
                return Err("unterminated ND2 chunk map".into());
            };
            let cname = &data[pos..e + 1];
            if cname == MAP_SIGNATURE {
                end_at = (pos, e + 1);
                break;
            }
            if e + 17 > data.len() {
                return Err("truncated ND2 chunk map record".into());
            }
            let offset = u64le(&data, e + 1);
            match self.index.get(cname) {
                Some(&k) => self.chunks[k].1 = offset,
                None => {
                    self.index.insert(cname.to_vec(), self.chunks.len());
                    self.chunks.push((cname.to_vec(), offset));
                }
            }
            records.push((pos, e + 17));
            pos = e + 17;
        }
        let ir = &mut self.ir;
        let s = ir.struct_(0, "map", NO_INDEX, 0, Some((m, 16 + n as u64 + d)))?;
        ir.value(s, "header", NO_INDEX, HEADER, 0, (m, 16), Some(&head))?;
        ir.value(
            s,
            "name",
            NO_INDEX,
            &format!("bytes[{n}]"),
            0,
            (m + 16, n as u64),
            Some(&name),
        )?;
        for (i, &(a, b)) in records.iter().enumerate() {
            let k = b - a - 16;
            ir.value(
                s,
                "records/",
                i as u64,
                &format!("{{name:bytes[{k}],offset:<u8,size:<u8}}"),
                0,
                (start + a as u64, (b - a) as u64),
                Some(&data[a..b]),
            )?;
        }
        ir.value(
            s,
            "end",
            NO_INDEX,
            "bytes[32]",
            0,
            (start + end_at.0 as u64, 32),
            Some(&data[end_at.0..end_at.1]),
        )?;
        let rest = d - end_at.1 as u64;
        if rest > 0 {
            ir.value(
                s,
                "rest",
                NO_INDEX,
                &format!("bytes[{rest}]"),
                0,
                (start + end_at.1 as u64, rest),
                None,
            )?;
        }
        self.got.clear();
        let size = self.size;
        let ranges: Vec<(u64, u64)> = self
            .chunks
            .iter()
            .filter(|c| c.1 <= size.saturating_sub(16))
            .map(|c| (c.1, 16))
            .collect();
        self.phase = Phase::Heads;
        Ok(self.ask(ranges))
    }

    /// A chunk's data (offset, length), when its header lies within the file, has
    /// the magic, and its data lies within the file (the chunk takes part).
    fn data_of(&self, k: usize) -> Option<(u64, u64)> {
        let (magic, n, d) = self.heads[k]?;
        if magic != schema::limit("chunk_magic") as u32 {
            return None;
        }
        let start = self.chunks[k].1 + 16 + n as u64;
        if start.checked_add(d).is_none_or(|e| e > self.size) {
            return None;
        }
        Some((start, d))
    }

    fn heads_done(&mut self) -> Result<Step, String> {
        let size = self.size;
        let mut heads = Vec::with_capacity(self.chunks.len());
        for (_, o) in &self.chunks {
            heads.push(if *o <= size.saturating_sub(16) {
                let h = self.bytes(*o, 16)?;
                Some((u32le(h, 0), u32le(h, 4), u64le(h, 8)))
            } else {
                None
            });
        }
        self.heads = heads;
        self.got.clear();
        // the three chunks the convention reads: their headers MUST be chunk headers
        let mut ranges = Vec::new();
        if !self.index.contains_key(PROFILE[0]) {
            return Err("no ImageAttributesLV! chunk".into());
        }
        for name in PROFILE {
            let Some(&k) = self.index.get(name) else {
                continue;
            };
            let o = self.chunks[k].1;
            if o > max_safe() {
                return Err(format!("chunk offset {o} is too large"));
            }
            let Some((magic, n, d)) = self.heads[k] else {
                return Err(format!(
                    "read of [{o}, {}) outside the {size}-byte file",
                    o.saturating_add(16)
                ));
            };
            if magic != schema::limit("chunk_magic") as u32 {
                return Err(format!("no ND2 chunk at {o}"));
            }
            if d > max_safe() {
                return Err("chunk length too large".into());
            }
            if self.outside(o + 16, n as u64) || self.outside(o + 16 + n as u64, d) {
                return Err(format!("chunk {} lies outside the file", decode_text(name)));
            }
            ranges.push((o + 16 + n as u64, d));
        }
        if let Some(&k) = self.index.get(DECLARATIONS) {
            if let Some((s, d)) = self.data_of(k) {
                if d > 0 && d <= schema::limit("source_budget") as u64 {
                    ranges.push((s, d));
                }
            }
        }
        self.phase = Phase::Profile;
        Ok(self.ask(ranges))
    }

    fn frame_heads(&self) -> Vec<FrameHead> {
        let mut out = Vec::new();
        for (k, (name, o)) in self.chunks.iter().enumerate() {
            if let Some(f) = frame_index(name) {
                if f < u64::MAX as u128 {
                    out.push(FrameHead {
                        f: f as u64,
                        offset: *o,
                        head: self.heads[k],
                    });
                }
            }
        }
        out
    }

    fn profile_done(&mut self) -> Result<Step, String> {
        let mut room: i64 = schema::limit("profile_records") as i64;
        for (i, name) in PROFILE.iter().enumerate() {
            let Some(&k) = self.index.get(*name) else {
                continue;
            };
            let (_, n, d) = self.heads[k].unwrap();
            let start = self.chunks[k].1 + 16 + n as u64;
            let data = self.bytes(start, d)?;
            let a = lv::decode(
                data,
                Some(schema::limit("inflate") as u64),
                false,
                Some(&mut room),
            );
            match a.result {
                Ok(dec) => self.profile[i] = Some(dec),
                Err(lv::LvErr::TooLarge) => {
                    return Err("the profile's chunks hold more than 1048576 LV records and byte-array bytes".into());
                }
                Err(e) => return Err(e.message()),
            }
        }
        let frames = self.frame_heads();
        fn view(d: &Option<Decoded>) -> Option<(&[lv::Rec], &[u8])> {
            d.as_ref()
                .map(|x| (x.records.as_slice(), x.bytes.as_slice()))
        }
        let facts = schema::check_nd2(
            view(&self.profile[0]),
            view(&self.profile[1]),
            view(&self.profile[2]),
            &frames,
            self.size,
        )?;
        self.facts = Some(facts);
        // the source metadata: the declarations first
        let budget = schema::limit("source_budget") as u64;
        if let Some(&k) = self.index.get(DECLARATIONS) {
            if let Some((s, d)) = self.data_of(k) {
                if d > 0 && self.used + d <= budget {
                    let el = xml::variant(self.bytes(s, d)?);
                    self.used += d;
                    if let Some(el) = el {
                        self.declared = xml::declared_streams(&el);
                        self.trees.insert(k, Tree::Xml(el));
                    }
                }
            }
        }
        // what the source metadata will decode, within its budget (a lower bound of
        // what it spends: the bytes it inflates are not known yet)
        let mut est = self.used;
        let mut ranges = Vec::new();
        for k in self.candidates() {
            let Some((s, d)) = self.data_of(k) else {
                continue;
            };
            if self.chunks[k].0 == DECLARATIONS {
                continue;
            }
            if let Some(p) = PROFILE
                .iter()
                .position(|n| *n == self.chunks[k].0.as_slice())
            {
                // what it charges grows with what was charged before it, so this stays a lower bound
                if let Some(dec) = &self.profile[p] {
                    if d > 0 && est + d <= budget {
                        est += self.profile_charge(dec, d, est).0;
                    }
                }
                continue;
            }
            if d == 0 || est + d > budget {
                continue;
            }
            est += d;
            ranges.push((s, d));
        }
        self.got.retain(|_, _| false);
        self.phase = Phase::Meta;
        Ok(self.ask(ranges))
    }

    /// What decoding a profile chunk again for the source metadata would charge,
    /// and whether it would decode (spec/virtualize/nd2.md §5.1), from the profile's decode.
    fn profile_charge(&self, dec: &Decoded, d: u64, used: u64) -> (u64, bool) {
        let budget = schema::limit("source_budget") as u64;
        let limit = budget - used - d;
        let room = schema::limit("source_room") as u64;
        if dec.compressed {
            let x = dec.bytes.len() as u64;
            if x > limit {
                return (d + limit.min(lv::DEFLATE_RATIO * d), false);
            }
            (d + x, dec.spent <= room)
        } else {
            (d, dec.spent <= room)
        }
    }

    /// The chunks the source metadata would decode, in map order (spec/virtualize/nd2.md §5):
    /// not frames, families or streams, not empty, and not named like an earlier chunk.
    fn candidates(&self) -> Vec<usize> {
        let n_frames = self.facts.as_ref().map(|f| f.frames).unwrap_or(0.0);
        let mut streams: Vec<Vec<u8>> = STREAMS
            .iter()
            .map(|s| format!("CustomData|{s}!").into_bytes())
            .collect();
        for (sid, _, _) in &self.declared {
            if !STREAMS.contains(&sid.as_str()) {
                streams.push(format!("CustomData|{sid}!").into_bytes());
            }
        }
        let mut texts = std::collections::HashSet::new();
        let mut out = Vec::new();
        for (k, (name, _)) in self.chunks.iter().enumerate() {
            if frame_index(name).is_some_and(|f| (f as f64) < n_frames) {
                continue;
            }
            let Some((_, d)) = self.data_of(k) else {
                continue;
            };
            let is_frame = name.starts_with(b"ImageDataSeq|");
            let text = decode_text(name);
            if d == 0
                || is_frame
                || texts.contains(&text)
                || is_family(name)
                || streams.contains(name)
            {
            } else if lv_named(name)
                || name.starts_with(b"CustomDataVar|")
                || name.starts_with(b"CustomData|")
            {
                out.push(k);
            }
            if !is_frame {
                texts.insert(text);
            }
        }
        out
    }

    /// What the source metadata decodes of chunk `k` (a candidate, in map order),
    /// within its budget, and whether it decodes; its bytes are dropped once used.
    fn sm_decode(&mut self, k: usize) -> Result<(Option<Tree>, bool), String> {
        let budget = schema::limit("source_budget") as u64;
        let (s, d) = self.data_of(k).unwrap();
        if self.chunks[k].0 == DECLARATIONS {
            let t = self.trees.remove(&k);
            let ok = t.is_some();
            return Ok((t, ok));
        }
        if let Some(p) = PROFILE
            .iter()
            .position(|n| *n == self.chunks[k].0.as_slice())
        {
            let dec = self.profile[p].take();
            let mut ok = false;
            if let Some(dec) = &dec {
                if d > 0 && self.used + d <= budget {
                    let (charge, decodes) = self.profile_charge(dec, d, self.used);
                    self.used += charge;
                    ok = decodes;
                }
            }
            return Ok((dec.map(Tree::Lv), ok));
        }
        if self.used + d > budget {
            return Ok((None, false));
        }
        let data = self
            .got
            .remove(&s)
            .ok_or_else(|| format!("internal: bytes [{s}, {}) were not read", s + d))?;
        let name = &self.chunks[k].0;
        if name.starts_with(b"CustomDataVar|") {
            self.used += d;
            return Ok(match xml::variant(&data) {
                Some(el) => (Some(Tree::Xml(el)), true),
                None => (None, false),
            });
        }
        let mut room = schema::limit("source_room") as i64;
        let a = lv::decode(
            &data,
            Some(budget - self.used - d),
            !lv_named(name),
            Some(&mut room),
        );
        self.used += d + a.inflated.unwrap_or(0);
        Ok(match a.result {
            Ok(dec) => (Some(Tree::Lv(dec)), true),
            Err(_) => (None, false),
        })
    }

    /// The chunks in map order, each decoded (when the source metadata decodes
    /// it) and emitted at once, then the frames.
    fn meta_done(&mut self) -> Result<(), String> {
        let candidates: std::collections::HashSet<usize> = self.candidates().into_iter().collect();
        let size = self.size;
        let chunks_el = self.ir.struct_(0, "chunks", NO_INDEX, 0, None)?;
        let is_frame = |name: &[u8]| frame_index(name).is_some_and(|f| f < u64::MAX as u128);
        // the chunks' names (spec/virtualize/nd2.md §5.3): their texts, unique among `chunks`' children
        let texts: Vec<String> = self.chunks.iter().filter(|c| !is_frame(&c.0)).map(|c| decode_text(&c.0)).collect();
        let mut names = source_names(&texts, &[]).into_iter();
        let mut frames: Vec<(u64, usize)> = Vec::new();
        for k in 0..self.chunks.len() {
            if is_frame(&self.chunks[k].0) {
                let f = frame_index(&self.chunks[k].0).unwrap_or_default();
                frames.push((f as u64, k));
                continue;
            }
            let (name, nidx) = names.next().ok_or("internal: a chunk without a name")?;
            let (tree, sm) = if candidates.contains(&k) {
                self.sm_decode(k)?
            } else {
                // not decoded for the source metadata; the convention's own chunks keep their tree
                let p = PROFILE
                    .iter()
                    .position(|p| *p == self.chunks[k].0.as_slice());
                (
                    p.and_then(|p| self.profile[p].take())
                        .map(Tree::Lv)
                        .or_else(|| self.trees.remove(&k)),
                    false,
                )
            };
            let o = self.chunks[k].1;
            let Some((magic, n, d)) = self.heads[k] else {
                self.ir.struct_(chunks_el, &name, nidx, 0, None)?;
                continue;
            };
            let s = self.chunk(chunks_el, &name, nidx, o, magic, n, d)?;
            if magic != schema::limit("chunk_magic") as u32 {
                continue;
            }
            let start = o + 16 + n as u64;
            let len = d.min(size.saturating_sub(start.min(size)));
            if len == 0 {
                continue;
            }
            let spent = match &tree {
                Some(Tree::Lv(dec)) => dec.spent,
                _ => 0,
            };
            let el = match tree {
                Some(Tree::Lv(dec)) if dec.compressed => {
                    let form = format!(
                        "{{\"transform\":\"{}\",\"skip\":12,\"size\":{}}}",
                        crate::transform::Transform::Nd2LvZlib.name(),
                        dec.bytes.len()
                    );
                    let e = self.ir.derived(s, "lv", 0, (start, len), &form)?;
                    lv::emit(&mut self.ir, e, e, 0, &dec.bytes, &dec.records)?;
                    Some(e)
                }
                Some(Tree::Lv(dec)) => {
                    let e = self.ir.struct_(s, "lv", NO_INDEX, 0, Some((start, len)))?;
                    lv::emit(&mut self.ir, e, 0, start, &dec.bytes, &dec.records)?;
                    Some(e)
                }
                Some(Tree::Xml(el)) => {
                    let e = self.ir.derived(
                        s,
                        "xml",
                        0,
                        (start, len),
                        &format!("{{\"transform\":\"{}\"}}", crate::transform::Transform::XmlVariant.name()),
                    )?;
                    let (name, nidx) = source_names(&[&el.name], &[]).remove(0);
                    emit_xml(&mut self.ir, e, e, &el, &name, nidx)?;
                    Some(e)
                }
                None => {
                    self.ir.value(
                        s,
                        "data",
                        NO_INDEX,
                        &format!("bytes[{len}]"),
                        0,
                        (start, len),
                        None,
                    )?;
                    None
                }
            };
            if let (Some(e), true) = (el, sm) {
                self.decoded.push((k, e, spent));
            }
        }
        self.got.clear();
        frames.sort();
        self.emit_frames(&frames)?;
        Ok(())
    }

    /// A chunk's struct, header and name (within the file); its data is the caller's.
    #[allow(clippy::too_many_arguments)]
    fn chunk(
        &mut self,
        parent: u32,
        name: &str,
        nidx: u64,
        o: u64,
        magic: u32,
        n: u32,
        d: u64,
    ) -> Res<u32> {
        let size = self.size;
        let ok = magic == schema::limit("chunk_magic") as u32;
        let end = if ok {
            (o as u128 + 16 + n as u128 + d as u128).min(size as u128) as u64
        } else {
            o + 16
        };
        let s = self.ir.struct_(parent, name, nidx, 0, Some((o, end - o)))?;
        let head = [
            magic.to_le_bytes().as_slice(),
            n.to_le_bytes().as_slice(),
            d.to_le_bytes().as_slice(),
        ]
        .concat();
        self.ir
            .value(s, "header", NO_INDEX, HEADER, 0, (o, 16), Some(&head))?;
        if ok {
            let nl = (n as u64).min(size - o - 16);
            if nl > 0 {
                self.ir.value(
                    s,
                    "name",
                    NO_INDEX,
                    &format!("bytes[{nl}]"),
                    0,
                    (o + 16, nl),
                    None,
                )?;
            }
        }
        Ok(s)
    }

    fn emit_frames(&mut self, frames: &[(u64, usize)]) -> Res<()> {
        let size = self.size;
        let facts = self.facts.as_ref().unwrap();
        let (h, wb, r, compressed, n_frames) = (
            facts.height,
            facts.width_bytes,
            facts.row,
            facts.compressed,
            facts.frames,
        );
        let dtype = match facts.bpc {
            8 => "uint8",
            16 => "uint16",
            _ => "float32",
        };
        let (width, comp) = (facts.width, facts.comp);
        let pixels_form = if compressed {
            None
        } else if wb == r {
            Some(format!(
                "{{\"geometry\":{{\"shape\":[{h},{width},{comp}],\"dtype\":\"{dtype}\"}},\"codec\":[\"bytes\"],\"recipe\":[[\"src\",0,{}]]}}",
                h * r
            ))
        } else {
            Some(format!(
                "{{\"geometry\":{{\"shape\":[{h},{width},{comp}],\"dtype\":\"{dtype}\"}},\"codec\":[\"bytes\"],\"recipe\":[[\"rows\",0,{wb},{r},{h}]]}}"
            ))
        };
        let parent = self.ir.struct_(0, "frames", NO_INDEX, 0, None)?;
        let mut prev: Option<u32> = None;
        for &(f, k) in frames {
            let o = self.chunks[k].1;
            let Some((magic, n, d)) = self.heads[k] else {
                self.ir.struct_(parent, "", f, 0, None)?;
                prev = None;
                continue;
            };
            let s = self.chunk(parent, "", f, o, magic, n, d)?;
            let ok = magic == schema::limit("chunk_magic") as u32;
            let start = o + 16 + n as u64;
            if ok && (f as f64) < n_frames {
                // a placed frame: valid by the schema
                self.ir
                    .value(s, "timestamp", NO_INDEX, "<f8", 0, (start, 8), None)?;
                let px = start + 8;
                if compressed {
                    let form = format!(
                        "{{\"geometry\":{{\"shape\":[{h},{width},{comp}],\"dtype\":\"{dtype}\"}},\"codec\":[\"zlib\",\"bytes\"],\"recipe\":[[\"src\",0,{}]]}}",
                        d - 8
                    );
                    self.ir.data(s, "pixels", NO_INDEX, 0, (px, d - 8), &form)?;
                } else {
                    let len = (h * wb).min(size - px);
                    self.ir.data(
                        s,
                        "pixels",
                        NO_INDEX,
                        0,
                        (px, len),
                        pixels_form.as_deref().unwrap(),
                    )?;
                    let rest = d - 8 - h * wb;
                    let at = px + h * wb;
                    if rest > 0 && at < size {
                        let rest = rest.min(size - at);
                        self.ir.value(
                            s,
                            "trailing",
                            NO_INDEX,
                            &format!("bytes[{rest}]"),
                            0,
                            (at, rest),
                            None,
                        )?;
                    }
                }
            } else if ok {
                let len = d.min(size.saturating_sub(start.min(size)));
                if len > 0 {
                    self.ir.value(
                        s,
                        "data",
                        NO_INDEX,
                        &format!("bytes[{len}]"),
                        0,
                        (start, len),
                        None,
                    )?;
                }
            }
            match prev {
                Some(p) if self.ir.fold(p, s) => {}
                _ => prev = Some(s),
            }
        }
        Ok(())
    }

    /// The IR, finished (aliases and gaps), and the facts the projection reads.
    pub fn finish(mut self) -> Result<(Ir, serde_json::Value), String> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        check::finish(&mut self.ir)?;
        let mut facts = self.facts.take().unwrap().json;
        facts["decoded"] = serde_json::Value::Array(
            self.decoded
                .iter()
                .map(|&(k, e, spent)| {
                    serde_json::json!([
                        self.chunks[k]
                            .0
                            .iter()
                            .map(|&c| c as u64)
                            .collect::<Vec<_>>(),
                        e,
                        spent
                    ])
                })
                .collect(),
        );
        facts["requests"] = serde_json::json!(self.requests);
        facts["requested"] = serde_json::json!(self.requested);
        Ok((self.ir, facts))
    }
}

/// Emits XML element `el` under `parent` as `name` (with `nidx`); its children are
/// named by their tags, made unique among siblings by `source_names` (spec/virtualize/nd2.md §5.3).
fn emit_xml(ir: &mut Ir, parent: u32, space: u32, el: &xml::El, name: &str, nidx: u64) -> Res<()> {
    match &el.value {
        Some((rt, v, span)) => {
            let ty = match rt {
                Some(r) => format!("xml:{r}"),
                None => "xml".to_string(),
            };
            ir.value(
                parent,
                name,
                nidx,
                &ty,
                space,
                (span.0 as u64, (span.1 - span.0) as u64),
                Some(v.as_bytes()),
            )?;
        }
        None => {
            let s = ir.struct_(
                parent,
                name,
                nidx,
                space,
                Some((el.span.0 as u64, (el.span.1 - el.span.0) as u64)),
            )?;
            let tags: Vec<&str> = el.children.iter().map(|c| c.name.as_str()).collect();
            for (c, (cname, cidx)) in el.children.iter().zip(source_names(&tags, &[])) {
                emit_xml(ir, s, space, c, &cname, cidx)?;
            }
        }
    }
    Ok(())
}
