use crate::json::J;
use std::collections::BTreeMap;

#[derive(Debug)]
pub enum Error {
    /// The specification rejects the input.
    Reject(String),
    /// Reading failed (network error etc.).
    Fail(String),
}

pub type Res<T> = Result<T, Error>;

#[macro_export]
macro_rules! rej {
    ($($a:tt)*) => { return Err($crate::common::Error::Reject(format!($($a)*))) };
}

pub const MAX_SAFE: u64 = (1u64 << 53) - 1;
pub const MAX_PAYLOAD: u64 = 65519;

pub fn varint_len(mut v: u64) -> u64 {
    let mut n = 1;
    while v >= 0x80 {
        v >>= 7;
        n += 1;
    }
    n
}

/// Length of a `Range` message for (source 0, offset o, length n).
pub fn range_msg_len(o: u64, n: u64) -> u64 {
    let mut l = 0;
    if o > 0 {
        l += 1 + varint_len(o);
    }
    if n > 0 {
        l += 1 + varint_len(n);
    }
    l
}

/// Size of one element of a `Concat` message holding range (o, n).
pub fn concat_elem_len(o: u64, n: u64) -> u64 {
    let r = range_msg_len(o, n);
    1 + varint_len(r) + r
}

pub fn payload_len(ranges: &[(u64, u64)]) -> u64 {
    if ranges.len() == 1 {
        range_msg_len(ranges[0].0, ranges[0].1)
    } else {
        ranges.iter().map(|&(o, n)| concat_elem_len(o, n)).sum()
    }
}

pub enum Entry {
    Ranges(Vec<(u64, u64)>),
    Json(J),
    Bytes(Vec<u8>),
}

pub struct Output {
    pub entries: BTreeMap<String, Entry>,
    pub file_size: u64,
}

impl Output {
    pub fn new(file_size: u64) -> Self {
        Output { entries: BTreeMap::new(), file_size }
    }
    pub fn json(&mut self, key: impl Into<String>, v: J) {
        self.entries.insert(key.into(), Entry::Json(v));
    }
    pub fn bytes(&mut self, key: impl Into<String>, v: Vec<u8>) {
        self.entries.insert(key.into(), Entry::Bytes(v));
    }
    /// Adds a reference entry, checking §1.2 "References stay in the file".
    pub fn ranges(&mut self, key: String, ranges: Vec<(u64, u64)>) -> Res<()> {
        for &(o, n) in &ranges {
            if o > MAX_SAFE || n > MAX_SAFE {
                rej!("{key}: range offset/length above 2^53-1");
            }
            if (o as u128) + (n as u128) > self.file_size as u128 {
                rej!("{key}: range ({o}, {n}) outside the file (size {})", self.file_size);
            }
        }
        let p = payload_len(&ranges);
        if p > MAX_PAYLOAD {
            rej!("{key}: reference payload {p} > 65519 bytes");
        }
        self.entries.insert(key, Entry::Ranges(ranges));
        Ok(())
    }

    pub fn to_json(&self, url: &str) -> String {
        let mut s = String::new();
        s.push_str("{\"sources\":[");
        crate::json::write_str(&mut s, url);
        s.push_str("],\"entries\":{");
        let mut first = true;
        for (k, v) in &self.entries {
            if !first {
                s.push(',');
            }
            first = false;
            s.push('\n');
            crate::json::write_str(&mut s, k);
            s.push(':');
            match v {
                Entry::Ranges(r) => {
                    s.push_str("{\"ranges\":[");
                    for (i, (o, n)) in r.iter().enumerate() {
                        if i > 0 {
                            s.push(',');
                        }
                        s.push_str(&format!("[0,{o},{n}]"));
                    }
                    s.push_str("]}");
                }
                Entry::Json(j) => {
                    s.push_str("{\"json\":");
                    j.write(&mut s);
                    s.push('}');
                }
                Entry::Bytes(b) => {
                    s.push_str("{\"base64\":\"");
                    s.push_str(&base64(b));
                    s.push_str("\"}");
                }
            }
        }
        s.push_str("\n}}\n");
        s
    }
}

pub fn base64(b: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut s = String::with_capacity(b.len().div_ceil(3) * 4);
    for c in b.chunks(3) {
        let v = (c[0] as u32) << 16
            | (*c.get(1).unwrap_or(&0) as u32) << 8
            | *c.get(2).unwrap_or(&0) as u32;
        s.push(A[(v >> 18) as usize & 63] as char);
        s.push(A[(v >> 12) as usize & 63] as char);
        s.push(if c.len() > 1 { A[(v >> 6) as usize & 63] as char } else { '=' });
        s.push(if c.len() > 2 { A[v as usize & 63] as char } else { '=' });
    }
    s
}

