//! The CZI image projection (spec/virtualize/czi.md §4) from a valid IR: each image
//! level's cells and each tile array's members name the IR's data elements. Row
//! bands come from the facts: `rows` a band holds and `per` bands a tile row
//! (spec/virtualize/czi.md §4.3, with the parser's band floor).

use super::{arr, key, n, obj, strs, translations, truthy};
use crate::coding::{pixel_type, JPEG, JPEGXR, UNCOMPRESSED, ZSTD0, ZSTD1};
use crate::ir::Ir;
use crate::out::{array_json, declare, group_doc, group_json, image_ome, metadata_group, transpose_codec, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The node of the tile arrays (spec/virtualize/czi.md §4.4).
const TILES: &str = "tiles";
/// The most channel indexes of an image that get `omero` metadata.
const MAX_OMERO: u64 = 64;
/// The most bytes of a channel name, in UTF-8.
const MAX_NAME: usize = 256;

/// A subblock form: (pixel type, compression, hi-lo packing).
type Form = (i32, i32, bool);

fn form_of(v: &J) -> Result<(Form, &'static str, u64, u64), String> {
    let f = (v[0].as_i64().unwrap_or(-1) as i32, v[1].as_i64().unwrap_or(-1) as i32, v[2].as_bool().unwrap_or(false));
    let (dt, p, q) = pixel_type(f.0).ok_or_else(|| format!("internal: pixel type {}", f.0))?;
    Ok((f, dt, p, q))
}

fn bytes_codec(data_type: &str) -> J {
    if data_type == "uint8" || data_type == "int8" {
        json!({"name": "bytes"})
    } else {
        json!({"name": "bytes", "configuration": {"endian": "little"}})
    }
}

/// The codecs of a form (spec/virtualize/czi.md §3.1): `transpose` for samples, then the
/// compression's.
fn codecs(form: Form, data_type: &str, p: u64, axes: &[&str]) -> J {
    let mut out = if p > 1 { vec![transpose_codec(axes)] } else { vec![] };
    match form.1 {
        JPEG => out.push(json!({"name": "imagecodecs_jpeg"})),
        JPEGXR => out.push(json!({"name": "imagecodecs_jpegxr"})),
        ZSTD0 | ZSTD1 => {
            out.push(bytes_codec(data_type));
            if form.2 {
                out.push(json!({"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}}));
            }
            out.push(json!({"name": "zstd", "configuration": {"level": 0, "checksum": false}}));
        }
        _ => out.push(bytes_codec(data_type)),
    }
    J::Array(out)
}

/// The sample letters of a pixel's `p` samples (JPEG and JPEG XR decode to RGB order).
fn sample_letters(p: u64, compression: i32) -> &'static [&'static str] {
    let rgb = compression == JPEG || compression == JPEGXR;
    match (p, rgb) {
        (3, false) => &["B", "G", "R"],
        (4, false) => &["B", "G", "R", "A"],
        (3, true) => &["R", "G", "B"],
        (4, true) => &["R", "G", "B", "A"],
        _ => &[""],
    }
}

/// An array's zarr.json, with the rectilinear chunk grid when a length is not one integer.
fn array_doc(shape: &J, data_type: &str, lengths: &J, codecs: J, axes: &[&str]) -> J {
    let regular = arr(lengths).iter().all(|v| v.is_i64() || v.is_u64());
    let shape: Vec<u64> = arr(shape).iter().map(n).collect();
    let mut doc = array_json(&shape, data_type, if regular { lengths.clone() } else { json!([]) }, codecs, axes);
    if !regular {
        doc["chunk_grid"] = json!({"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": lengths}});
    }
    doc["fill_value"] = if data_type == "complex64" { json!([0.0, 0.0]) } else { json!(0) };
    doc
}

/// A channel's name when it is a nonempty string of at most 256 bytes.
fn name(v: &J) -> Option<&str> {
    v.as_str().filter(|s| !s.is_empty() && s.len() <= MAX_NAME)
}

/// `v` × `full`, an integer when `v` is one (as Python multiplies).
fn times(v: &J, full: i64) -> J {
    match v.as_i64() {
        Some(i) => json!(i * full),
        None => json!(v.as_f64().unwrap_or(0.0) * full as f64),
    }
}

/// The `omero` channels of an image (spec/virtualize/czi.md §4.2), or None past 64 channel indexes.
fn omero(values: &J, form: Form, data_type: &str, p: u64, lo_c: i64, channels: u64) -> Option<J> {
    if channels * p > MAX_OMERO {
        return None;
    }
    let letters = sample_letters(p, form.1);
    let type_bits: Option<i64> = match data_type {
        "uint8" => Some(8),
        "uint16" => Some(16),
        _ => None,
    };
    let (info, display) = (arr(&values["info"]), arr(&values["display"]));
    fn at(l: &[J], c: i64) -> Option<&J> {
        usize::try_from(c).ok().and_then(|c| l.get(c))
    }
    let mut out = Vec::new();
    for c in lo_c..lo_c + channels as i64 {
        let (i, d) = (at(info, c), at(display, c));
        let label = i
            .and_then(|i| name(&i["name"]))
            .or_else(|| d.and_then(|d| name(&d["name"])))
            .map(str::to_string)
            .unwrap_or_else(|| format!("C{c}"));
        let color = d
            .map(|d| &d["color"])
            .filter(|v| truthy(v))
            .or_else(|| i.map(|i| &i["color"]).filter(|v| truthy(v)))
            .cloned()
            .unwrap_or_else(|| json!("FFFFFF"));
        let window = type_bits.map(|tb| {
            let bits = [i.map(|i| &i["bits"]), Some(&values["bits"])]
                .into_iter()
                .flatten()
                .filter_map(|b| b.as_i64())
                .find(|&b| 1 <= b && b <= tb)
                .unwrap_or(tb);
            let (top, full) = ((1i64 << bits) - 1, (1i64 << tb) - 1);
            let low = d.map(|d| &d["low"]).filter(|v| !v.is_null());
            let high = d.map(|d| &d["high"]).filter(|v| !v.is_null());
            json!({"min": 0, "max": top, "start": low.map_or(json!(0), |v| times(v, full)),
                   "end": high.map_or(json!(top), |v| times(v, full))})
        });
        for letter in letters {
            let mut ch = Map::new();
            ch.insert("label".into(), json!(if letter.is_empty() { label.clone() } else { format!("{label} {letter}") }));
            let rgb = match *letter {
                "B" => Some("0000FF"),
                "G" => Some("00FF00"),
                "R" => Some("FF0000"),
                "A" => Some("FFFFFF"),
                _ => None,
            };
            ch.insert("color".into(), rgb.map_or_else(|| color.clone(), |x| json!(x)));
            ch.insert("active".into(), json!(true));
            if let Some(w) = &window {
                ch.insert("window".into(), w.clone());
            }
            out.push(J::Object(ch));
        }
    }
    Some(json!({"channels": out}))
}

/// Each cell's chunks: one per band of `rows` rows, at row × per + k.
fn cell_refs(refs: &mut Refs, path: &str, axes: &[&str], compression: i32, q: u64, band: &J, cells: &J) -> Result<(), String> {
    let ir = refs.ir;
    let (rows, per) = (n(&band["rows"]), n(&band["per"]));
    if rows == 0 {
        return Err("internal: a band of no rows".into());
    }
    let prefix = format!("{path}/c/");
    for cell in arr(cells) {
        let v: Vec<u64> = arr(cell).iter().map(n).collect();
        let [el, t, c, z, col, row, w, h] = v[..] else {
            return Err("internal: a cell is not 8 numbers".into());
        };
        let (Some(&start), Some(&length)) = (ir.start.get(el as usize), ir.len.get(el as usize)) else {
            return Err(format!("internal: no element {el}"));
        };
        let pieces: Vec<(u64, u64)> = if compression != UNCOMPRESSED {
            vec![(start, length)]
        } else {
            (0..h.div_ceil(rows)).map(|k| (start + k * rows * w * q, rows.min(h - k * rows) * w * q)).collect()
        };
        for (k, (o, len)) in pieces.into_iter().enumerate() {
            let y = row * per + k as u64;
            let coords = axes.iter().map(|a| match *a {
                "t" => t,
                "c" => c,
                "z" => z,
                "x" => col,
                _ => y,
            });
            refs.chunk(key(&prefix, coords), start, length, 0, Some(vec![Part::Src(o, len)]), k as u64)?;
        }
    }
    Ok(())
}

/// The CZI hierarchy: the root, an image group per series with an array per level,
/// and the tile arrays under `tiles`.
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let values = &facts["values"];
    let images = arr(&facts["images"]);
    let tiles = arr(&facts["tiles"]);
    let mut out = Out::new(url);
    out.lazy.push(format!("{TILES}/"));
    let mut root = Map::new();
    if !images.is_empty() {
        root.insert("ome".into(), json!({"version": "0.5", "bioformats2raw.layout": 3}));
    }
    out.json("zarr.json", &group_doc(declare(root, "czi", Some(url), Some(facts["root"].clone()))));
    if !images.is_empty() {
        let series: Vec<String> = (0..images.len()).map(|k| k.to_string()).collect();
        out.json("OME/zarr.json", &group_json(json!({"version": "0.5", "series": series})));
    }
    let mut refs = Refs::new(ir, out);
    for (k, im) in images.iter().enumerate() {
        let axes = strs(&im["axes"]);
        let (form, _, p, q) = form_of(&im["form"])?;
        let dtype = im["dtype"].as_str().unwrap_or("");
        let mut ome = image_ome(&axes, &obj(&im["units"]), arr(&im["scales"]), Some(&im["name"]), translations(&im["translations"]))?;
        if let Some(o) = omero(values, form, dtype, p, im["lo"]["c"].as_i64().unwrap_or(0), n(&im["extent"]["c"])) {
            ome["omero"] = o;
        }
        let dims = &im["dims"];
        let mut a = Map::new();
        a.insert("ome".into(), ome);
        let own = truthy(dims).then(|| json!({"dimensions": dims}));
        refs.out.json(&format!("{k}/zarr.json"), &group_doc(declare(a, "czi", None, own)));
        for (di, lv) in arr(&im["levels"]).iter().enumerate() {
            let path = format!("{k}/{di}");
            refs.out.json(&format!("{path}/zarr.json"), &array_doc(&lv["shape"], dtype, &lv["chunks"], codecs(form, dtype, p, &axes), &axes));
            cell_refs(&mut refs, &path, &axes, form.1, q, &lv["band"], &lv["cells"])?;
        }
    }
    if !tiles.is_empty() {
        metadata_group(&mut refs.out, TILES, None);
    }
    for (k, t) in tiles.iter().enumerate() {
        let axes = strs(&t["axes"]);
        let (form, _, p, q) = form_of(&t["form"])?;
        let dtype = t["dtype"].as_str().unwrap_or("");
        let mut own = Map::new();
        if truthy(&t["dims"]) {
            own.insert("dimensions".into(), t["dims"].clone());
        }
        for m in ["x", "y", "size", "stored_size", "planes", "copy"] {
            own.insert(m.into(), t[m].clone());
        }
        let mut doc = array_doc(&t["shape"], dtype, &t["chunks"], codecs(form, dtype, p, &axes), &axes);
        doc["attributes"] = J::Object(declare(Map::new(), "czi", None, Some(J::Object(own))));
        let path = format!("{TILES}/{k}");
        refs.out.json(&format!("{path}/zarr.json"), &doc);
        cell_refs(&mut refs, &path, &axes, form.1, q, &t["band"], &t["members"])?;
    }
    let mut out = refs.out;
    let summary_images: Vec<J> = images
        .iter()
        .map(|im| {
            J::Array(arr(&im["levels"]).iter().map(|lv| {
                let mut v = vec![lv["factor"].clone()];
                v.extend(arr(&lv["grid"]).iter().cloned());
                J::Array(v)
            }).collect())
        })
        .collect();
    out.summary = json!({"subblocks": facts["subblocks"], "images": summary_images, "tiles": tiles.len()});
    Ok(out)
}
