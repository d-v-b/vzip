//! The ND2 image projection (spec/virtualize/nd2.md §4, and the root's source metadata
//! of §5.1) from a valid IR: the IR holds what it needs.

use super::{arr, n, truthy};
use crate::ir::{source_text, Ir, NO_INDEX, STRUCT};
use crate::lv::{base64, well_formed};
use crate::out::{array_json, group_json, image_ome, payload_size, root_json, transpose_codec, Out, Part, MAX_PAYLOAD};
use crate::types::{self, Ty, Val};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, HashMap, HashSet};

/// A decoded chunk with more JSON than this is on vzip_source.
const MAX_ROOT_JSON: usize = 1 << 14;
/// The most JSON of the root's chunks.
const MAX_ROOT_TOTAL: usize = 1 << 16;
/// The largest integer JSON holds exactly.
const MAX_SAFE: i128 = (1 << 53) - 1;
/// The names of the tags (spec/virtualize/nd2.md §5.1).
const TAGS: [&str; 3] = ["utf16", "int", "float"];

use crate::out::{js_string_size, json_size};

// ---- values as JSON (spec/virtualize/nd2.md §5.1)

/// An integer as JSON: itself, or a tag when JSON cannot hold it exactly.
fn int_json(v: i128) -> J {
    if v.abs() <= MAX_SAFE {
        json!(v as i64)
    } else {
        json!({"int": v.to_string()})
    }
}

/// A binary64 as JSON: itself, or a tag for NaN, the infinities and -0.
fn float_json(v: f64) -> J {
    if v.is_nan() {
        json!({"float": "NaN"})
    } else if v.is_infinite() {
        json!({"float": if v > 0.0 { "Infinity" } else { "-Infinity" }})
    } else if v == 0.0 && v.is_sign_negative() {
        json!({"float": "-0"})
    } else {
        json!(v)
    }
}

fn pair_like(j: &J) -> bool {
    j.as_array().is_some_and(|a| a.len() == 2 && a[0].is_string())
}

/// A name that is an array index (0, 1, ... without leading zeros): JavaScript would order it first.
fn is_index(s: &str) -> bool {
    s == "0" || (!s.is_empty() && !s.starts_with('0') && s.bytes().all(|c| c.is_ascii_digit()))
}

/// Named values as an object, or as [name, value] pairs when a name repeats, a name
/// is an array index, or the object would read as a tag (one member named utf16,
/// int or float).
fn pairs_or_object(records: Vec<(String, J)>) -> J {
    let distinct: HashSet<&str> = records.iter().map(|r| r.0.as_str()).collect();
    if distinct.len() < records.len()
        || (records.len() == 1 && TAGS.contains(&records[0].0.as_str()))
        || records.iter().any(|r| is_index(&r.0))
    {
        return J::Array(records.into_iter().map(|(k, v)| json!([k, v])).collect());
    }
    J::Object(records.into_iter().collect())
}

/// The bytes of a decoded value.
fn bytes_of(v: &Val) -> &[u8] {
    match v {
        Val::Bytes(b) => b,
        _ => &[],
    }
}

/// A string's exact JSON value: the text, or {"utf16": base64} when it is not
/// well-formed UTF-16.
fn exact_text(units: &[u8]) -> J {
    let u: Vec<u16> = units.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
    if well_formed(&u) {
        json!(String::from_utf16_lossy(&u))
    } else {
        json!({"utf16": base64(units)})
    }
}

