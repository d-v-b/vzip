//! The lite variant (LV) encoding of ND2 metadata chunks (conventions/nd2/README.md
//! §2.2): a decoder to a tree of records with their positions, the object reading
//! the convention defines, and the records' emission as IR elements.

use crate::ir::*;

pub const MAX_DEPTH: u32 = 100;
pub const DEFLATE_RATIO: u64 = 1032;
const QUIET_NAN: u64 = 0x7FF8000000000000;

#[derive(Debug, Clone)]
pub enum LvVal {
    Scalar {
        typ: u8,
        raw: (usize, usize),
    },
    Str {
        units: Vec<u16>,
    },
    Bytes {
        at: usize,
        n: usize,
    },
    Level {
        count: u32,
        records: Vec<Rec>,
        table: (usize, usize),
    },
}

#[derive(Debug, Clone)]
pub struct Rec {
    pub start: usize,
    pub end: usize,
    pub typ: u8,
    pub k: u8,
    pub units: Vec<u16>, // the name: its units up to the first NUL
    pub val: LvVal,
}

#[derive(Debug)]
pub enum LvErr {
    Rejected(String),
    TooLarge,
    Lossy(String),
}

impl LvErr {
    pub fn message(&self) -> String {
        match self {
            LvErr::Rejected(m) | LvErr::Lossy(m) => m.clone(),
            LvErr::TooLarge => {
                "LV data with more records and byte-array bytes than its budget".into()
            }
        }
    }
}

type R<T> = Result<T, LvErr>;

fn rej<T>(m: &str) -> R<T> {
    Err(LvErr::Rejected(m.to_string()))
}

pub fn well_formed(units: &[u16]) -> bool {
    let mut i = 0;
    while i < units.len() {
        let u = units[i];
        if (0xD800..=0xDBFF).contains(&u) {
            if i + 1 < units.len() && (0xDC00..=0xDFFF).contains(&units[i + 1]) {
                i += 2;
                continue;
            }
            return false;
        }
        if (0xDC00..=0xDFFF).contains(&u) {
            return false;
        }
        i += 1;
    }
    true
}

pub fn units_bytes(units: &[u16]) -> Vec<u8> {
    units.iter().flat_map(|u| u.to_le_bytes()).collect()
}

