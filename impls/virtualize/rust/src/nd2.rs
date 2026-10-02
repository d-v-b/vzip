//! ND2 profile (VIRTUALIZE.md §4).

use crate::http::Source;
use crate::json::{Value, int, obj, s};
use crate::{Axis, Entry, Error, Output, array_json, codecs, group_json, image_json, lv, reject};
use std::collections::HashMap;

const MAGIC: u32 = 0x0ABE_CEDA;
const SIG_NAME: &str = "ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIG: &str = "ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME: &str = "ND2 FILEMAP SIGNATURE NAME 0001!";
const JP2_SIG: [u8; 12] = [0, 0, 0, 0x0C, 0x6A, 0x50, 0x20, 0x20, 0x0D, 0x0A, 0x87, 0x0A];

struct Header {
    name_len: u64,
    data_len: u64,
    name: String,
}

fn le32(b: &[u8]) -> u32 {
    u32::from_le_bytes(b[..4].try_into().unwrap())
}
fn le64(b: &[u8]) -> u64 {
    u64::from_le_bytes(b[..8].try_into().unwrap())
}

fn header(src: &mut Source, off: u64, want_name: bool) -> Result<Header, Error> {
    let h = src.read(off, 16)?;
    if le32(&h) != MAGIC {
        return reject(format!("no chunk magic at offset {off}"));
    }
    let name_len = le32(&h[4..]) as u64;
    let data_len = le64(&h[8..]);
    let name = if want_name {
        let nb = src.read(off + 16, name_len)?;
        let end = nb.iter().position(|&b| b == 0).unwrap_or(nb.len());
        String::from_utf8_lossy(&nb[..end]).into_owned()
    } else {
        String::new()
    };
    Ok(Header { name_len, data_len, name })
}

