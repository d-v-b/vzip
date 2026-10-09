//! A projection's output (spec/virtualize.md §1.1, §1.2): the entries of a vzip archive
//! before it is written. The archive writer stays per host (Python's `Output.write`,
//! the browser's `writeVzip`); an `Out` crosses to it as Python objects (py.rs) or
//! as one binary buffer (`encode`, for wasm hosts).
//!
//! Also the shared parts of the conventions every projection uses (conventions §2–§5,
//! §7; `vzip/virtualize/common.py` in Python): arrays' and groups' documents,
//! OME-NGFF multiscales, the convention's declaration, reference sizes, and the
//! cuts of metadata arrays.

use serde_json::{json, Map, Value as J};
use std::collections::HashMap;

/// A range of an output: of source 0 (offset, length), of a data source (index ≥ 1,
/// offset, length), or literal bytes.
#[derive(Clone, Debug, PartialEq)]
pub enum Part {
    Src(u64, u64),
    Data(u32, u64, u64),
    Lit(Vec<u8>),
}

pub const MAX_PAYLOAD: usize = 65519;
pub const SOURCE_NODE: &str = "vzip_source";

#[derive(Default)]
pub struct Out {
    pub url: String,
    /// documents and copied bytes, in order of first write (a later write replaces)
    pub entries: Vec<(String, Vec<u8>)>,
    entry_at: HashMap<String, usize>,
    /// references
    pub refs: Vec<(String, Vec<Part>)>,
    ref_at: HashMap<String, usize>,
    /// the data sources 1, 2, ... in order of first use
    pub data: Vec<Vec<u8>>,
    data_at: HashMap<Vec<u8>, u32>,
    /// documents under these prefixes are read only when asked for, like chunks
    pub lazy: Vec<String>,
    pub summary: J,
}

/// JSON text as the hosts write documents: compact, UTF-8 (no \u escapes but
/// for control characters), members in insertion order.
pub fn to_text(v: &J) -> String {
    serde_json::to_string(v).unwrap()
}

impl Out {
    pub fn new(url: &str) -> Out {
        Out {
            url: url.to_string(),
            lazy: vec![format!("{SOURCE_NODE}/")],
            summary: json!({}),
            ..Default::default()
        }
    }

    pub fn json(&mut self, key: &str, v: &J) {
        self.bytes(key, to_text(v).into_bytes());
    }

    pub fn bytes(&mut self, key: &str, b: Vec<u8>) {
        match self.entry_at.get(key) {
            Some(&k) => self.entries[k].1 = b,
            None => {
                self.entry_at.insert(key.to_string(), self.entries.len());
                self.entries.push((key.to_string(), b));
            }
        }
    }

    pub fn has(&self, key: &str) -> bool {
        self.entry_at.contains_key(key)
    }

    pub fn refs(&mut self, key: &str, parts: Vec<Part>) {
        match self.ref_at.get(key) {
            Some(&k) => self.refs[k].1 = parts,
            None => {
                self.ref_at.insert(key.to_string(), self.refs.len());
                self.refs.push((key.to_string(), parts));
            }
        }
    }

    /// A range of all of `value`, as a data source: added the first time it is used.
    pub fn shared(&mut self, value: &[u8]) -> Part {
        let i = match self.data_at.get(value) {
            Some(&i) => i,
            None => {
                self.data.push(value.to_vec());
                let i = self.data.len() as u32;
                self.data_at.insert(value.to_vec(), i);
                i
            }
        };
        Part::Data(i, 0, value.len() as u64)
    }

