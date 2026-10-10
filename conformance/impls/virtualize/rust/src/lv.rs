// §4.2 lite variant decoder and typed accessors.

use crate::common::*;
use crate::rej;

#[derive(Clone, Debug)]
pub enum V {
    Bool(u8),
    I32(i32),
    U32(u32),
    I64(i64),
    U64(u64),
    F64(f64),
    Ptr,
    Str(String),
    Bytes(Vec<u8>),
    Obj(Vec<(String, V)>),
    List(Vec<V>),
}

const MAX_DEPTH: u32 = 100;

struct P<'a> {
    d: &'a [u8],
    pos: usize,
}

impl P<'_> {
    fn take(&mut self, n: u64) -> R<&[u8]> {
        let end = (self.pos as u64).checked_add(n);
        match end {
            Some(e) if e <= self.d.len() as u64 => {
                let s = &self.d[self.pos..e as usize];
                self.pos = e as usize;
                Ok(s)
            }
            _ => rej!("LV record runs past the end of the data"),
        }
    }
    fn u8(&mut self) -> R<u8> {
        Ok(self.take(1)?[0])
    }
    fn u32(&mut self) -> R<u32> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> R<u64> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn record(&mut self, depth: u32) -> R<(String, V)> {
        let start = self.pos;
        let typ = self.u8()?;
        let k = self.u8()?;
        let nb = self.take(2 * k as u64)?;
        let units: Vec<u16> = nb.chunks(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        let nl = units.iter().position(|&u| u == 0).unwrap_or(units.len());
        let name = String::from_utf16_lossy(&units[..nl]);
        let v = match typ {
            1 => V::Bool(self.u8()?),
            2 => V::I32(self.u32()? as i32),
            3 => V::U32(self.u32()?),
            4 => V::I64(self.u64()? as i64),
            5 => V::U64(self.u64()?),
            6 => V::F64(f64::from_bits(self.u64()?)),
            7 => {
                self.u64()?;
                V::Ptr
            }
            8 => {
                let mut u = Vec::new();
                loop {
                    let b = self.take(2)?;
                    let c = u16::from_le_bytes([b[0], b[1]]);
                    if c == 0 {
                        break;
                    }
                    u.push(c);
                }
                V::Str(String::from_utf16_lossy(&u))
            }
            9 => {
                let b = self.u64()?;
                V::Bytes(self.take(b)?.to_vec())
            }
            11 => {
                let c = self.u32()?;
                let l = self.u64()?;
                if c > 0 && depth + 1 > MAX_DEPTH {
                    rej!("LV levels nested more than {} deep", MAX_DEPTH);
                }
                let mut recs = Vec::new();
                for _ in 0..c {
                    recs.push(self.record(depth + 1)?);
                }
                if (self.pos - start) as u64 != l {
                    rej!("LV level does not end at its length");
                }
                self.take(8 * c as u64)?;
                level_value(recs, false)
            }
            t => rej!("LV record type {}", t),
        };
        Ok((name, v))
    }
}

fn level_value(recs: Vec<(String, V)>, top: bool) -> V {
    if !top && !recs.is_empty() && recs.iter().all(|(n, _)| n.is_empty()) {
        return V::List(recs.into_iter().map(|(_, v)| v).collect());
    }
    let mut members: Vec<(String, V)> = Vec::new();
    let mut idx: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for (n, v) in recs {
        match idx.get(&n) {
            Some(&i) => members[i].1 = v,
            None => {
                idx.insert(n.clone(), members.len());
                members.push((n, v));
            }
        }
    }
    V::Obj(members)
}

fn parse_plain(d: &[u8]) -> R<V> {
    let mut p = P { d, pos: 0 };
    let mut recs = Vec::new();
    while p.pos < d.len() {
        recs.push(p.record(0)?);
    }
    Ok(level_value(recs, true))
}

/// Decode a chunk's data (possibly a single compressed record).
pub fn parse_chunk(d: &[u8]) -> R<V> {
    if !d.is_empty() && d[0] == 76 {
        if d.len() < 12 {
            rej!("compressed LV record shorter than 12 bytes");
        }
        let inflated = inflate_exact(&d[12..])?;
        if !inflated.is_empty() && inflated[0] == 76 {
            rej!("nested compressed LV record");
        }
        return parse_plain(&inflated);
    }
    parse_plain(d)
}