/// Zarr v3 array metadata (§2.1).
pub fn array_json(shape: &[u64], dtype: &str, chunk: &[u64], codecs: Vec<J>, dims: &[&str]) -> J {
    use crate::json::obj;
    obj(vec![
        ("zarr_format", J::Int(3)),
        ("node_type", J::s("array")),
        ("shape", J::Arr(shape.iter().map(|&v| J::UInt(v)).collect())),
        ("data_type", J::s(dtype)),
        (
            "chunk_grid",
            obj(vec![
                ("name", J::s("regular")),
                (
                    "configuration",
                    obj(vec![("chunk_shape", J::Arr(chunk.iter().map(|&v| J::UInt(v)).collect()))]),
                ),
            ]),
        ),
        (
            "chunk_key_encoding",
            obj(vec![
                ("name", J::s("default")),
                ("configuration", obj(vec![("separator", J::s("/"))])),
            ]),
        ),
        ("fill_value", J::Int(0)),
        ("codecs", J::Arr(codecs)),
        ("dimension_names", J::Arr(dims.iter().map(|d| J::s(d)).collect())),
        ("attributes", obj(vec![])),
    ])
}

pub fn transpose_codec(dims: &[&str]) -> J {
    use crate::json::obj;
    let mut order: Vec<J> = Vec::new();
    let mut ci = None;
    for (i, d) in dims.iter().enumerate() {
        if *d == "c" {
            ci = Some(i);
        } else {
            order.push(J::UInt(i as u64));
        }
    }
    order.push(J::UInt(ci.expect("transpose without c") as u64));
    obj(vec![
        ("name", J::s("transpose")),
        ("configuration", obj(vec![("order", J::Arr(order))])),
    ])
}

pub fn bytes_codec(item_size: u64, little: bool) -> J {
    use crate::json::obj;
    if item_size == 1 {
        obj(vec![("name", J::s("bytes"))])
    } else {
        obj(vec![
            ("name", J::s("bytes")),
            ("configuration", obj(vec![("endian", J::s(if little { "little" } else { "big" }))])),
        ])
    }
}

pub fn zlib_codec() -> J {
    use crate::json::obj;
    obj(vec![("name", J::s("zlib")), ("configuration", obj(vec![("level", J::Int(1))]))])
}

pub fn zstd_codec() -> J {
    use crate::json::obj;
    obj(vec![
        ("name", J::s("zstd")),
        ("configuration", obj(vec![("level", J::Int(0)), ("checksum", J::Bool(false))])),
    ])
}

pub struct Axis {
    pub name: &'static str,
    pub unit: Option<&'static str>,
}

/// OME-NGFF 0.5 image group (§2.2).
pub fn image_group(name: Option<&str>, axes: &[Axis], scales: &[Vec<f64>], omero: Option<J>) -> J {
    use crate::json::obj;
    let mut ms: Vec<(&str, J)> = Vec::new();
    if let Some(n) = name {
        ms.push(("name", J::s(n)));
    }
    let axes_j: Vec<J> = axes
        .iter()
        .map(|a| {
            let ty = match a.name {
                "t" => "time",
                "c" => "channel",
                _ => "space",
            };
            let mut m = vec![("name", J::s(a.name)), ("type", J::s(ty))];
            if let Some(u) = a.unit {
                m.push(("unit", J::s(u)));
            }
            obj(m)
        })
        .collect();
    ms.push(("axes", J::Arr(axes_j)));
    let ds: Vec<J> = scales
        .iter()
        .enumerate()
        .map(|(i, sc)| {
            obj(vec![
                ("path", J::Str(i.to_string())),
                (
                    "coordinateTransformations",
                    J::Arr(vec![obj(vec![
                        ("type", J::s("scale")),
                        ("scale", J::Arr(sc.iter().map(|&v| J::Num(v)).collect())),
                    ])]),
                ),
            ])
        })
        .collect();
    ms.push(("datasets", J::Arr(ds)));
    let mut m = vec![("version", J::s("0.5")), ("multiscales", J::Arr(vec![obj(ms)]))];
    if let Some(o) = omero {
        m.push(("omero", o));
    }
    obj(vec![
        ("zarr_format", J::Int(3)),
        ("node_type", J::s("group")),
        ("attributes", obj(vec![("ome", obj(m))])),
    ])
}

pub fn check_finite(v: f64, what: &str) -> Res<f64> {
    if v.is_finite() {
        Ok(v)
    } else {
        rej!("{what} is not finite")
    }
}