pub fn base64(b: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in b.chunks(3) {
        let n = (c[0] as u32) << 16
            | (*c.get(1).unwrap_or(&0) as u32) << 8
            | *c.get(2).unwrap_or(&0) as u32;
        out.push(A[(n >> 18) as usize & 63] as char);
        out.push(A[(n >> 12) as usize & 63] as char);
        out.push(if c.len() > 1 {
            A[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if c.len() > 2 {
            A[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// A name's exact form (conventions/nd2/README.md §5.1): its text, or U+0000 and
/// the base64 of its units when it is not well-formed.
pub fn exact_name(units: &[u16]) -> String {
    if well_formed(units) {
        String::from_utf16_lossy(units)
    } else {
        format!("\0{}", base64(&units_bytes(units)))
    }
}

pub fn lossy(units: &[u16]) -> String {
    String::from_utf16_lossy(units)
}

struct P<'a> {
    d: &'a [u8],
    exact: bool,
    room: Option<&'a mut i64>,
    records: u64,
}

impl P<'_> {
    fn spend(&mut self, n: u64) -> R<()> {
        self.records += n;
        if let Some(r) = self.room.as_deref_mut() {
            *r -= n as i64;
            if *r < 0 {
                return Err(LvErr::TooLarge);
            }
        }
        Ok(())
    }

    fn records(
        &mut self,
        mut pos: usize,
        end: usize,
        count: Option<u32>,
        depth: u32,
    ) -> R<(Vec<Rec>, usize)> {
        if depth > MAX_DEPTH {
            return rej("LV levels nested more than 100 deep");
        }
        let d = self.d;
        let mut out = Vec::new();
        while pos < end && count.is_none_or(|c| (out.len() as u64) < c as u64) {
            let start = pos;
            self.spend(1)?;
            if pos + 2 > end {
                return rej("truncated LV record");
            }
            let (typ, k) = (d[pos], d[pos + 1]);
            pos += 2;
            if typ == 76 {
                return rej("compressed LV record inside a structure");
            }
            if pos + 2 * k as usize > end {
                return rej("truncated LV record name");
            }
            let mut units = Vec::new();
            for c in d[pos..pos + 2 * k as usize].chunks(2) {
                let u = u16::from_le_bytes([c[0], c[1]]);
                if u == 0 {
                    break;
                }
                units.push(u);
            }
            if self.exact
                && (if units.is_empty() {
                    k != 0
                } else {
                    units.len() != k as usize - 1
                })
            {
                return Err(LvErr::Lossy(
                    "an LV name other than its units and one NUL, or k = 0 when empty".into(),
                ));
            }
            pos += 2 * k as usize;
            let take = |pos: &mut usize, n: u64| -> R<(usize, usize)> {
                if (*pos as u64).checked_add(n).is_none_or(|e| e > end as u64) {
                    return rej("truncated LV value");
                }
                let s = *pos;
                *pos += n as usize;
                Ok((s, *pos))
            };
            let val = match typ {
                1 => {
                    let r = take(&mut pos, 1)?;
                    if self.exact && d[r.0] > 1 {
                        return Err(LvErr::Lossy("an LV bool other than 0 or 1".into()));
                    }
                    LvVal::Scalar { typ, raw: r }
                }
                2..=7 => {
                    let r = take(&mut pos, if typ <= 3 { 4 } else { 8 })?;
                    if self.exact && typ == 6 {
                        let bits = u64::from_le_bytes(d[r.0..r.1].try_into().unwrap());
                        let v = f64::from_bits(bits);
                        if v.is_nan() && bits != QUIET_NAN {
                            return Err(LvErr::Lossy(
                                "an LV NaN other than 0x7FF8000000000000".into(),
                            ));
                        }
                    }
                    LvVal::Scalar { typ, raw: r }
                }
                8 => {
                    let mut units = Vec::new();
                    loop {
                        let r = take(&mut pos, 2)?;
                        let u = u16::from_le_bytes([d[r.0], d[r.0 + 1]]);
                        if u == 0 {
                            break;
                        }
                        units.push(u);
                    }
                    LvVal::Str { units }
                }
                9 => {
                    let r = take(&mut pos, 8)?;
                    let n = u64::from_le_bytes(d[r.0..r.1].try_into().unwrap());
                    self.spend(n)?;
                    let r = take(&mut pos, n)?;
                    LvVal::Bytes {
                        at: r.0,
                        n: r.1 - r.0,
                    }
                }
                11 => {
                    let r = take(&mut pos, 12)?;
                    let c = u32::from_le_bytes(d[r.0..r.0 + 4].try_into().unwrap());
                    let length = u64::from_le_bytes(d[r.0 + 4..r.1].try_into().unwrap());
                    let level_end = (start as u64).checked_add(length);
                    let Some(level_end) = level_end.filter(|&e| e <= end as u64 && e >= pos as u64)
                    else {
                        return rej("LV level length outside the data");
                    };
                    let level_end = level_end as usize;
                    let (members, after) = self.records(pos, level_end, Some(c), depth + 1)?;
                    if members.len() as u64 != c as u64 || after != level_end {
                        return rej("LV level records do not end at its length");
                    }
                    pos = level_end;
                    let t = take(&mut pos, 8 * c as u64)?;
                    if self.exact && d[t.0..t.1].iter().any(|&b| b != 0) {
                        let mut offsets: Vec<u64> = d[t.0..t.1]
                            .chunks(8)
                            .map(|x| u64::from_le_bytes(x.try_into().unwrap()))
                            .collect();
                        offsets.sort();
                        let mut want: Vec<u64> =
                            members.iter().map(|m| (m.start - start) as u64).collect();
                        want.sort();
                        if offsets != want {
                            return Err(LvErr::Lossy("an LV level whose skipped bytes are neither zero nor its offset table".into()));
                        }
                    }
                    LvVal::Level {
                        count: c,
                        records: members,
                        table: t,
                    }
                }
                _ => return rej(&format!("unknown LV record type {typ}")),
            };
            out.push(Rec {
                start,
                end: pos,
                typ,
                k,
                units,
                val,
            });
        }
        Ok((out, pos))
    }
}

/// A chunk's data, decoded: the records and the bytes they index (the data, or
/// what it inflates to).
pub struct Decoded {
    pub records: Vec<Rec>,
    pub bytes: Vec<u8>,
    pub compressed: bool,
    /// records and byte-array bytes spent
    pub spent: u64,
}

/// The outcome of an attempt, for the convention's budget: the bytes inflated
/// (or, when the stream does not inflate within the limit, the most it could).
pub struct Attempt {
    pub inflated: Option<u64>,
    pub result: Result<Decoded, LvErr>,
}

pub fn inflate(data: &[u8], limit: Option<u64>) -> Result<Vec<u8>, String> {
    use miniz_oxide::inflate::stream::{InflateState, inflate as step};
    use miniz_oxide::{DataFormat, MZFlush, MZStatus};
    let mut st = InflateState::new_boxed(DataFormat::Zlib);
    let cap = limit.map(|l| l + 1).unwrap_or(u64::MAX);
    let mut out: Vec<u8> = Vec::new();
    let mut buf = vec![0u8; 1 << 16];
    let mut input = &data[12..];
    loop {
        let r = step(&mut st, input, &mut buf, MZFlush::None);
        input = &input[r.bytes_consumed..];
        out.extend_from_slice(&buf[..r.bytes_written]);
        if out.len() as u64 >= cap {
            return Err(format!(
                "compressed LV data inflates to more than {} bytes",
                limit.unwrap()
            ));
        }
        match r.status {
            Ok(MZStatus::StreamEnd) => {
                if !input.is_empty() {
                    return Err("compressed LV data does not end with its zlib stream".into());
                }
                return Ok(out);
            }
            Ok(_) => {
                if r.bytes_consumed == 0 && r.bytes_written == 0 {
                    return Err("compressed LV data does not end with its zlib stream".into());
                }
            }
            Err(e) => {
                if matches!(e, miniz_oxide::MZError::Buf) && input.is_empty() {
                    return Err("compressed LV data does not end with its zlib stream".into());
                }
                return Err(format!("invalid zlib stream in compressed LV data: {e:?}"));
            }
        }
    }
}

/// decode_lv of the convention: a compressed record inflates within `limit`; with
/// `exact`, data its JSON would not keep is Lossy; `room` is shared and spent.
pub fn decode(data: &[u8], limit: Option<u64>, exact: bool, room: Option<&mut i64>) -> Attempt {
    let mut inflated = None;
    let (bytes, compressed) = if data.first() == Some(&76) {
        if data.len() < 12 {
            return Attempt {
                inflated,
                result: rej("truncated compressed LV record"),
            };
        }
        match inflate(data, limit) {
            Ok(inner) => {
                inflated = Some(inner.len() as u64);
                if inner.first() == Some(&0x4c) {
                    return Attempt {
                        inflated,
                        result: rej("compressed LV data inside compressed LV data"),
                    };
                }
                (inner, true)
            }
            Err(m) => {
                let most = DEFLATE_RATIO * data.len() as u64;
                inflated = Some(limit.map(|l| l.min(most)).unwrap_or(most));
                return Attempt {
                    inflated,
                    result: Err(LvErr::Rejected(m)),
                };
            }
        }
    } else {
        (data.to_vec(), false)
    };
    let mut p = P {
        d: &bytes,
        exact,
        room,
        records: 0,
    };
    let result = p
        .records(0, bytes.len(), None, 0)
        .map(|(records, _)| (records, p.records));
    Attempt {
        inflated,
        result: result.map(|(records, spent)| Decoded {
            records,
            bytes,
            compressed,
            spent,
        }),
    }
}

// ---- reading (conventions/nd2/README.md §2.2): objects, lists and scalars

#[derive(Clone, Copy)]
pub struct Node<'a> {
    pub rec: Option<&'a Rec>, // None: a chunk's top level
    pub top: &'a [Rec],
    pub bytes: &'a [u8],
}

pub enum Read<'a> {
    Object(Vec<(String, Node<'a>)>), // members by lossy name: first position, last value
    List(Vec<Node<'a>>),
    Bytes(&'a [u8]),
    Scalar(u8, &'a [u8]),
    Str(String),
}

impl<'a> Node<'a> {
    pub fn top(records: &'a [Rec], bytes: &'a [u8]) -> Self {
        Node {
            rec: None,
            top: records,
            bytes,
        }
    }

    fn of(&self, r: &'a Rec) -> Node<'a> {
        Node {
            rec: Some(r),
            top: self.top,
            bytes: self.bytes,
        }
    }

    pub fn read(&self) -> Read<'a> {
        let recs: &'a [Rec] = match self.rec {
            None => self.top,
            Some(r) => match &r.val {
                LvVal::Level { records, .. } => {
                    if !records.is_empty() && records.iter().all(|m| m.units.is_empty()) {
                        return Read::List(records.iter().map(|m| self.of(m)).collect());
                    }
                    records
                }
                LvVal::Scalar { typ, raw } => return Read::Scalar(*typ, &self.bytes[raw.0..raw.1]),
                LvVal::Str { units } => return Read::Str(lossy(units)),
                LvVal::Bytes { at, n } => return Read::Bytes(&self.bytes[*at..at + n]),
            },
        };
        let mut members: Vec<(String, Node<'a>)> = Vec::new();
        let mut index: std::collections::HashMap<String, usize> = Default::default();
        for m in recs {
            let name = lossy(&m.units);
            match index.get(&name) {
                Some(&k) => members[k].1 = self.of(m),
                None => {
                    index.insert(name.clone(), members.len());
                    members.push((name, self.of(m)));
                }
            }
        }
        Read::Object(members)
    }
}

/// A scalar's value as binary64 (types 2-6), or None.
pub fn scalar_number(typ: u8, raw: &[u8]) -> Option<f64> {
    Some(match typ {
        2 => i32::from_le_bytes(raw.try_into().ok()?) as f64,
        3 => u32::from_le_bytes(raw.try_into().ok()?) as f64,
        4 => i64::from_le_bytes(raw.try_into().ok()?) as f64,
        5 => u64::from_le_bytes(raw.try_into().ok()?) as f64,
        6 => f64::from_le_bytes(raw.try_into().ok()?),
        _ => return None,
    })
}

/// A scalar's value as a flag (types 1-5), or None.
pub fn scalar_flag(typ: u8, raw: &[u8]) -> Option<bool> {
    match typ {
        1..=5 => Some(raw.iter().any(|&b| b != 0)),
        _ => None,
    }
}

// ---- emission

fn record_type(r: &Rec, bytes: &[u8]) -> String {
    let head = format!("lv:u1,k:u1,name:bytes[{}]", 2 * r.k as usize);
    match &r.val {
        LvVal::Scalar { typ, .. } => {
            let v = match typ {
                1 => "u1",
                2 => "<i4",
                3 => "<u4",
                4 => "<i8",
                5 | 7 => "<u8",
                _ => "<f8",
            };
            format!("{{{head},v:{v}}}")
        }
        LvVal::Str { units } => format!("{{{head},v:utf16[{}]}}", 2 * units.len() + 2),
        LvVal::Bytes { n, .. } => format!("{{{head},n:<u8,v:bytes[{n}]}}"),
        LvVal::Level { .. } => {
            let _ = bytes;
            format!("{{{head},count:<u4,length:<u8}}")
        }
    }
}

/// Emits `records` (whose positions index `bytes`, which start at `base` in `space`)
/// under `parent`: a scalar as one value element (the whole record), a level as a
/// struct with its `header`, its records and its skipped `table`.
pub fn emit(
    ir: &mut Ir,
    parent: u32,
    space: u32,
    base: u64,
    bytes: &[u8],
    records: &[Rec],
) -> Res<()> {
    for r in records {
        let name = exact_name(&r.units);
        let ext = (base + r.start as u64, (r.end - r.start) as u64);
        match &r.val {
            LvVal::Level {
                count,
                records: members,
                table,
            } => {
                let s = ir.struct_(parent, &name, NO_INDEX, space, Some(ext))?;
                let hn = 2 + 2 * r.k as usize + 12;
                ir.value(
                    s,
                    "header",
                    NO_INDEX,
                    &record_type(r, bytes),
                    space,
                    (ext.0, hn as u64),
                    Some(&bytes[r.start..r.start + hn]),
                )?;
                emit(ir, s, space, base, bytes, members)?;
                if *count > 0 {
                    ir.value(
                        s,
                        "table",
                        NO_INDEX,
                        &format!("bytes[{}]", table.1 - table.0),
                        space,
                        (base + table.0 as u64, (table.1 - table.0) as u64),
                        None,
                    )?;
                }
            }
            _ => {
                ir.value(
                    parent,
                    &name,
                    NO_INDEX,
                    &record_type(r, bytes),
                    space,
                    ext,
                    Some(&bytes[r.start..r.end]),
                )?;
            }
        }
    }
    Ok(())
}
