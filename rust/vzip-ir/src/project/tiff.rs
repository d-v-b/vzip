//! The TIFF image projection (conventions/tiff §4) from a valid IR: each level's
//! planes name the IR's tiles; the facts hold the format, sizes, scales and
//! translation.

use super::{arr, key, n, obj, strs, translations, truthy};
use crate::ir::Ir;
use crate::out::{array_json, image_ome, root_json, transpose_codec, Out};
use crate::refs::Refs;
use serde_json::{json, Value as J};

/// The codecs after `transpose` of a compression, for samples of `bits`.
fn codecs_of(compression: u64, bits: u64, little: bool) -> Vec<J> {
    match compression {
        33003 | 33004 | 33005 | 34712 => return vec![json!({"name": "imagecodecs_jpeg2k"})],
        7 => return vec![json!({"name": "imagecodecs_jpeg"})],
        _ => {}
    }
    let mut out = vec![if bits > 8 {
        json!({"name": "bytes", "configuration": {"endian": if little { "little" } else { "big" }}})
    } else {
        json!({"name": "bytes"})
    }];
    match compression {
        8 | 32946 => out.push(json!({"name": "zlib", "configuration": {"level": 1}})),
        50000 => out.push(json!({"name": "zstd", "configuration": {"level": 0, "checksum": false}})),
        _ => {}
    }
    out
}

/// The TIFF image: one array per level, its chunks the tiles of each plane.
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let little = facts["little"].as_bool().unwrap_or(false);
    let (spp, planar, bits) = (n(&facts["spp"]), n(&facts["planar"]), n(&facts["bits"]));
    let sizes = &facts["sizes"];
    let (size_t, size_c, size_z) = (n(&sizes["t"]), n(&sizes["c"]), n(&sizes["z"]));
    let plane_c = n(&facts["plane_c"]);
    let axes = strs(&facts["axes"]);
    let contig = spp > 1 && planar == 1;
    let kind = match n(&facts["sample_format"]) {
        1 => "uint",
        2 => "int",
        3 => "float",
        f => return Err(format!("internal: sample format {f}")),
    };
    let data_type = format!("{kind}{bits}");
    let mut codecs = if contig { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.extend(codecs_of(n(&facts["compression"]), bits, little));
    let codecs = J::Array(codecs);
    let mut refs = Refs::new(ir, Out::new(url));
    let levels = arr(&facts["levels"]);
    let mut shapes = Vec::with_capacity(levels.len());
    for (li, lv) in levels.iter().enumerate() {
        let (w, h, tw, th) = (n(&lv["w"]), n(&lv["h"]), n(&lv["tw"]), n(&lv["th"]));
        let of = |a: &str, t: u64, c: u64, z: u64, y: u64, x: u64| match a {
            "t" => t,
            "c" => c,
            "z" => z,
            "y" => y,
            _ => x,
        };
        let shape: Vec<u64> = axes.iter().map(|a| of(a, size_t, size_c, size_z, h, w)).collect();
        let chunk: Vec<u64> = axes.iter().map(|a| of(a, 1, if contig { spp } else { 1 }, 1, th, tw)).collect();
        refs.out.json(&format!("{li}/zarr.json"), &array_json(&shape, &data_type, json!(chunk), codecs.clone(), &axes));
        shapes.push(shape);
        let across = w.div_ceil(tw);
        let per = across * h.div_ceil(th);
        let planes = arr(&lv["planes"]);
        let prefix = format!("{li}/c/");
        for t in 0..size_t {
            for c in 0..plane_c {
                for z in 0..size_z {
                    let tiles = planes.get(((t * plane_c + c) * size_z + z) as usize).and_then(|v| v.as_u64());
                    let mut members = tiles.map(|e| ir.members(e as u32)).unwrap_or_default();
                    members.sort_unstable();
                    for (k, start, length, form) in members {
                        let (s, j) = (k / per, k % per);
                        let mut coords = Vec::with_capacity(5);
                        if size_t > 1 {
                            coords.push(t);
                        }
                        if size_c > 1 {
                            coords.push(if spp > 1 { if contig { 0 } else { s } } else { c });
                        }
                        if size_z > 1 {
                            coords.push(z);
                        }
                        coords.extend([j / across, j % across]);
                        refs.chunk(key(&prefix, coords), start, length, form, None, 0)?;
                    }
                }
            }
        }
    }
    let mut out = refs.out;
    let tr = &facts["translation"];
    let trs = J::Array(if truthy(tr) { vec![tr.clone(); levels.len()] } else { vec![] });
    let ome = image_ome(&axes, &obj(&facts["units"]), arr(&facts["scales"]), Some(&facts["name"]), translations(&trs))?;
    let own = json!({"byte_order": if little { "little" } else { "big" }, "bigtiff": facts["bigtiff"]});
    out.json("zarr.json", &root_json(ome, "tiff", url, Some(own)));
    out.summary = json!({"axes": axes, "levels": shapes, "references": out.refs.len()});
    Ok(out)
}