/// An XML variant value by its runtype (spec/virtualize/nd2.md §5.1).
fn scalar(runtype: Option<&str>, value: &str) -> J {
    const INTS: [&str; 8] = ["lx_int8", "lx_int16", "lx_int32", "lx_int64", "lx_uint8", "lx_uint16", "lx_uint32", "lx_uint64"];
    let unsigned = value.strip_prefix(['+', '-']).unwrap_or(value);
    match runtype {
        Some(rt) if INTS.contains(&rt) && !unsigned.is_empty() && unsigned.bytes().all(|c| c.is_ascii_digit()) => {
            let sign = if value.starts_with('-') { "-" } else { "" };
            let digits = unsigned.trim_start_matches('0');
            let digits = if digits.is_empty() { "0" } else { digits };
            if digits.len() <= 20 {
                let v: i128 = digits.parse().unwrap();
                int_json(if sign == "-" { -v } else { v })
            } else {
                json!({"int": format!("{sign}{digits}")})
            }
        }
        Some("double" | "float") if crate::xml::decimal(value) => float_json(value.parse().unwrap_or(f64::NAN)),
        Some("bool") if value == "true" || value == "false" => json!(value == "true"),
        _ => json!(value),
    }
}

/// Bytes as text: UTF-8 if valid, else ISO 8859-1.
fn decode_text(b: &[u8]) -> String {
    match std::str::from_utf8(b) {
        Ok(s) => s.to_string(),
        Err(_) => b.iter().map(|&c| c as char).collect(),
    }
}

/// A fixed-size character field: its bytes up to the first NUL, as a text value.
fn json_text(b: &[u8]) -> J {
    let b = &b[..b.iter().position(|&c| c == 0).unwrap_or(b.len())];
    match std::str::from_utf8(b) {
        Ok(s) => json!(s),
        Err(_) => json!({"latin1": decode_text(b)}),
    }
}

/// The IR's values decoded by their types (each type parsed once).
struct Values<'a> {
    ir: &'a Ir,
    types: HashMap<u32, Option<Ty>>,
}

impl Values<'_> {
    fn type_name(&self, i: u32) -> &str {
        self.ir.types.get(self.ir.ty[i as usize])
    }

    /// The decoded value of element `i`.
    fn value(&mut self, i: u32) -> Result<Val, String> {
        let ir = self.ir;
        let raw = ir.value_bytes(i).ok_or_else(|| format!("internal: element {i} has no value"))?;
        let tid = ir.ty[i as usize];
        let ty = self.types.entry(tid).or_insert_with(|| types::parse(ir.types.get(tid)).ok());
        types::decode(ty.as_ref().ok_or("internal: a value of no type")?, raw)
    }

    /// The name the source gives an LV record or an XML element: its element's name
    /// with `source_names`'s rule undone (spec/virtualize/nd2.md §5.3).
    fn source_name(&self, i: u32) -> String {
        source_text(self.ir.names.get(self.ir.name[i as usize]), self.ir.nidx[i as usize])
    }

    /// An LV element's JSON: a level (struct) as an object, pairs or a list; a record by its type.
    fn lv_json(&mut self, i: u32) -> Result<J, String> {
        let ir = self.ir;
        if ir.kind[i as usize] == STRUCT {
            let mut members = ir.children(i).get(1..).unwrap_or(&[]); // a level's first child is its header
            if members.last().is_some_and(|&m| self.type_name(m).starts_with("bytes[")) {
                members = &members[..members.len() - 1]; // and its last, when it has records, the bytes it skips
            }
            return self.level(members, false);
        }
        let Val::Rec(fields) = self.value(i)? else {
            return Err(format!("internal: element {i} is not an LV record"));
        };
        let field = |k: &str| fields.iter().find(|f| f.0 == k).map(|f| &f.1);
        let v = field("v").ok_or("internal: an LV record without a value")?;
        Ok(match field("lv") {
            Some(Val::Int(1)) => json!(!matches!(v, Val::Int(0))),
            Some(Val::Int(8)) => {
                let b = bytes_of(v);
                exact_text(&b[..b.len().saturating_sub(2)])
            }
            Some(Val::Int(9)) => json!(bytes_of(v)),
            _ => match v {
                Val::Int(x) => int_json(*x),
                Val::Float(f) => float_json(*f),
                _ => return Err("internal: an LV number of no number".into()),
            },
        })
    }

    fn level(&mut self, members: &[u32], top: bool) -> Result<J, String> {
        let names: Vec<String> = members.iter().map(|&m| self.source_name(m)).collect();
        let items = members.iter().map(|&m| self.lv_json(m)).collect::<Result<Vec<J>, String>>()?;
        if !top && !members.is_empty() && names.iter().all(|n| n.is_empty()) {
            return Ok(if items.iter().all(pair_like) {
                J::Array(items.into_iter().map(|j| json!(["", j])).collect())
            } else {
                J::Array(items)
            });
        }
        Ok(pairs_or_object(names.into_iter().zip(items).collect()))
    }

    fn xml_json(&mut self, i: u32) -> Result<J, String> {
        let ir = self.ir;
        if ir.kind[i as usize] == STRUCT {
            let records = ir.children(i).iter().map(|&c| Ok((self.source_name(c), self.xml_json(c)?))).collect::<Result<Vec<_>, String>>()?;
            return Ok(pairs_or_object(records));
        }
        let Val::Str(v) = self.value(i)? else {
            return Err(format!("internal: element {i} is not XML text"));
        };
        Ok(scalar(self.type_name(i).strip_prefix("xml:"), &v))
    }

    /// A decoded chunk's JSON: an XML variant's, or its top-level LV records, always an object.
    fn chunk_json(&mut self, e: u32) -> Result<J, String> {
        if self.ir.name_of(e) == "xml" {
            let &root = self.ir.children(e).first().ok_or("internal: an XML chunk with no element")?;
            return self.xml_json(root);
        }
        self.level(self.ir.children(e), true)
    }
}

