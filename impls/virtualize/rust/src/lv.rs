//! Lite variant (LV) decoder (VIRTUALIZE.md §4.2).

use crate::json::Value;
use crate::{Error, reject};
use std::io::Read;

struct Cur<'a> {
    d: &'a [u8],
    p: usize,
}

impl<'a> Cur<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], Error> {
        if self.d.len() - self.p < n {
            return reject("LV structure truncated");
        }
        let s = &self.d[self.p..self.p + n];
        self.p += n;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8, Error> {
        Ok(self.take(1)?[0])
    }
    fn u32(&mut self) -> Result<u32, Error> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Result<u64, Error> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
}

fn utf16(units: &[u16]) -> String {
    String::from_utf16_lossy(units)
}

/// Decode one top-level LV structure (a chunk's data).
pub fn decode(data: &[u8]) -> Result<Value, Error> {
    decode_depth(data, 0)
}

fn decode_depth(data: &[u8], nest: u32) -> Result<Value, Error> {
    if nest > 16 {
        return reject("LV compressed structures nested too deeply");
    }
    let mut c = Cur { d: data, p: 0 };
    let mut recs = Vec::new();
    while c.p < data.len() {
        // type 76: the rest of the data is a zlib stream of an LV structure
        // which replaces this one
        if data[c.p] == 76 {
            c.take(2)?; // type, name-length byte
            c.take(10)?;
            if std::env::var_os("VIRTUALIZE_DEBUG").is_some() {
                eprintln!("LV: compressed structure at offset {}", c.p - 12);
            }
            let mut z = flate2::read::ZlibDecoder::new(&data[c.p..]);
            let mut out = Vec::new();
            z.read_to_end(&mut out)
                .map_err(|e| Error::Reject(format!("LV compressed structure: zlib error: {e}")))?;
            return decode_depth(&out, nest + 1);
        }
        recs.push(record(&mut c, 0)?);
    }
    Ok(level(recs))
}

/// Build a level from its records: an object by name (a repeated name keeps
/// its last value, at the position of its first occurrence), or a list when
/// every record has an empty name.
fn level(recs: Vec<(String, Value)>) -> Value {
    if !recs.is_empty() && recs.iter().all(|(n, _)| n.is_empty()) {
        return Value::List(recs.into_iter().map(|(_, v)| v).collect());
    }
    let mut m: Vec<(String, Value)> = Vec::with_capacity(recs.len());
    for (n, v) in recs {
        if let Some(slot) = m.iter_mut().find(|(k, _)| *k == n) {
            slot.1 = v;
        } else {
            m.push((n, v));
        }
    }
    Value::Obj(m)
}

fn record(c: &mut Cur, depth: u32) -> Result<(String, Value), Error> {
    if depth > 64 {
        return reject("LV levels nested too deeply");
    }
    let start = c.p;
    let typ = c.u8()?;
    if typ == 76 {
        return reject("LV compressed record inside a level");
    }
    let k = c.u8()? as usize;
    let nb = c.take(2 * k)?;
    let mut units: Vec<u16> = nb.chunks(2).map(|b| u16::from_le_bytes([b[0], b[1]])).collect();
    if units.last() == Some(&0) {
        units.pop();
    }
    // a name is the units before the terminating NUL
    if let Some(z) = units.iter().position(|&u| u == 0) {
        units.truncate(z);
    }
    let name = utf16(&units);
    let v = match typ {
        1 => Value::Bool(c.u8()? != 0),
        2 => Value::Int(c.u32()? as i32 as i128),
        3 => Value::Int(c.u32()? as i128),
        4 => Value::Int(c.u64()? as i64 as i128),
        5 => Value::Int(c.u64()? as i128),
        6 => Value::Float(f64::from_le_bytes(c.take(8)?.try_into().unwrap())),
        7 => {
            c.u64()?;
            Value::Int(0)
        }
        8 => {
            let mut units = Vec::new();
            loop {
                let b = c.take(2)?;
                let u = u16::from_le_bytes([b[0], b[1]]);
                if u == 0 {
                    break;
                }
                units.push(u);
            }
            Value::Str(utf16(&units))
        }
        9 => {
            let b = c.u64()?;
            let bytes = c.take(usize::try_from(b).map_err(|_| Error::Reject("LV byte array too large".into()))?)?;
            Value::List(bytes.iter().map(|&x| Value::Int(x as i128)).collect())
        }
        11 => {
            let count = c.u32()? as usize;
            let len = c.u64()?;
            let mut recs = Vec::with_capacity(count.min(4096));
            for _ in 0..count {
                recs.push(record(c, depth + 1)?);
            }
            let end = start as u64 + len;
            if c.p as u64 != end {
                return reject(format!(
                    "LV level {name:?}: length {len} does not match its records (they end at {}, expected {end})",
                    c.p - start
                ));
            }
            c.take(8 * count)?;
            level(recs)
        }
        t => return reject(format!("unknown LV record type {t}")),
    };
    Ok((name, v))
}