    /// The whole output as one buffer (little-endian; strings and byte strings are a
    /// u32 length and the bytes): "VZO1", the url, the summary's JSON, the lazy
    /// prefixes, the data sources, the entries (key, bytes), and the references
    /// (key, then its parts: 0 source 0 (u64 offset, u64 length), 1 a data source
    /// (u32 index, u64, u64), 2 literal bytes).
    pub fn encode(&self) -> Vec<u8> {
        let mut b = Vec::new();
        let s = |b: &mut Vec<u8>, x: &[u8]| {
            b.extend_from_slice(&(x.len() as u32).to_le_bytes());
            b.extend_from_slice(x);
        };
        b.extend_from_slice(b"VZO1");
        s(&mut b, self.url.as_bytes());
        s(&mut b, to_text(&self.summary).as_bytes());
        b.extend_from_slice(&(self.lazy.len() as u32).to_le_bytes());
        for p in &self.lazy {
            s(&mut b, p.as_bytes());
        }
        b.extend_from_slice(&(self.data.len() as u32).to_le_bytes());
        for d in &self.data {
            s(&mut b, d);
        }
        b.extend_from_slice(&(self.entries.len() as u32).to_le_bytes());
        for (k, v) in &self.entries {
            s(&mut b, k.as_bytes());
            s(&mut b, v);
        }
        b.extend_from_slice(&(self.refs.len() as u32).to_le_bytes());
        for (k, parts) in &self.refs {
            s(&mut b, k.as_bytes());
            b.extend_from_slice(&(parts.len() as u32).to_le_bytes());
            for p in parts {
                match p {
                    Part::Src(o, n) => {
                        b.push(0);
                        b.extend_from_slice(&o.to_le_bytes());
                        b.extend_from_slice(&n.to_le_bytes());
                    }
                    Part::Data(i, o, n) => {
                        b.push(1);
                        b.extend_from_slice(&i.to_le_bytes());
                        b.extend_from_slice(&o.to_le_bytes());
                        b.extend_from_slice(&n.to_le_bytes());
                    }
                    Part::Lit(x) => {
                        b.push(2);
                        s(&mut b, x);
                    }
                }
            }
        }
        b
    }