/// Chunks that grow with the frames: the events, and per-frame metadata after frame 0.
fn frame_scaled(name: &[u8]) -> bool {
    if name == b"ImageEventsLV!" || name == b"CustomData|ExperimentEventsV1_0!" {
        return true;
    }
    let Some(body) = name.strip_suffix(b"!") else { return false };
    let Some(bar) = body.iter().rposition(|&c| c == b'|') else { return false };
    let digits = &body[bar + 1..];
    !digits.is_empty()
        && digits.iter().all(u8::is_ascii_digit)
        && name.starts_with(b"ImageMetadataSeqLV|")
        && digits.iter().any(|&c| c != b'0')
}

/// The root's `chunks` (spec/virtualize/nd2.md §5.1): the decoded chunks that do not grow
/// with the frames, have at most 16 KiB of JSON, and fit the root's budget.
fn root_chunks(ir: &Ir, decoded: &[J]) -> Result<J, String> {
    let mut vals = Values { ir, types: HashMap::new() };
    let mut names: Vec<Vec<u8>> = Vec::new();
    let mut values: Vec<J> = Vec::new();
    let mut sizes: Vec<usize> = Vec::new();
    for d in decoded {
        let name: Vec<u8> = arr(&d[0]).iter().map(|c| n(c) as u8).collect();
        let (e, spent) = (n(&d[1]) as u32, n(&d[2]) as usize);
        if frame_scaled(&name) || spent > MAX_ROOT_JSON {
            // bound for vzip_source: each LV record and array byte is at least a byte of JSON
            values.push(J::Null);
            sizes.push(MAX_ROOT_JSON + 1);
        } else {
            let v = vals.chunk_json(e)?;
            sizes.push(json_size(&v));
            values.push(v);
        }
        names.push(name);
    }
    let mut on_node: Vec<bool> = (0..names.len()).map(|k| frame_scaled(&names[k]) || sizes[k] > MAX_ROOT_JSON).collect();
    let texts: Vec<String> = names.iter().map(|b| decode_text(b)).collect();
    let mut members: Vec<(usize, usize)> = (0..names.len())
        .filter(|&k| !on_node[k])
        .map(|k| (k, js_string_size(&texts[k]) + 1 + sizes[k]))
        .collect();
    let (mut total, mut count) = (members.iter().map(|m| m.1).sum::<usize>(), members.len());
    members.sort_by_key(|&(k, s)| (std::cmp::Reverse(s), k));
    for (k, s) in members {
        if 2 + total + count.saturating_sub(1) <= MAX_ROOT_TOTAL {
            break;
        }
        on_node[k] = true;
        total -= s;
        count -= 1;
    }
    let mut out = Map::new();
    for (k, v) in values.into_iter().enumerate() {
        if !on_node[k] {
            out.insert(texts[k].clone(), v);
        }
    }
    Ok(J::Object(out))
}

