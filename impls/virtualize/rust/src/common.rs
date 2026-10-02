// Shared types: errors, JSON values, output description.

use std::fmt::Write as _;

pub const MAX53: u64 = (1u64 << 53) - 1;

#[derive(Debug)]
pub enum E {
    /// The specification rejects the input (exit status 3).
    Reject(String),
    /// Reading failed (network error etc.).
    Fail(String),
}

pub type R<T> = Result<T, E>;

#[macro_export]
macro_rules! rej {
    ($($a:tt)*) => { return Err($crate::common::E::Reject(format!($($a)*))) };
}


#[derive(Clone, Debug)]
pub enum Json {
    Bool(bool),
    Int(i128),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn obj(members: Vec<(&str, Json)>) -> Json {
        Json::Obj(members.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }
    pub fn s(v: &str) -> Json {
        Json::Str(v.to_string())
    }
    pub fn i(v: u64) -> Json {
        Json::Int(v as i128)
    }
    pub fn write(&self, out: &mut String) {
        match self {
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(i) => {
                let _ = write!(out, "{}", i);
            }
            Json::Num(f) => {
                // Debug formatting is shortest round-trip and always valid JSON
                // for finite values (e.g. "1.0", "0.1", "1e300", "1e-7").
                assert!(f.is_finite());
                let _ = write!(out, "{:?}", f);
            }
            Json::Str(s) => write_str(s, out),
            Json::Arr(a) => {
                out.push('[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    v.write(out);
                }
                out.push(']');
            }
            Json::Obj(o) => {
                out.push('{');
                for (i, (k, v)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_str(k, out);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

fn write_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

pub enum Entry {
    Ranges(Vec<(u64, u64)>), // (offset, length), source 0
    Json(Json),
    Bytes(Vec<u8>),
}

pub struct Output {
    pub entries: Vec<(String, Entry)>,
}

fn varint_len(mut v: u64) -> u64 {
    let mut n = 1;
    while v >= 0x80 {
        v >>= 7;
        n += 1;
    }
    n
}

/// Length of the `Range` message of a range (0, o, n) (§1.2).
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

/// Payload size of a reference entry (§1.2).
pub fn payload_len(ranges: &[(u64, u64)]) -> u64 {
    if ranges.len() == 1 {
        return range_msg_len(ranges[0].0, ranges[0].1);
    }
    ranges
        .iter()
        .map(|&(o, n)| {
            let r = range_msg_len(o, n);
            1 + varint_len(r) + r
        })
        .sum()
}

/// §1.2: every range within the file, every payload at most 65519 bytes.
pub fn check_output(out: &Output, size: u64) -> R<()> {
    for (k, e) in &out.entries {
        if let Entry::Ranges(rs) = e {
            for &(o, n) in rs {
                if o > MAX53 || n > MAX53 || (o as u128 + n as u128) > size as u128 {
                    rej!("range ({}, {}) of {} lies outside the file", o, n, k);
                }
            }
            if payload_len(rs) > 65519 {
                rej!("payload of {} exceeds 65519 bytes", k);
            }
        }
    }
    Ok(())
}

pub fn base64(data: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut s = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let b = [c[0], *c.get(1).unwrap_or(&0), *c.get(2).unwrap_or(&0)];
        let v = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        s.push(A[(v >> 18) as usize & 63] as char);
        s.push(A[(v >> 12) as usize & 63] as char);
        s.push(if c.len() > 1 { A[(v >> 6) as usize & 63] as char } else { '=' });
        s.push(if c.len() > 2 { A[v as usize & 63] as char } else { '=' });
    }
    s
}

pub fn serialize(url: &str, out: &Output) -> String {
    let mut s = String::new();
    s.push_str("{\"sources\":");
    Json::Arr(vec![Json::s(url)]).write(&mut s);
    s.push_str(",\"entries\":{");
    for (i, (k, e)) in out.entries.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        write_str(k, &mut s);
        s.push(':');
        match e {
            Entry::Ranges(rs) => {
                s.push_str("{\"ranges\":[");
                for (j, (o, n)) in rs.iter().enumerate() {
                    if j > 0 {
                        s.push(',');
                    }
                    let _ = write!(s, "[0,{},{}]", o, n);
                }
                s.push_str("]}");
            }
            Entry::Json(j) => {
                s.push_str("{\"json\":");
                j.write(&mut s);
                s.push('}');
            }
            Entry::Bytes(b) => {
                s.push_str("{\"base64\":");
                write_str(&base64(b), &mut s);
                s.push('}');
            }
        }
    }
    s.push_str("}}\n");
    s
}

/// §2.1 array zarr.json.
pub struct ArrayMeta {
    pub shape: Vec<u64>,
    pub data_type: String,
    pub chunk_shape: Vec<u64>,
    pub codecs: Vec<Json>,
    pub dims: Vec<String>,
}

pub fn array_json(a: &ArrayMeta) -> Json {
    Json::obj(vec![
        ("zarr_format", Json::i(3)),
        ("node_type", Json::s("array")),
        ("shape", Json::Arr(a.shape.iter().map(|&v| Json::i(v)).collect())),
        ("data_type", Json::s(&a.data_type)),
        (
            "chunk_grid",
            Json::obj(vec![
                ("name", Json::s("regular")),
                (
                    "configuration",
                    Json::obj(vec![(
                        "chunk_shape",
                        Json::Arr(a.chunk_shape.iter().map(|&v| Json::i(v)).collect()),
                    )]),
                ),
            ]),
        ),
        (
            "chunk_key_encoding",
            Json::obj(vec![
                ("name", Json::s("default")),
                ("configuration", Json::obj(vec![("separator", Json::s("/"))])),
            ]),
        ),
        ("fill_value", Json::i(0)),
        ("codecs", Json::Arr(a.codecs.clone())),
        ("dimension_names", Json::Arr(a.dims.iter().map(|d| Json::s(d)).collect())),
        ("attributes", Json::Obj(vec![])),
    ])
}

pub fn transpose_codec(dims: &[String]) -> Json {
    let mut order: Vec<Json> = Vec::new();
    let mut ci = 0;
    for (i, d) in dims.iter().enumerate() {
        if d == "c" {
            ci = i;
        } else {
            order.push(Json::i(i as u64));
        }
    }
    order.push(Json::i(ci as u64));
    Json::obj(vec![
        ("name", Json::s("transpose")),
        ("configuration", Json::obj(vec![("order", Json::Arr(order))])),
    ])
}

pub fn bytes_codec(item_size: u64, little: bool) -> Json {
    if item_size == 1 {
        Json::obj(vec![("name", Json::s("bytes"))])
    } else {
        Json::obj(vec![
            ("name", Json::s("bytes")),
            (
                "configuration",
                Json::obj(vec![("endian", Json::s(if little { "little" } else { "big" }))]),
            ),
        ])
    }
}

pub fn zlib_codec() -> Json {
    Json::obj(vec![
        ("name", Json::s("zlib")),
        ("configuration", Json::obj(vec![("level", Json::i(1))])),
    ])
}

pub fn zstd_codec() -> Json {
    Json::obj(vec![
        ("name", Json::s("zstd")),
        (
            "configuration",
            Json::obj(vec![("level", Json::i(0)), ("checksum", Json::Bool(false))]),
        ),
    ])
}

pub struct Axis {
    pub name: &'static str,
    pub unit: Option<&'static str>,
    pub scales: Vec<f64>, // one per level
}

/// §2.2 image group zarr.json.
pub fn image_json(name: Option<&str>, axes: &[Axis], nlevels: usize, omero: Option<Json>) -> R<Json> {
    let mut ms: Vec<(&str, Json)> = Vec::new();
    if let Some(n) = name {
        ms.push(("name", Json::s(n)));
    }
    let axes_json = axes
        .iter()
        .map(|a| {
            let t = match a.name {
                "t" => "time",
                "c" => "channel",
                _ => "space",
            };
            let mut m = vec![("name", Json::s(a.name)), ("type", Json::s(t))];
            if let Some(u) = a.unit {
                m.push(("unit", Json::s(u)));
            }
            Json::obj(m)
        })
        .collect();
    ms.push(("axes", Json::Arr(axes_json)));
    let mut ds = Vec::new();
    for l in 0..nlevels {
        let mut scale = Vec::new();
        for a in axes {
            let v = a.scales[l];
            if !v.is_finite() {
                rej!("scale of axis {} is not finite", a.name);
            }
            scale.push(Json::Num(v));
        }
        ds.push(Json::obj(vec![
            ("path", Json::Str(l.to_string())),
            (
                "coordinateTransformations",
                Json::Arr(vec![Json::obj(vec![
                    ("type", Json::s("scale")),
                    ("scale", Json::Arr(scale)),
                ])]),
            ),
        ]));
    }
    ms.push(("datasets", Json::Arr(ds)));
    let mut m = vec![("version", Json::s("0.5")), ("multiscales", Json::Arr(vec![Json::obj(ms)]))];
    if let Some(o) = omero {
        m.push(("omero", o));
    }
    Ok(Json::obj(vec![
        ("zarr_format", Json::i(3)),
        ("node_type", Json::s("group")),
        ("attributes", Json::obj(vec![("ome", Json::obj(m))])),
    ]))
}
