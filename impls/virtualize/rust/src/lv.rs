//! Lite variant (§4.2).
use crate::common::*;
use crate::rej;
use std::collections::HashMap;

#[derive(Debug)]
pub enum Lv {
    Bool(u8),
    I32(i32),
    U32(u32),
    I64(i64),
    U64(u64),
    F64(f64),
    Ptr,
    Str(String),
    Bytes(Vec<u8>),
    Obj(Vec<(String, Lv)>),
    List(Vec<Lv>),
}

/// A borrowed view of a list member (byte arrays yield type-3 values).
#[derive(Clone, Copy)]
pub enum Item<'a> {
    V(&'a Lv),
    Byte(u8),
}

struct P<'a> {
    d: &'a [u8],
    pos: usize,
}

impl<'a> P<'a> {
    fn take(&mut self, n: u64) -> Res<&'a [u8]> {
        if n > (self.d.len() - self.pos) as u64 {
            rej!("LV: record runs past the end of the data");
        }
        let s = &self.d[self.pos..self.pos + n as usize];
        self.pos += n as usize;
        Ok(s)
    }
    fn u8(&mut self) -> Res<u8> {
        Ok(self.take(1)?[0])
    }
    fn u32(&mut self) -> Res<u32> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Res<u64> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
}

fn utf16(units: &[u16]) -> String {
    String::from_utf16_lossy(units)
}

fn record(p: &mut P) -> Res<(String, Lv)> {
    let start = p.pos;
    let typ = p.u8()?;
    let k = p.u8()? as u64;
    let nb = p.take(2 * k)?;
    let units: Vec<u16> = nb.chunks(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
    let nlen = units.iter().position(|&u| u == 0).unwrap_or(units.len());
    let name = utf16(&units[..nlen]);
    let v = match typ {
        1 => Lv::Bool(p.u8()?),
        2 => Lv::I32(p.u32()? as i32),
        3 => Lv::U32(p.u32()?),
        4 => Lv::I64(p.u64()? as i64),
        5 => Lv::U64(p.u64()?),
        6 => Lv::F64(f64::from_bits(p.u64()?)),
        7 => {
            p.u64()?;
            Lv::Ptr
        }
        8 => {
            let mut u = Vec::new();
            loop {
                let b = p.take(2)?;
                let c = u16::from_le_bytes([b[0], b[1]]);
                if c == 0 {
                    break;
                }
                u.push(c);
            }
            Lv::Str(utf16(&u))
        }
        9 => {
            let b = p.u64()?;
            Lv::Bytes(p.take(b)?.to_vec())
        }
        11 => {
            let c = p.u32()? as u64;
            let l = p.u64()?;
            let mut recs = Vec::new();
            for _ in 0..c {
                recs.push(record(p)?);
            }
            if (p.pos - start) as u64 != l {
                rej!("LV: level '{name}' of {c} records ends at {} bytes, expected {l}", p.pos - start);
            }
            p.take(8 * c)?;
            level_value(recs, false)
        }
        t => rej!("LV: record '{name}' has unknown type {t}"),
    };
    Ok((name, v))
}

fn level_value(recs: Vec<(String, Lv)>, top: bool) -> Lv {
    if !top && !recs.is_empty() && recs.iter().all(|(n, _)| n.is_empty()) {
        return Lv::List(recs.into_iter().map(|(_, v)| v).collect());
    }
    let mut members: Vec<(String, Lv)> = Vec::new();
    let mut idx: HashMap<String, usize> = HashMap::new();
    for (n, v) in recs {
        if let Some(&i) = idx.get(&n) {
            members[i].1 = v;
        } else {
            idx.insert(n.clone(), members.len());
            members.push((n, v));
        }
    }
    Lv::Obj(members)
}

fn parse_plain(d: &[u8]) -> Res<Lv> {
    let mut p = P { d, pos: 0 };
    let mut recs = Vec::new();
    while p.pos < d.len() {
        recs.push(record(&mut p)?);
    }
    Ok(level_value(recs, true))
}

/// Parses a metadata chunk's data (possibly a single compressed record).
pub fn parse_chunk(d: &[u8]) -> Res<Lv> {
    if !d.is_empty() && d[0] == 76 {
        if d.len() < 12 {
            rej!("LV: compressed record shorter than 12 bytes");
        }
        let inflated = inflate_exact(&d[12..])?;
        return parse_plain(&inflated);
    }
    parse_plain(d)
}

/// Inflates a zlib stream that must end exactly at the end of `input`.
pub fn inflate_exact(input: &[u8]) -> Res<Vec<u8>> {
    use flate2::{Decompress, FlushDecompress, Status};
    let mut z = Decompress::new(true);
    let mut out: Vec<u8> = Vec::with_capacity(input.len() * 4 + 1024);
    loop {
        if out.len() == out.capacity() {
            out.reserve(out.capacity().max(1024));
        }
        let before_in = z.total_in();
        let before_out = z.total_out();
        let consumed = z.total_in() as usize;
        let st = match z.decompress_vec(&input[consumed..], &mut out, FlushDecompress::Finish) {
            Ok(s) => s,
            Err(e) => rej!("zlib: {e}"),
        };
        match st {
            Status::StreamEnd => break,
            _ => {
                if z.total_in() == before_in && z.total_out() == before_out && out.len() < out.capacity() {
                    rej!("zlib: truncated stream");
                }
            }
        }
    }
    if z.total_in() as usize != input.len() {
        rej!("zlib: stream ends {} bytes before the end of the data", input.len() - z.total_in() as usize);
    }
    Ok(out)
}

// ---------------------------------------------------------------- access

pub fn member<'a>(obj: &'a Lv, name: &str) -> Res<Option<&'a Lv>> {
    match obj {
        Lv::Obj(m) => Ok(m.iter().find(|(k, _)| k == name).map(|(_, v)| v)),
        _ => rej!("LV: path step '{name}' from a value that is not an object"),
    }
}