// ---- the image (spec/virtualize/nd2.md §4)

/// Each placed frame's pixels: f -> (start, length), runs expanded.
fn placed_frames(ir: &Ir) -> BTreeMap<u64, (u64, u64)> {
    let child = |i: u32, name: &str| ir.children(i).iter().copied().find(|&c| ir.name_of(c) == name);
    let mut out = BTreeMap::new();
    let Some(frames) = child(0, "frames") else { return out };
    for &c in ir.children(frames) {
        let Some(px) = child(c, "pixels") else { continue };
        let f0 = ir.nidx[c as usize];
        if f0 == NO_INDEX {
            continue;
        }
        let (start, length) = (ir.start[px as usize], ir.len[px as usize]);
        let (count, stride) = ir.run(c).unwrap_or((1, 0));
        for j in 0..count {
            out.insert(f0 + j, (start + j * stride, length));
        }
    }
    out
}

/// A number of the facts as Python's int() of it.
fn int(v: &J) -> i64 {
    v.as_i64().unwrap_or_else(|| v.as_f64().unwrap_or(0.0) as i64)
}

/// Labels and colors (spec/virtualize/nd2.md §4.2).
fn channels(planes: &J, comp: u64) -> (Vec<String>, Vec<String>) {
    let count = int(&planes["uiCount"]);
    let mut ps: BTreeMap<i64, (String, i64, i64)> = BTreeMap::new();
    for (k, v) in planes["sPlaneNew"].as_object().into_iter().flatten() {
        let Ok(k) = k.trim().parse::<i64>() else { continue };
        let desc = match &v["sDescription"] {
            J::String(s) => s.clone(),
            other => other.to_string(),
        };
        ps.insert(k, (desc, int(&v["uiColor"]), int(&v["uiCompCount"])));
    }
    let (mut labels, mut colors) = (Vec::new(), Vec::new());
    if count >= 1
        && ps.len() as i64 == count
        && ps.values().all(|p| p.2 == 1 || p.2 == 3)
        && ps.values().map(|p| p.2).sum::<i64>() == comp as i64
        && (0..count).all(|i| ps.contains_key(&i))
    {
        for (desc, abgr, k) in ps.values() {
            if *k == 3 {
                labels.extend([format!("{desc} R"), format!("{desc} G"), format!("{desc} B")]);
                colors.extend(["FF0000", "00FF00", "0000FF"].map(String::from));
            } else {
                labels.push(desc.clone());
                colors.push(format!("{:02X}{:02X}{:02X}", abgr & 255, (abgr >> 8) & 255, (abgr >> 16) & 255));
            }
        }
        return (labels, colors);
    }
    ((0..comp).map(|k| format!("C{k}")).collect(), vec!["FFFFFF".to_string(); comp as usize])
}

/// The ranges of `count` rows of `length` bytes, `stride` bytes apart from `start`.
fn rows(start: u64, stride: u64, length: u64, count: u64) -> Vec<Part> {
    (0..count).map(|r| Part::Src(start + r * stride, length)).collect()
}

