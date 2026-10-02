//! `virtualize <url> <out.json>`: implements VIRTUALIZE.md (profiles version 0,
//! revision 1) for TIFF (§3) and ND2 (§4).

mod http;
mod json;
mod lv;
mod nd2;
mod tiff;
mod xml;

use json::{Value, int, obj, s};
use std::collections::BTreeMap;

#[derive(Debug)]
pub enum Error {
    /// The specification rejects the input (exit status 3).
    Reject(String),
    /// Anything else (I/O, network): exit status 1.
    Io(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Reject(m) => write!(f, "rejected: {m}"),
            Error::Io(m) => write!(f, "error: {m}"),
        }
    }
}

pub fn reject<T>(msg: impl Into<String>) -> Result<T, Error> {
    Err(Error::Reject(msg.into()))
}

pub enum Entry {
    Ranges(Vec<(u64, u64)>),
    Json(Value),
    Bytes(Vec<u8>),
}

pub type Output = BTreeMap<String, Entry>;

/// §2.3
pub fn unit_of(sym: &str) -> Option<&'static str> {
    Some(match sym {
        "\u{b5}m" | "\u{3bc}m" | "um" => "micrometer",
        "nm" => "nanometer",
        "mm" => "millimeter",
        "cm" => "centimeter",
        "m" => "meter",
        "\u{c5}" => "angstrom",
        "pm" => "picometer",
        "in" => "inch",
        "ft" => "foot",
        "s" => "second",
        "ms" => "millisecond",
        "min" => "minute",
        "h" => "hour",
        _ => return None,
    })
}

pub struct Axis {
    pub name: &'static str,
    pub unit: Option<&'static str>,
}

/// §2.1 array metadata.
pub fn array_json(shape: &[u64], dtype: &str, chunks: &[u64], codecs: Vec<Value>, dims: &[&str]) -> Value {
    let ints = |v: &[u64]| Value::List(v.iter().map(|&x| int(x)).collect());
    obj(vec![
        ("zarr_format", int(3)),
        ("node_type", s("array")),
        ("shape", ints(shape)),
        ("data_type", s(dtype)),
        (
            "chunk_grid",
            obj(vec![
                ("name", s("regular")),
                ("configuration", obj(vec![("chunk_shape", ints(chunks))])),
            ]),
        ),
        (
            "chunk_key_encoding",
            obj(vec![
                ("name", s("default")),
                ("configuration", obj(vec![("separator", s("/"))])),
            ]),
        ),
        ("fill_value", int(0)),
        ("codecs", Value::List(codecs)),
        ("dimension_names", Value::List(dims.iter().map(|d| s(d)).collect())),
        ("attributes", obj(vec![])),
    ])
}

/// §2.1 codecs list.
pub fn codecs(
    axes: &[&str],
    interleaved: bool,
    item_size: u32,
    endian: &str,
    jpeg2k: bool,
    compressor: Option<&str>,
) -> Vec<Value> {
    let mut v = Vec::new();
    if interleaved {
        let ci = axes.iter().position(|a| *a == "c").expect("c axis");
        let mut order: Vec<Value> = (0..axes.len()).filter(|&i| i != ci).map(|i| int(i as u64)).collect();
        order.push(int(ci as u64));
        v.push(obj(vec![
            ("name", s("transpose")),
            ("configuration", obj(vec![("order", Value::List(order))])),
        ]));
    }
    if jpeg2k {
        v.push(obj(vec![("name", s("imagecodecs_jpeg2k"))]));
    } else if item_size == 1 {
        v.push(obj(vec![("name", s("bytes"))]));
    } else {
        v.push(obj(vec![
            ("name", s("bytes")),
            ("configuration", obj(vec![("endian", s(endian))])),
        ]));
    }
    match compressor {
        Some("zlib") => v.push(obj(vec![
            ("name", s("zlib")),
            ("configuration", obj(vec![("level", int(1))])),
        ])),
        Some("zstd") => v.push(obj(vec![
            ("name", s("zstd")),
            ("configuration", obj(vec![("level", int(0)), ("checksum", Value::Bool(false))])),
        ])),
        Some(other) => panic!("unknown compressor {other}"),
        None => {}
    }
    v
}