/// Inflate a zlib stream that must end exactly at the end of `src`.
pub fn inflate_exact(src: &[u8]) -> R<Vec<u8>> {
    use flate2::{Decompress, FlushDecompress, Status};
    let mut z = Decompress::new(true);
    let mut out: Vec<u8> = Vec::with_capacity(src.len() * 4 + 64);
    loop {
        if out.len() == out.capacity() {
            out.reserve(out.capacity().max(1 << 16));
        }
        let before_in = z.total_in();
        let before_out = z.total_out();
        let st = match z.decompress_vec(&src[z.total_in() as usize..], &mut out, FlushDecompress::None) {
            Ok(s) => s,
            Err(e) => rej!("invalid zlib stream: {}", e),
        };
        match st {
            Status::StreamEnd => break,
            _ => {
                if z.total_in() == before_in && z.total_out() == before_out && out.len() < out.capacity() {
                    rej!("truncated zlib stream");
                }
            }
        }
    }
    if z.total_in() as usize != src.len() {
        rej!("zlib stream does not end at the end of the data");
    }
    Ok(out)
}

// ---- typed access (§4.2 Values / Paths) ----

pub fn member<'a>(o: &'a V, name: &str) -> R<Option<&'a V>> {
    match o {
        V::Obj(m) => Ok(m.iter().find(|(n, _)| n == name).map(|(_, v)| v)),
        _ => rej!("path step {:?} from a value that is not an object", name),
    }
}

pub fn path<'a>(o: &'a V, p: &str) -> R<Option<&'a V>> {
    let mut cur = o;
    for step in p.split('/') {
        match member(cur, step)? {
            Some(v) => cur = v,
            None => return Ok(None),
        }
    }
    Ok(Some(cur))
}

pub fn number(v: &V) -> R<f64> {
    let f = match v {
        V::I32(x) => *x as f64,
        V::U32(x) => *x as f64,
        V::I64(x) => *x as f64,
        V::U64(x) => *x as f64,
        V::F64(x) => *x,
        _ => rej!("value is not a number: {:?}", short(v)),
    };
    if !f.is_finite() {
        rej!("number is not finite");
    }
    Ok(f)
}

pub fn integer(v: &V) -> R<u64> {
    let f = number(v)?;
    if f.fract() != 0.0 || f < 0.0 || f > MAX53 as f64 {
        rej!("value {} is not an integer from 0 to 2^53-1", f);
    }
    Ok(f as u64)
}

pub fn color(v: &V) -> R<u32> {
    let f = number(v)?;
    if f.fract() != 0.0 || f < -2147483648.0 || f > 4294967295.0 {
        rej!("value {} is not a color", f);
    }
    Ok((f as i64).rem_euclid(1 << 32) as u32)
}

pub fn flag(v: &V) -> R<bool> {
    Ok(match v {
        V::Bool(x) => *x != 0,
        V::I32(x) => *x != 0,
        V::U32(x) => *x != 0,
        V::I64(x) => *x != 0,
        V::U64(x) => *x != 0,
        _ => rej!("value is not a flag: {:?}", short(v)),
    })
}

pub fn string(v: &V) -> R<String> {
    match v {
        V::Str(s) => Ok(s.clone()),
        _ => rej!("value is not a string"),
    }
}

pub fn object(v: &V) -> R<&V> {
    match v {
        V::Obj(_) => Ok(v),
        _ => rej!("value is not an object"),
    }
}

pub fn is_list(v: &V) -> bool {
    matches!(v, V::List(_) | V::Bytes(_))
}

/// The members of an object or a list (a byte array is a list of type-3 values).
pub fn members(v: &V) -> R<Vec<V>> {
    Ok(match v {
        V::Obj(m) => m.iter().map(|(_, v)| v.clone()).collect(),
        V::List(l) => l.clone(),
        V::Bytes(b) => b.iter().map(|&x| V::U32(x as u32)).collect(),
        _ => rej!("value is not an object or a list"),
    })
}

pub fn list_members(v: &V) -> R<Vec<V>> {
    if !is_list(v) {
        rej!("value is not a list");
    }
    members(v)
}

fn short(v: &V) -> String {
    let s = format!("{:?}", v);
    s.chars().take(60).collect()
}

// Optional typed members.
pub fn opt_integer(o: &V, p: &str) -> R<Option<u64>> {
    path(o, p)?.map(integer).transpose()
}
pub fn opt_number(o: &V, p: &str) -> R<Option<f64>> {
    path(o, p)?.map(number).transpose()
}
pub fn opt_flag(o: &V, p: &str) -> R<Option<bool>> {
    path(o, p)?.map(flag).transpose()
}
pub fn req<T>(v: Option<T>, what: &str) -> R<T> {
    match v {
        Some(v) => Ok(v),
        None => rej!("required member {} is missing", what),
    }
}