    /// A 64-bit digest of the output (entries, references, data sources, summary), for
    /// comparing builds.
    pub fn digest(&self) -> u64 {
        let mut h: u64 = 0xcbf29ce484222325;
        for x in self.encode() {
            h ^= x as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        h
    }
}

// ---- reference sizes (§1.2)

fn varint_size(v: u64) -> usize {
    let bits = 64 - v.leading_zeros() as usize;
    bits.div_ceil(7).max(1)
}

fn range_size(r: &Part) -> usize {
    match r {
        Part::Lit(b) => 1 + varint_size(b.len() as u64) + b.len(),
        Part::Src(o, n) => [0, *o, *n].iter().filter(|&&v| v != 0).map(|&v| 1 + varint_size(v)).sum(),
        Part::Data(s, o, n) => [*s as u64, *o, *n].iter().filter(|&&v| v != 0).map(|&v| 1 + varint_size(v)).sum(),
    }
}

/// The encoded size of a reference to `ranges`.
pub fn payload_size(ranges: &[Part]) -> usize {
    if ranges.len() == 1 {
        return range_size(&ranges[0]);
    }
    ranges.iter().map(|r| {
        let n = range_size(r);
        1 + varint_size(n as u64) + n
    }).sum()
}

// ---- documents (conventions §2–§5)

pub const CONVENTION_KEY: &str = "vzip_virtualized";
/// The revision of spec/virtualize.md the projections follow (`common.REVISION` in Python).
pub const REVISION: u64 = 24;

/// (uuid, version, title) of a profile's convention.
pub fn profile(p: &str) -> (&'static str, u64, &'static str) {
    match p {
        "tiff" => ("48e9ac4e-1156-4a62-955e-20467d9c2700", 0, "TIFF"),
        "nd2" => ("59612f14-e314-4207-ba00-8f422ba71490", 0, "ND2"),
        "czi" => ("7a0733c7-d4be-4482-a64f-6904d9354ea5", 0, "CZI"),
        _ => panic!("no profile {p}"),
    }
}

pub fn convention(p: &str) -> J {
    let (uuid, version, title) = profile(p);
    let (r, blob) = if version == 0 {
        ("heads/main".to_string(), "main".to_string())
    } else {
        let t = format!("tags/virtualize-{p}-v{version}");
        (t.clone(), t)
    };
    json!({
        "uuid": uuid,
        "schema_url": format!("https://raw.githubusercontent.com/d-v-b/vzip/refs/{r}/spec/virtualize/{p}/schema.json"),
        "spec_url": format!("https://github.com/d-v-b/vzip/blob/{blob}/spec/virtualize/{p}.md"),
        "name": CONVENTION_KEY,
        "description": format!("The Zarr layout of a {title} source virtualized by vzip, and the source's metadata"),
    })
}

pub fn root_property(p: &str, url: &str) -> Map<String, J> {
    let version = profile(p).1;
    let mut m = Map::new();
    m.insert("profile".into(), json!(p));
    m.insert("version".into(), json!(version));
    if version == 0 {
        m.insert("revision".into(), json!(REVISION));
    }
    m.insert("source".into(), json!({"url": url}));
    m
}

/// A node's attributes: `attributes`, and the convention when the node is the root
/// (`url` given) or has source-specific metadata (`own` a nonempty object).
pub fn declare(attributes: Map<String, J>, p: &str, url: Option<&str>, own: Option<J>) -> Map<String, J> {
    let mut value = match url {
        Some(u) => root_property(p, u),
        None => Map::new(),
    };
    if let Some(o) = own {
        if o.as_object().is_some_and(|m| !m.is_empty()) {
            value.insert(p.to_string(), o);
        }
    }
    if value.is_empty() {
        return attributes;
    }
    let mut a = attributes;
    a.insert("zarr_conventions".into(), json!([convention(p)]));
    a.insert(CONVENTION_KEY.into(), J::Object(value));
    a
}

pub fn group_doc(attributes: Map<String, J>) -> J {
    json!({"zarr_format": 3, "node_type": "group", "attributes": attributes})
}

pub fn group_json(ome: J) -> J {
    let mut a = Map::new();
    a.insert("ome".into(), ome);
    group_doc(a)
}

pub fn root_json(ome: J, p: &str, url: &str, own: Option<J>) -> J {
    let mut a = Map::new();
    a.insert("ome".into(), ome);
    group_doc(declare(a, p, Some(url), own))
}

/// An array's zarr.json (conventions §3).
pub fn array_json(shape: &[u64], data_type: &str, chunk_shape: J, codecs: J, axes: &[&str]) -> J {
    json!({
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": axes,
        "attributes": {},
    })
}

/// The transpose codec for frames that hold the channel axis last.
pub fn transpose_codec(axes: &[&str]) -> J {
    let stored: Vec<&str> = axes.iter().copied().filter(|a| *a != "c").chain(["c"]).collect();
    let order: Vec<usize> = stored.iter().map(|a| axes.iter().position(|b| b == a).unwrap()).collect();
    json!({"name": "transpose", "configuration": {"order": order}})
}

fn axis_type(a: &str) -> &'static str {
    match a {
        "t" => "time",
        "c" => "channel",
        _ => "space",
    }
}

/// The OME-NGFF 0.5 object of an image, with a translation per level after its scale
/// when `translations` is given; a translation that is not finite rejects.
pub fn image_ome(axes: &[&str], units: &Map<String, J>, scales: &[J], name: Option<&J>, translations: Option<&[J]>) -> Result<J, String> {
    for t in translations.unwrap_or(&[]) {
        if t.as_array().is_some_and(|v| v.iter().any(|x| x.as_f64().is_some_and(|f| !f.is_finite()) || x.is_null())) {
            return Err("a translation is not finite".into());
        }
    }
    let mut ms = Map::new();
    if let Some(n) = name {
        if !n.is_null() {
            ms.insert("name".into(), n.clone());
        }
    }
    let ax: Vec<J> = axes
        .iter()
        .map(|a| {
            let mut m = Map::new();
            m.insert("name".into(), json!(a));
            m.insert("type".into(), json!(axis_type(a)));
            if let Some(u) = units.get(*a) {
                if u.as_str().is_some_and(|s| !s.is_empty()) {
                    m.insert("unit".into(), u.clone());
                }
            }
            J::Object(m)
        })
        .collect();
    ms.insert("axes".into(), J::Array(ax));
    let ds: Vec<J> = scales
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let mut t = vec![json!({"type": "scale", "scale": s})];
            if let Some(tr) = translations {
                t.push(json!({"type": "translation", "translation": tr[i]}));
            }
            json!({"path": i.to_string(), "coordinateTransformations": t})
        })
        .collect();
    ms.insert("datasets".into(), J::Array(ds));
    Ok(json!({"version": "0.5", "multiscales": [J::Object(ms)]}))
}