/// The ND2 hierarchy: the root (with its source metadata), and an image per position
/// whose chunks are the frames (or their row blocks).
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let a = &facts["attributes"];
    let (width, height, wb) = (n(&a["width"]), n(&a["height"]), n(&a["width_bytes"]));
    let (comp, bpc, row) = (n(&a["components"]), n(&a["bits"]), n(&a["row"]));
    let compressed = truthy(&a["compressed"]);
    let significant = a["significant"].as_f64().unwrap_or(0.0);
    let data_type = match bpc {
        8 => "uint8",
        16 => "uint16",
        32 => "float32",
        b => return Err(format!("internal: {b} bits")),
    };
    let loops = arr(&facts["loops"]);
    let positions = n(&facts["positions"]);
    let pic = &facts["picture"];
    let (labels, colors) = channels(&pic["planes"], comp);
    let present = placed_frames(ir);
    let mut h = height;
    if !compressed && wb != row && !present.is_empty() {
        let far = present.values().map(|p| p.0).max().unwrap();
        h = (1..=height)
            .rev()
            .find(|&d| height % d == 0 && payload_size(&rows(far + (height - d) * wb, wb, row, d)) <= MAX_PAYLOAD)
            .unwrap_or(1);
    }
    let lp = |kind: &str| loops.iter().find(|l| l["kind"] == kind);
    let (t, z) = (lp("t"), lp("z"));
    let mut axes = Vec::new();
    if t.is_some() {
        axes.push("t");
    }
    if comp > 1 {
        axes.push("c");
    }
    if z.is_some() {
        axes.push("z");
    }
    axes.extend(["y", "x"]);
    let of = |ax: &str, tv: J, c: J, zv: J, y: J, x: J| match ax {
        "t" => tv,
        "c" => c,
        "z" => zv,
        "y" => y,
        _ => x,
    };
    let shape: Vec<u64> = axes
        .iter()
        .map(|ax| n(&of(ax, t.map_or(json!(1), |l| l["count"].clone()), json!(comp), z.map_or(json!(1), |l| l["count"].clone()), json!(height), json!(width))))
        .collect();
    let chunk_shape: Vec<u64> = axes.iter().map(|ax| n(&of(ax, json!(1), json!(comp), json!(1), json!(h), json!(width)))).collect();
    let s = &pic["scales"];
    let calibrated = truthy(&pic["calibrated"]);
    let positive = |l: Option<&J>| l.is_some_and(|l| l["scale"].as_f64().is_some_and(|x| x > 0.0));
    let (period, step) = (positive(t), positive(z));
    let pick = |on: bool, v: &J| if on { v.clone() } else { json!(1) };
    let scale: Vec<J> = axes
        .iter()
        .map(|ax| of(ax, pick(period, &s["t"]), json!(1), pick(step, &s["z"]), pick(calibrated, &s["y"]), pick(calibrated, &s["x"])))
        .collect();
    let mut units = Map::new();
    let unit = |on: bool, u: &str| if on { json!(u) } else { J::Null };
    units.insert("t".into(), unit(period, "second"));
    units.insert("z".into(), unit(step, "micrometer"));
    units.insert("y".into(), unit(calibrated, "micrometer"));
    units.insert("x".into(), unit(calibrated, "micrometer"));
    let mut codecs = if comp > 1 { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.push(if bpc > 8 { json!({"name": "bytes", "configuration": {"endian": "little"}}) } else { json!({"name": "bytes"}) });
    if compressed {
        codecs.push(json!({"name": "zlib", "configuration": {"level": 1}}));
    }
    let codecs = J::Array(codecs);
    let b = if significant == significant.trunc() && 1.0 <= significant && significant <= bpc as f64 { significant as u64 } else { bpc };
    let window = (data_type != "float32").then(|| {
        let top = (1u64 << b) - 1;
        json!({"min": 0, "max": top, "start": 0, "end": top})
    });
    let trs = arr(&pic["translations"]);
    let mut out = Out::new(url);
    let child = |i: u32, name: &str| ir.children(i).iter().copied().find(|&c| ir.name_of(c) == name);
    let sig = child(0, "signature")
        .and_then(|s| child(s, "data"))
        .and_then(|d| ir.value_bytes(d))
        .ok_or("internal: no signature")?;
    let root_meta = json!({"signature": json_text(sig), "chunks": root_chunks(ir, arr(&facts["decoded"]))?});
    out.json("zarr.json", &root_json(json!({"version": "0.5", "bioformats2raw.layout": 3}), "nd2", url, Some(root_meta)));
    let series: Vec<String> = (0..positions).map(|i| i.to_string()).collect();
    out.json("OME/zarr.json", &group_json(json!({"version": "0.5", "series": series})));
    for pi in 0..positions {
        let tr = if trs.is_empty() {
            None
        } else {
            let p = trs.get(pi as usize).ok_or("internal: no translation of a position")?;
            Some(vec![J::Array(axes.iter().map(|&ax| match ax {
                "x" | "y" => p[ax].clone(),
                _ => json!(0),
            }).collect())])
        };
        let mut ome = image_ome(&axes, &units, &[J::Array(scale.clone())], Some(&json!(format!("position {pi}"))), tr.as_deref())?;
        let chans: Vec<J> = labels
            .iter()
            .zip(&colors)
            .map(|(label, c)| {
                let mut m = Map::new();
                m.insert("label".into(), json!(label));
                m.insert("color".into(), json!(c));
                m.insert("active".into(), json!(true));
                if let Some(w) = &window {
                    m.insert("window".into(), w.clone());
                }
                J::Object(m)
            })
            .collect();
        ome["omero"] = json!({"channels": chans});
        out.json(&format!("{pi}/zarr.json"), &group_json(ome));
        out.json(&format!("{pi}/0/zarr.json"), &array_json(&shape, data_type, json!(chunk_shape), codecs.clone(), &axes));
    }
    for (&f, &(start, length)) in &present {
        let mut coords: HashMap<&str, u64> = HashMap::new();
        let mut rest = f;
        for l in loops.iter().rev() {
            let count = n(&l["count"]);
            if count == 0 {
                return Err("internal: a loop of no count".into());
            }
            let c = rest % count;
            coords.insert(l["kind"].as_str().unwrap_or(""), if truthy(&l["flip"]) { count - 1 - c } else { c });
            rest /= count;
        }
        let blocks: Vec<Vec<Part>> = if compressed || wb == row {
            vec![vec![Part::Src(start, if compressed { length } else { height * row })]]
        } else {
            (0..height).step_by(h as usize).map(|j| rows(start + j * wb, wb, row, h)).collect()
        };
        let prefix = format!("{}/0/c/", coords.get("p").copied().unwrap_or(0));
        for (j, ranges) in blocks.into_iter().enumerate() {
            let index = axes.iter().map(|&ax| match ax {
                "t" | "z" => coords.get(ax).copied().unwrap_or(0),
                "y" => j as u64,
                _ => 0,
            });
            out.refs(&super::key(&prefix, index), ranges);
        }
    }
    let mut sizes = Map::new();
    for l in loops {
        sizes.insert(l["kind"].as_str().unwrap_or("").to_string(), l["count"].clone());
    }
    sizes.insert("c".into(), json!(comp));
    sizes.insert("y".into(), json!(height));
    sizes.insert("x".into(), json!(width));
    let frames = &facts["frames"];
    let placed = present.len();
    let missing = match frames.as_i64() {
        Some(f) => json!(f - placed as i64),
        None => json!(frames.as_f64().unwrap_or(0.0) - placed as f64),
    };
    out.summary = json!({"sizes": sizes, "dataType": data_type, "compressed": a["compressed"], "paddedRows": wb != row,
                         "rowBlock": h, "positions": positions, "frames": frames, "missing": missing, "channels": labels});
    Ok(out)
}