pub fn members_of(v: &Lv) -> Vec<Item<'_>> {
    match v {
        Lv::Obj(m) => m.iter().map(|(_, v)| Item::V(v)).collect(),
        Lv::List(l) => l.iter().map(Item::V).collect(),
        Lv::Bytes(b) => b.iter().map(|&x| Item::Byte(x)).collect(),
        _ => Vec::new(),
    }
}

pub fn number_item(v: Item, what: &str) -> Res<f64> {
    let f = match v {
        Item::Byte(b) => b as f64,
        Item::V(Lv::I32(x)) => *x as f64,
        Item::V(Lv::U32(x)) => *x as f64,
        Item::V(Lv::I64(x)) => *x as f64,
        Item::V(Lv::U64(x)) => *x as f64,
        Item::V(Lv::F64(x)) => *x,
        _ => rej!("LV: {what} is not a number"),
    };
    if !f.is_finite() {
        rej!("LV: {what} is not finite");
    }
    Ok(f)
}

pub fn number(v: &Lv, what: &str) -> Res<f64> {
    number_item(Item::V(v), what)
}

pub fn integer(v: &Lv, what: &str) -> Res<u64> {
    let f = number(v, what)?;
    if f.fract() != 0.0 || f < 0.0 || f > MAX_SAFE as f64 {
        rej!("LV: {what} = {f} is not an integer from 0 to 2^53-1");
    }
    Ok(f as u64)
}

pub fn color(v: &Lv, what: &str) -> Res<u32> {
    let f = number(v, what)?;
    if f.fract() != 0.0 || f < -(2f64.powi(31)) || f > 2f64.powi(32) - 1.0 {
        rej!("LV: {what} = {f} is not a color");
    }
    Ok((f as i64).rem_euclid(1i64 << 32) as u32)
}

pub fn flag_item(v: Item, what: &str) -> Res<bool> {
    Ok(match v {
        Item::Byte(b) => b != 0,
        Item::V(Lv::Bool(b)) => *b != 0,
        Item::V(Lv::I32(x)) => *x != 0,
        Item::V(Lv::U32(x)) => *x != 0,
        Item::V(Lv::I64(x)) => *x != 0,
        Item::V(Lv::U64(x)) => *x != 0,
        _ => rej!("LV: {what} is not a flag"),
    })
}

pub fn string<'a>(v: &'a Lv, what: &str) -> Res<&'a str> {
    match v {
        Lv::Str(s) => Ok(s),
        _ => rej!("LV: {what} is not a string"),
    }
}

pub fn object<'a>(v: &'a Lv, what: &str) -> Res<&'a Lv> {
    match v {
        Lv::Obj(_) => Ok(v),
        _ => rej!("LV: {what} is not an object"),
    }
}

pub fn list<'a>(v: &'a Lv, what: &str) -> Res<&'a Lv> {
    match v {
        Lv::List(_) | Lv::Bytes(_) => Ok(v),
        _ => rej!("LV: {what} is not a list"),
    }
}

pub fn object_or_list<'a>(v: &'a Lv, what: &str) -> Res<&'a Lv> {
    match v {
        Lv::Obj(_) | Lv::List(_) | Lv::Bytes(_) => Ok(v),
        _ => rej!("LV: {what} is not an object or a list"),
    }
}