/// §2.2 image group metadata.
pub fn image_json(
    name: Option<&str>,
    axes: &[Axis],
    scales: &[Vec<f64>],
    omero: Option<Value>,
) -> Result<Value, Error> {
    let mut ms: Vec<(&str, Value)> = Vec::new();
    if let Some(n) = name {
        ms.push(("name", s(n)));
    }
    let axes_v = axes
        .iter()
        .map(|a| {
            let ty = match a.name {
                "t" => "time",
                "c" => "channel",
                _ => "space",
            };
            let mut m = vec![("name", s(a.name)), ("type", s(ty))];
            if let Some(u) = a.unit {
                m.push(("unit", s(u)));
            }
            obj(m)
        })
        .collect();
    ms.push(("axes", Value::List(axes_v)));
    let ds = scales
        .iter()
        .enumerate()
        .map(|(i, sc)| {
            for x in sc {
                if !x.is_finite() {
                    return Err(Error::Reject(format!("non-finite scale {x}")));
                }
            }
            Ok(obj(vec![
                ("path", s(&i.to_string())),
                (
                    "coordinateTransformations",
                    Value::List(vec![obj(vec![
                        ("type", s("scale")),
                        ("scale", Value::List(sc.iter().map(|&x| Value::Float(x)).collect())),
                    ])]),
                ),
            ]))
        })
        .collect::<Result<Vec<_>, _>>()?;
    ms.push(("datasets", Value::List(ds)));
    let mut ome = vec![("version", s("0.5")), ("multiscales", Value::List(vec![obj(ms)]))];
    if let Some(o) = omero {
        ome.push(("omero", o));
    }
    Ok(group_json(obj(vec![("ome", obj(ome))])))
}

pub fn group_json(attributes: Value) -> Value {
    obj(vec![
        ("zarr_format", int(3)),
        ("node_type", s("group")),
        ("attributes", attributes),
    ])
}

fn serialize(url: &str, out: &Output) -> String {
    let mut o = String::new();
    o.push_str("{\"sources\":[");
    json::write_str(&mut o, url);
    o.push_str("],\"entries\":{");
    for (i, (k, e)) in out.iter().enumerate() {
        if i > 0 {
            o.push_str(",\n");
        }
        json::write_str(&mut o, k);
        o.push(':');
        match e {
            Entry::Ranges(r) => {
                o.push_str("{\"ranges\":[");
                for (j, (off, len)) in r.iter().enumerate() {
                    if j > 0 {
                        o.push(',');
                    }
                    o.push_str(&format!("[0,{off},{len}]"));
                }
                o.push_str("]}");
            }
            Entry::Json(v) => {
                o.push_str("{\"json\":");
                json::write(&mut o, v);
                o.push('}');
            }
            Entry::Bytes(b) => {
                o.push_str("{\"base64\":\"");
                o.push_str(&json::base64(b));
                o.push_str("\"}");
            }
        }
    }
    o.push_str("}}\n");
    o
}

fn run(url: &str) -> Result<(Output, u64), Error> {
    let mut src = http::Source::open(url)?;
    let head = src.read(0, src.size.min(16))?;
    let out = if head.len() >= 4
        && (head.starts_with(b"II") || head.starts_with(b"MM"))
        && matches!(
            (head[0], head[2], head[3]),
            (b'I', 42, 0) | (b'I', 43, 0) | (b'M', 0, 42) | (b'M', 0, 43)
        ) {
        tiff::virtualize(&mut src)?
    } else if head.len() >= 4 && (head[..4] == [0xDA, 0xCE, 0xBE, 0x0A] || head[..4] == [0, 0, 0, 0x0C]) {
        nd2::virtualize(&mut src)?
    } else {
        return reject("not a TIFF or ND2 file");
    };
    Ok((out, src.requests))
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: virtualize <url> <out.json>");
        std::process::exit(2);
    }
    let url = &args[1];
    match run(url) {
        Ok((out, requests)) => {
            let text = serialize(url, &out);
            if let Err(e) = std::fs::write(&args[2], text) {
                eprintln!("error: writing {}: {e}", args[2]);
                std::process::exit(1);
            }
            let refs = out.values().filter(|e| matches!(e, Entry::Ranges(_))).count();
            println!("{} entries ({} references), {} range requests", out.len(), refs, requests);
        }
        Err(e @ Error::Reject(_)) => {
            eprintln!("{e}");
            std::process::exit(3);
        }
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