// ---- metadata arrays (conventions §7)

/// A chunk of a metadata array: the ranges that hold it, or its bytes when copied.
pub enum Chunk {
    Ranges(Vec<Part>),
    Bytes(Vec<u8>),
}

pub fn metadata_group(out: &mut Out, path: &str, attributes: Option<Map<String, J>>) {
    out.json(&format!("{path}/zarr.json"), &group_doc(attributes.unwrap_or_default()));
}

/// An array of the source metadata node: `dims` None gives no dimension names.
#[allow(clippy::too_many_arguments)]
pub fn metadata_array(out: &mut Out, path: &str, data_type: &str, shape: &[u64], chunk_shape: &[u64], dims: Option<&[&str]>,
                      chunks: Vec<(Vec<u64>, Chunk)>, endian: &str, compressor: Option<J>) {
    let mut codecs = if data_type == "uint8" || data_type == "int8" {
        vec![json!({"name": "bytes"})]
    } else {
        vec![json!({"name": "bytes", "configuration": {"endian": endian}})]
    };
    if let Some(c) = compressor {
        codecs.push(c);
    }
    let mut doc = array_json(shape, data_type, json!(chunk_shape), J::Array(codecs), dims.unwrap_or(&[]));
    if dims.is_none() {
        doc.as_object_mut().unwrap().remove("dimension_names");
    }
    out.json(&format!("{path}/zarr.json"), &doc);
    for (coords, c) in chunks {
        let key = std::iter::once(format!("{path}/c")).chain(coords.iter().map(|x| x.to_string())).collect::<Vec<_>>().join("/");
        match c {
            Chunk::Bytes(b) => out.bytes(&key, b),
            Chunk::Ranges(r) => out.refs(&key, r),
        }
    }
}

pub const MAX_CHUNK: u64 = 1 << 24;
const MAX_PADDING: u64 = (1 << 16) - (1 << 10);
const SMALL_PADDING: u64 = 1 << 10;
const MAX_TOTAL_PADDING: u64 = 1 << 20;

/// A run of the source's bytes (length ≥ 1) as a 1-D uint8 array: k chunks of equal
/// size ceil(length / k), k = ceil(length / 2^24), the last padded with zero bytes.
pub fn blob_chunks(offset: u64, length: u64) -> (u64, Vec<(Vec<u64>, Chunk)>) {
    let k = length.div_ceil(MAX_CHUNK);
    let size = length.div_ceil(k);
    let mut chunks = Vec::new();
    for i in 0..k {
        let n = size.min(length - i * size);
        let mut parts = vec![Part::Src(offset + i * size, n)];
        if n < size {
            parts.push(Part::Lit(vec![0; (size - n) as usize]));
        }
        chunks.push((vec![i], Chunk::Ranges(parts)));
    }
    (size, chunks)
}