fn chunk_data(src: &mut Source, off: u64) -> Result<Vec<u8>, Error> {
    let h = header(src, off, false)?;
    src.read(off + 16 + h.name_len, h.data_len)
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Kind {
    Time,
    Position,
    Z,
}

#[derive(Debug)]
struct Loop {
    etype: i128,
    kind: Kind,
    count: u64,
    depth: u32,
    /// period in ms (time) or step in µm (z)
    param: f64,
}

fn count_of(v: Option<&Value>) -> Result<u64, Error> {
    match v.and_then(|x| x.as_int()) {
        Some(n) if n > 0 => Ok(u64::try_from(n).unwrap_or(u64::MAX)),
        Some(_) => Ok(0),
        None => reject("experiment loop has no integer count"),
    }
}

fn float_or0(v: Option<&Value>) -> f64 {
    v.and_then(|x| x.as_f64()).unwrap_or(0.0)
}

fn visit(node: &Value, depth: u32, list: &mut Vec<Loop>, nest: u32) -> Result<(), Error> {
    if nest > 64 {
        return reject("experiment tree too deep");
    }
    let Some(etype) = node.get("eType").and_then(|v| v.as_int()) else {
        return reject("experiment node without eType");
    };
    if !matches!(etype, 1 | 2 | 4 | 6 | 8) {
        return reject(format!("experiment loop type {etype} not supported"));
    }
    let Some(pars) = node.get("uLoopPars") else {
        return Ok(());
    };
    let (kind, count, param) = match etype {
        1 => (Kind::Time, count_of(pars.get("uiCount"))?, float_or0(pars.get("dPeriod"))),
        8 => {
            let periods = pars.get("pPeriod").map(|p| p.members()).unwrap_or_default();
            let valid = pars.get("pPeriodValid").map(|p| p.members()).unwrap_or_default();
            let mut total = 0u64;
            let mut period: Option<f64> = None;
            for (i, p) in periods.iter().enumerate() {
                let ok = valid.get(i).map(|v| v.truthy()).unwrap_or(false);
                if ok {
                    total = total.saturating_add(count_of(p.get("uiCount"))?);
                    if period.is_none() {
                        period = Some(float_or0(p.get("dPeriod")));
                    }
                }
            }
            (Kind::Time, total, period.unwrap_or(0.0))
        }
        2 => {
            let points = pars.get("Points").map(|p| p.members()).unwrap_or_default();
            let n = match node.get("pItemValid") {
                Some(valid) => {
                    let valid = valid.members();
                    (0..points.len())
                        .filter(|&i| valid.get(i).map(|v| v.truthy()).unwrap_or(false))
                        .count()
                }
                None => points.len(),
            };
            (Kind::Position, n as u64, 0.0)
        }
        4 => {
            let n = count_of(pars.get("uiCount"))?;
            let mut step = float_or0(pars.get("dZStep")).abs();
            if step == 0.0 && n > 1 {
                step = (float_or0(pars.get("dZHigh")) - float_or0(pars.get("dZLow"))).abs() / ((n - 1) as f64);
            }
            (Kind::Z, n, step)
        }
        _ => {
            // 6: spectral
            let n = match pars.get("uiCount") {
                Some(v) => count_of(Some(v))?,
                None => match pars.path("pPlanes/uiCount") {
                    Some(v) => count_of(Some(v))?,
                    None => 0,
                },
            };
            (Kind::Z, n, 0.0) // kind unused
        }
    };
    if count == 0 {
        return Ok(());
    }
    let children = node.get("ppNextLevelEx").map(|c| c.members()).unwrap_or_default();
    if etype == 6 {
        for ch in children {
            visit(ch, depth, list, nest + 1)?;
        }
        return Ok(());
    }
    let lp = Loop { etype, kind, count, depth, param };
    match list.last() {
        None => list.push(lp),
        Some(last) if last.depth < depth => list.push(lp),
        Some(last) if last.depth == depth && last.etype == etype && last.count < count => {
            *list.last_mut().unwrap() = lp;
        }
        _ => {}
    }
    for ch in children {
        visit(ch, depth + 1, list, nest + 1)?;
    }
    Ok(())
}

fn req_uint(attrs: &Value, name: &str) -> Result<u64, Error> {
    match attrs.get(name).and_then(|v| v.as_int()) {
        Some(n) if n >= 0 && n <= u64::MAX as i128 => Ok(n as u64),
        Some(n) => reject(format!("SLxImageAttributes/{name} = {n} is invalid")),
        None => reject(format!("SLxImageAttributes/{name} is missing")),
    }
}

pub fn virtualize(src: &mut Source) -> Result<Output, Error> {
    // §4.1 signature
    let start = src.read(0, src.size.min(16))?;
    if start.len() >= 12 && start[..12] == JP2_SIG {
        return reject("legacy (JPEG 2000) ND2 file");
    }
    let sig = header(src, 0, true)?;
    if sig.name_len != 32 || sig.name != SIG_NAME || sig.data_len != 64 {
        return reject("missing ND2 file signature chunk");
    }
    let sd = src.read(48, 64)?;
    let ver = sd.strip_prefix(b"Ver").ok_or(Error::Reject("signature data does not start with Ver".into()))?;
    let digits: Vec<u8> = ver.iter().take_while(|b| b.is_ascii_digit()).cloned().collect();
    if digits.is_empty() || ver.get(digits.len()) != Some(&b'.') || !ver.get(digits.len() + 1).is_some_and(|b| b.is_ascii_digit()) {
        return reject("signature data is not VerM.m");
    }
    let major: u64 = std::str::from_utf8(&digits).unwrap().parse().unwrap_or(u64::MAX);
    if major < 3 {
        return reject(format!("ND2 format version {major} is not supported (need 3 or later)"));
    }

    // chunk map
    if src.size < 40 {
        return reject("file too short for a chunk map");
    }
    let tail = src.read(src.size - 40, 40)?;
    if &tail[..32] != MAP_SIG.as_bytes() {
        return reject("missing chunk map signature at end of file");
    }
    let m = le64(&tail[32..]);
    let mh = header(src, m, true)?;
    if mh.name != FILEMAP_NAME {
        return reject("chunk map chunk has the wrong name");
    }
    let md = src.read(m + 16 + mh.name_len, mh.data_len)?;
    let mut map: HashMap<String, u64> = HashMap::new();
    let mut p = 0usize;
    let mut terminated = false;
    while p < md.len() {
        let Some(e) = md[p..].iter().position(|&b| b == b'!') else { break };
        let name = String::from_utf8_lossy(&md[p..p + e + 1]).into_owned();
        p += e + 1;
        if md.len() - p < 16 {
            return reject("chunk map record truncated");
        }
        let off = le64(&md[p..]);
        p += 16;
        if name == MAP_SIG {
            terminated = true;
            break;
        }
        map.insert(name, off);
    }
    if !terminated {
        return reject("chunk map does not end with its signature record");
    }
    let read_lv = |src: &mut Source, name: &str| -> Result<Option<Value>, Error> {
        match map.get(name) {
            Some(&o) => Ok(Some(lv::decode(&chunk_data(src, o)?)?)),
            None => Ok(None),
        }
    };

    // §4.3 attributes
    let Some(attrs_lv) = read_lv(src, "ImageAttributesLV!")? else {
        return reject("no ImageAttributesLV! chunk");
    };
    let Some(attrs) = attrs_lv.get("SLxImageAttributes") else {
        return reject("no SLxImageAttributes");
    };
    let width = req_uint(attrs, "uiWidth")?;
    let height = req_uint(attrs, "uiHeight")?;
    let width_bytes = req_uint(attrs, "uiWidthBytes")?;
    let comp = req_uint(attrs, "uiComp")?;
    let bpc = req_uint(attrs, "uiBpcInMemory")?;
    let bpc_sig = req_uint(attrs, "uiBpcSignificant")?;
    let compression = match attrs.get("eCompression") {
        None => 2,
        Some(v) => v.as_int().ok_or(Error::Reject("eCompression is not an integer".into()))?,
    };
    let dtype = match bpc {
        8 => "uint8",
        16 => "uint16",
        32 => "float32",
        _ => return reject(format!("uiBpcInMemory {bpc} not supported")),
    };
    let compressed = match compression {
        2 => false,
        0 => true,
        1 => return reject("lossy ND2 compression"),
        c => return reject(format!("eCompression {c} not supported")),
    };
    for (name, size) in [("uiTileWidth", width), ("uiTileHeight", height)] {
        if let Some(v) = attrs.get(name).and_then(|v| v.as_int()) {
            if v > 0 && v as u64 != size {
                return reject(format!("tiled ND2 ({name} = {v})"));
            }
        }
    }
    if comp == 0 || width == 0 || height == 0 {
        return reject("ND2 image has zero size");
    }

    // experiment
    let mut loops: Vec<Loop> = Vec::new();
    if let Some(meta) = read_lv(src, "ImageMetadataLV!")? {
        if let Some(exp) = meta.get("SLxExperiment") {
            if std::env::var_os("VIRTUALIZE_DEBUG").is_some() {
                let mut t = String::new();
                crate::json::write(&mut t, exp);
                eprintln!("SLxExperiment: {t}");
            }
            visit(exp, 0, &mut loops, 0)?;
        }
    }
    if std::env::var_os("VIRTUALIZE_DEBUG").is_some() {
        eprintln!("attributes: {attrs:?}");
        eprintln!("loops: {loops:?}");
    }
    for (i, a) in loops.iter().enumerate() {
        if loops[..i].iter().any(|b| b.kind == a.kind) {
            return reject(format!("two {:?} loops", a.kind));
        }
    }

    // picture metadata
    struct Plane {
        desc: String,
        color: u32,
        comps: u64,
    }
    let mut planes: Vec<Plane> = Vec::new();
    let mut calibration: Option<(f64, f64)> = None;
    if let Some(pm_lv) = read_lv(src, "ImageMetadataSeqLV|0!")? {
        if let Some(pm) = pm_lv.get("SLxPictureMetadata") {
            let n = pm.path("sPicturePlanes/uiCount").and_then(|v| v.as_int()).unwrap_or(0);
            let mut ok = true;
            for i in 0..n.max(0) {
                match pm.path(&format!("sPicturePlanes/sPlaneNew/a{i}")) {
                    Some(pl) => planes.push(Plane {
                        desc: pl.get("sDescription").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        color: pl.get("uiColor").and_then(|v| v.as_int()).unwrap_or(0xFFFFFF) as u32,
                        comps: pl
                            .get("uiCompCount")
                            .and_then(|v| v.as_int())
                            .map(|n| n.max(0) as u64)
                            .unwrap_or(1),
                    }),
                    None => ok = false,
                }
            }
            if !ok {
                planes.clear();
            }
            if pm.get("bCalibrated").map(|v| v.truthy()).unwrap_or(false) {
                if let Some(cal) = pm.get("dCalibration").and_then(|v| v.as_f64()) {
                    let aspect = pm.get("dAspect").and_then(|v| v.as_f64()).unwrap_or(1.0);
                    calibration = Some((cal, aspect));
                }
            }
        }
    }

    // §4.4 frames
    let n_frames: u64 = loops
        .iter()
        .try_fold(1u64, |a, l| a.checked_mul(l.count))
        .ok_or(Error::Reject("too many frames".into()))?;
    if n_frames > 100_000_000 {
        return reject("too many frames");
    }
    let r_bytes = width * comp * bpc / 8;
    let mut frame_off: Vec<Option<u64>> = Vec::with_capacity(n_frames as usize);
    for f in 0..n_frames {
        frame_off.push(map.get(&format!("ImageDataSeq|{f}!")).copied());
    }
    let mut frame_ranges: Vec<Option<Vec<(u64, u64)>>> = vec![None; n_frames as usize];
    if compressed {
        if width_bytes != r_bytes {
            return reject("compressed ND2 with row padding");
        }
        for f in 0..n_frames as usize {
            if let Some(o) = frame_off[f] {
                let h = header(src, o, false)?;
                if h.data_len < 8 {
                    return reject(format!("frame {f} is shorter than its timestamp"));
                }
                frame_ranges[f] = Some(vec![(o + 16 + h.name_len + 8, h.data_len - 8)]);
            }
        }
    } else {
        let present: Vec<usize> = (0..n_frames as usize).filter(|&f| frame_off[f].is_some()).collect();
        if let (Some(&first), Some(&last)) = (present.first(), present.last()) {
            let h0 = header(src, frame_off[first].unwrap(), false)?;
            let h1 = header(src, frame_off[last].unwrap(), false)?;
            if h0.name_len != h1.name_len {
                return reject("first and last frame chunks have different name lengths");
            }
            let n = h0.name_len;
            for &f in &present {
                let start = frame_off[f].unwrap() + 16 + n + 8;
                frame_ranges[f] = Some(if width_bytes == r_bytes {
                    vec![(start, height * r_bytes)]
                } else {
                    (0..height).map(|r| (start + r * width_bytes, r_bytes)).collect()
                });
            }
        }
    }

    // §4.5 channels
    let mut channels: Vec<(String, String)> = Vec::new();
    let total: u64 = planes.iter().map(|p| p.comps).sum();
    if !planes.is_empty() && total == comp && planes.iter().all(|p| p.comps == 1 || p.comps == 3) {
        for p in &planes {
            if p.comps == 1 {
                let c = p.color;
                channels.push((p.desc.clone(), format!("{:02X}{:02X}{:02X}", c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)));
            } else {
                for (suffix, col) in [("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF")] {
                    channels.push((format!("{} {suffix}", p.desc), col.to_string()));
                }
            }
        }
    } else {
        for k in 0..comp {
            channels.push((format!("C{k}"), "FFFFFF".to_string()));
        }
    }
    if bpc_sig > 126 {
        return reject("uiBpcSignificant too large");
    }
    let vmax: i128 = (1i128 << bpc_sig) - 1;
    let omero_channels: Vec<Value> = channels
        .iter()
        .map(|(label, color)| {
            let mut m = vec![("label", s(label)), ("color", s(color)), ("active", Value::Bool(true))];
            if dtype != "float32" {
                m.push((
                    "window",
                    obj(vec![
                        ("min", int(0)),
                        ("max", Value::Int(vmax)),
                        ("start", int(0)),
                        ("end", Value::Int(vmax)),
                    ]),
                ));
            }
            obj(m)
        })
        .collect();
    let omero = obj(vec![("channels", Value::List(omero_channels))]);

    // §4.6 output
    let find = |k: Kind| loops.iter().position(|l| l.kind == k);
    let (ti, pi, zi) = (find(Kind::Time), find(Kind::Position), find(Kind::Z));
    let n_pos = pi.map(|i| loops[i].count).unwrap_or(1);
    let has_c = comp > 1;
    let mut axes = Vec::new();
    let mut dims: Vec<&str> = Vec::new();
    let mut shape = Vec::new();
    let mut chunk = Vec::new();
    let mut scale = Vec::new();
    if let Some(i) = ti {
        let period = loops[i].param;
        let pos = period > 0.0;
        axes.push(Axis { name: "t", unit: pos.then_some("second") });
        dims.push("t");
        shape.push(loops[i].count);
        chunk.push(1);
        scale.push(if pos { period / 1000.0 } else { 1.0 });
    }
    if has_c {
        axes.push(Axis { name: "c", unit: None });
        dims.push("c");
        shape.push(comp);
        chunk.push(comp);
        scale.push(1.0);
    }
    if let Some(i) = zi {
        let step = loops[i].param;
        let pos = step > 0.0;
        axes.push(Axis { name: "z", unit: pos.then_some("micrometer") });
        dims.push("z");
        shape.push(loops[i].count);
        chunk.push(1);
        scale.push(if pos { step } else { 1.0 });
    }
    let xy_unit = calibration.map(|_| "micrometer");
    axes.push(Axis { name: "y", unit: xy_unit });
    axes.push(Axis { name: "x", unit: xy_unit });
    dims.extend(["y", "x"]);
    shape.extend([height, width]);
    chunk.extend([height, width]);
    match calibration {
        Some((cal, aspect)) => scale.extend([cal * aspect, cal]),
        None => scale.extend([1.0, 1.0]),
    }
    let codec_list = codecs(&dims, has_c, (bpc / 8) as u32, "little", false, compressed.then_some("zlib"));

    let mut out = Output::new();
    out.insert(
        "zarr.json".into(),
        Entry::Json(group_json(obj(vec![(
            "ome",
            obj(vec![("version", s("0.5")), ("bioformats2raw.layout", int(3))]),
        )]))),
    );
    out.insert(
        "OME/zarr.json".into(),
        Entry::Json(group_json(obj(vec![(
            "ome",
            obj(vec![
                ("version", s("0.5")),
                ("series", Value::List((0..n_pos).map(|p| s(&p.to_string())).collect())),
            ]),
        )]))),
    );
    for p in 0..n_pos {
        out.insert(
            format!("{p}/zarr.json"),
            Entry::Json(image_json(Some(&format!("position {p}")), &axes, &[scale.clone()], Some(omero.clone()))?),
        );
        out.insert(
            format!("{p}/0/zarr.json"),
            Entry::Json(array_json(&shape, dtype, &chunk, codec_list.clone(), &dims)),
        );
    }
    for f in 0..n_frames as usize {
        let Some(ranges) = frame_ranges[f].take() else { continue };
        // row-major coordinates over the loops, last fastest
        let mut coords = vec![0u64; loops.len()];
        let mut rem = f as u64;
        for i in (0..loops.len()).rev() {
            coords[i] = rem % loops[i].count;
            rem /= loops[i].count;
        }
        let p = pi.map(|i| coords[i]).unwrap_or(0);
        let mut key = format!("{p}/0/c");
        if let Some(i) = ti {
            key.push_str(&format!("/{}", coords[i]));
        }
        if has_c {
            key.push_str("/0");
        }
        if let Some(i) = zi {
            key.push_str(&format!("/{}", coords[i]));
        }
        key.push_str("/0/0");
        out.insert(key, Entry::Ranges(ranges));
    }
    Ok(out)
}