/// The C-order array of `shape`, of `item`-byte elements contiguous at `offset`, cut
/// as conventions §7 cuts contiguous values (`common.grid_chunks` with no reader):
/// its chunk shape and chunks, or None when an edge chunk's padding does not fit in a
/// reference.
pub fn grid_chunks(offset: u64, shape: &[u64], item: u64, limit: u64) -> Option<(Vec<u64>, Vec<(Vec<u64>, Chunk)>)> {
    if item > limit {
        return None;
    }
    if shape.contains(&0) {
        return Some((shape.iter().map(|&v| v.max(1)).collect(), Vec::new()));
    }
    if shape.is_empty() {
        return Some((Vec::new(), vec![(Vec::new(), Chunk::Ranges(vec![Part::Src(offset, item)]))]));
    }
    let mut a = 0usize;
    let mut slab = item;
    for v in &shape[1..] {
        slab = slab.checked_mul(*v)?;
    }
    while slab > limit {
        a += 1;
        slab /= shape[a];
    }
    let total: u64 = shape.iter().try_fold(item, |acc, &v| acc.checked_mul(v))?;
    let (n, c) = loop {
        let n = shape[a];
        let k = n.div_ceil(limit / slab);
        let mut best: Option<(u64, u64)> = None;
        for q in k..=(2 * k).min(n) {
            let cq = n.div_ceil(q);
            let pad = (n.div_ceil(cq) * cq - n) * slab;
            if best.is_none_or(|b| pad < b.0) {
                best = Some((pad, cq));
            }
            if pad <= SMALL_PADDING {
                break;
            }
        }
        let best = best.unwrap();
        let outer: u64 = shape[..a].iter().product();
        let fits = best.0 <= MAX_PADDING && best.0 * outer <= MAX_TOTAL_PADDING.max(total / 64);
        if fits || a == shape.len() - 1 {
            break (n, if fits { best.1 } else { n.div_ceil(k) });
        }
        a += 1;
        slab /= shape[a];
    };
    let tail = vec![0u64; shape.len() - a - 1];
    let outer_count: u64 = shape[..a].iter().product();
    if outer_count.saturating_mul(n.div_ceil(c)) > 1 << 22 {
        return None;
    }
    let mut chunks = Vec::new();
    for base in 0..outer_count {
        let mut o = Vec::with_capacity(a);
        let mut rest = base;
        for v in shape[..a].iter().rev() {
            o.push(rest % v);
            rest /= v;
        }
        o.reverse();
        for q in 0..n.div_ceil(c) {
            let m = c.min(n - q * c);
            let mut parts = vec![Part::Src(offset + (base * n + q * c) * slab, m * slab)];
            if m < c {
                parts.push(Part::Lit(vec![0; ((c - m) * slab) as usize]));
                if payload_size(&parts) > MAX_PAYLOAD {
                    return None;
                }
            }
            let mut coords = o.clone();
            coords.push(q);
            coords.extend_from_slice(&tail);
            chunks.push((coords, Chunk::Ranges(parts)));
        }
    }
    let mut cs = vec![1u64; a];
    cs.push(c);
    cs.extend_from_slice(&shape[a + 1..]);
    Some((cs, chunks))
}

// ---- JSON sizes, as ECMAScript's JSON.stringify writes values (spec/virtualize/nd2.md §5.1,
// conventions §8.7)

/// The length of ECMAScript's Number::toString of a finite binary64.
pub fn js_number_size(x: f64) -> usize {
    if x == 0.0 {
        return 1;
    }
    let sign = usize::from(x < 0.0);
    // the shortest digits that round-trip, as d.ddde±x
    let s = format!("{:e}", x.abs());
    let (mantissa, exp) = s.split_once('e').unwrap();
    let k = mantissa.bytes().filter(u8::is_ascii_digit).count();
    let n = exp.parse::<i64>().unwrap() + 1;
    let k_i = k as i64;
    sign + if k_i <= n && n <= 21 {
        n as usize
    } else if 0 < n && n <= 21 {
        k + 1
    } else if -6 < n && n <= 0 {
        2 + (-n) as usize + k
    } else {
        let e = n - 1;
        1 + if k > 1 { k } else { 0 } + 2 + e.unsigned_abs().to_string().len()
    }
}

pub fn js_string_size(s: &str) -> usize {
    2 + s
        .chars()
        .map(|c| match c {
            '"' | '\\' | '\u{8}' | '\u{c}' | '\n' | '\r' | '\t' => 2,
            c if (c as u32) < 0x20 => 6,
            c => c.len_utf8(),
        })
        .sum::<usize>()
}

/// The length in UTF-8 bytes of `v` as JSON.stringify writes it, without whitespace.
pub fn json_size(v: &J) -> usize {
    match v {
        J::Null | J::Bool(true) => 4,
        J::Bool(false) => 5,
        J::Number(x) => match x.as_f64() {
            Some(f) if x.is_f64() => js_number_size(f),
            _ => x.to_string().len(),
        },
        J::String(s) => js_string_size(s),
        J::Array(a) => 2 + a.iter().map(json_size).sum::<usize>() + a.len().saturating_sub(1),
        J::Object(o) => 2 + o.iter().map(|(k, x)| js_string_size(k) + 1 + json_size(x)).sum::<usize>() + o.len().saturating_sub(1),
    }
}

