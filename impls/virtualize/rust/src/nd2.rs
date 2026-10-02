//! ND2 profile (§4).
use crate::common::*;
use crate::http::Source;
use crate::json::{J, obj};
use crate::lv::{self, Item, Lv};
use crate::rej;
use std::collections::HashMap;

const MAGIC: u32 = 0x0ABE_CEDA;
const SIG_NAME: &[u8] = b"ND2 FILE SIGNATURE CHUNK NAME01!";
const MAP_SIG: &[u8] = b"ND2 CHUNK MAP SIGNATURE 0000001!";
const FILEMAP_NAME: &[u8] = b"ND2 FILEMAP SIGNATURE NAME 0001!";

struct ChunkHeader {
    n: u64,
    d: u64,
}

fn u32le(b: &[u8]) -> u32 {
    u32::from_le_bytes(b[..4].try_into().unwrap())
}
fn u64le(b: &[u8]) -> u64 {
    u64::from_le_bytes(b[..8].try_into().unwrap())
}

fn header(src: &mut Source, o: u64) -> Res<ChunkHeader> {
    if o > MAX_SAFE {
        rej!("chunk offset {o} above 2^53-1");
    }
    let h = src.read(o, 16)?;
    if u32le(&h) != MAGIC {
        rej!("chunk at {o}: bad magic");
    }
    Ok(ChunkHeader { n: u32le(&h[4..]) as u64, d: u64le(&h[8..]) })
}

fn chunk_name(src: &mut Source, o: u64, h: &ChunkHeader) -> Res<Vec<u8>> {
    src.read(o + 16, h.n)
}

fn chunk_data(src: &mut Source, o: u64, h: &ChunkHeader) -> Res<Vec<u8>> {
    src.read(o + 16 + h.n, h.d)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Time,
    Position,
    Z,
}

#[derive(Clone, Copy, Debug)]
struct Loop {
    kind: Kind,
    depth: u64,
    count: u64,
    param: f64, // period (ms) or z step
}

fn valid(validity: Option<&Lv>, i: usize) -> Res<bool> {
    match validity {
        None => Ok(true),
        Some(v) => match lv::members_of(v).get(i) {
            None => Ok(false),
            Some(&it) => lv::flag_item(it, "validity member"),
        },
    }
}

fn check_validity_list<'a>(v: Option<&'a Lv>, what: &str) -> Res<Option<&'a Lv>> {
    match v {
        None => Ok(None),
        Some(v) => {
            let l = lv::list(v, what)?;
            for it in lv::members_of(l) {
                lv::flag_item(it, what)?;
            }
            Ok(Some(l))
        }
    }
}

fn opt_int(o: &Lv, name: &str, default: u64) -> Res<u64> {
    match lv::member(o, name)? {
        Some(v) => lv::integer(v, name),
        None => Ok(default),
    }
}

fn opt_num(o: &Lv, name: &str, default: f64) -> Res<f64> {
    match lv::member(o, name)? {
        Some(v) => lv::number(v, name),
        None => Ok(default),
    }
}

/// Checks a node (and its subtree) and, when `visit`, appends loops (§4.3).
fn walk(node: &Lv, depth: u64, visit: bool, loops: &mut Vec<Loop>) -> Res<()> {
    let node = lv::object(node, "experiment node")?;
    let etype = match lv::member(node, "eType")? {
        Some(v) => lv::integer(v, "eType")?,
        None => rej!("experiment node without eType"),
    };
    if !matches!(etype, 1 | 2 | 4 | 6 | 8) {
        rej!("experiment eType {etype}");
    }
    let pars = match lv::member(node, "uLoopPars")? {
        Some(v) => Some(lv::object(v, "uLoopPars")?),
        None => None,
    };
    let item_valid = check_validity_list(lv::member(node, "pItemValid")?, "pItemValid")?;
    let children: Vec<Item> = match lv::member(node, "ppNextLevelEx")? {
        Some(v) => lv::members_of(lv::object_or_list(v, "ppNextLevelEx")?),
        None => Vec::new(),
    };
    let mut child_nodes = Vec::new();
    for c in &children {
        match c {
            Item::V(v @ Lv::Obj(_)) => child_nodes.push(*v),
            _ => rej!("ppNextLevelEx member is not an object"),
        }
    }
    // (kind, count, param); kind None = spectral
    let mut lp: Option<(Option<Kind>, u64, f64)> = None;
    if let Some(p) = pars {
        lp = Some(match etype {
            1 => (Some(Kind::Time), opt_int(p, "uiCount", 0)?, opt_num(p, "dPeriod", 0.0)?),
            8 => {
                let pv = check_validity_list(lv::member(p, "pPeriodValid")?, "pPeriodValid")?;
                let mems = match lv::member(p, "pPeriod")? {
                    Some(v) => lv::members_of(lv::object_or_list(v, "pPeriod")?),
                    None => Vec::new(),
                };
                let mut count: u128 = 0;
                let mut period: Option<f64> = None;
                for (i, m) in mems.iter().enumerate() {
                    let m = match m {
                        Item::V(v @ Lv::Obj(_)) => *v,
                        _ => rej!("pPeriod member is not an object"),
                    };
                    if valid(pv, i)? {
                        let c = match lv::member(m, "uiCount")? {
                            Some(v) => lv::integer(v, "pPeriod/uiCount")?,
                            None => rej!("pPeriod member without uiCount"),
                        };
                        count += c as u128;
                        let d = opt_num(m, "dPeriod", 0.0)?;
                        if period.is_none() {
                            period = Some(d);
                        }
                    }
                }
                if count > MAX_SAFE as u128 {
                    rej!("time loop count above 2^53-1");
                }
                (Some(Kind::Time), count as u64, period.unwrap_or(0.0))
            }
            2 => {
                let mems = match lv::member(p, "Points")? {
                    Some(v) => lv::members_of(lv::object_or_list(v, "Points")?),
                    None => Vec::new(),
                };
                let mut c = 0u64;
                for i in 0..mems.len() {
                    if valid(item_valid, i)? {
                        c += 1;
                    }
                }
                (Some(Kind::Position), c, 0.0)
            }
            4 => {
                let count = opt_int(p, "uiCount", 0)?;
                let step = opt_num(p, "dZStep", 0.0)?;
                let low = opt_num(p, "dZLow", 0.0)?;
                let high = opt_num(p, "dZHigh", 0.0)?;
                let mut s = step.abs();
                if s == 0.0 && count > 1 {
                    s = check_finite((high - low).abs() / (count - 1) as f64, "z step")?;
                }
                (Some(Kind::Z), count, s)
            }
            6 => {
                let c = match lv::member(p, "uiCount")? {
                    Some(v) => lv::integer(v, "uiCount")?,
                    None => match lv::member(p, "pPlanes")? {
                        Some(pp) => opt_int(pp, "uiCount", 0)?,
                        None => 0,
                    },
                };
                (None, c, 0.0)
            }
            _ => unreachable!(),
        });
    }
    let mut child_visit = visit;
    let mut child_depth = depth + 1;
    match lp {
        None => child_visit = false,
        Some((_, 0, _)) => child_visit = false,
        Some((None, _, _)) => child_depth = depth,
        Some((Some(kind), count, param)) => {
            if visit {
                let lpv = Loop { kind, depth, count, param };
                match loops.last() {
                    None => loops.push(lpv),
                    Some(l) if l.depth < depth => loops.push(lpv),
                    Some(l) if l.depth == depth && l.kind == kind && l.count < count => {
                        *loops.last_mut().unwrap() = lpv;
                    }
                    _ => {}
                }
            }
        }
    }
    for c in child_nodes {
        walk(c, child_depth, child_visit, loops)?;
    }
    Ok(())
}

struct Plane {
    desc: String,
    color: u32,
    comps: u64,
}

pub fn virtualize(src: &mut Source, out: &mut Output) -> Res<()> {
    let size = src.size;
    // Signature chunk
    let h0 = header(src, 0)?;
    if h0.n != 32 || h0.d != 64 {
        rej!("signature chunk: name length {} / data length {}", h0.n, h0.d);
    }
    if chunk_name(src, 0, &h0)? != SIG_NAME {
        rej!("signature chunk: wrong name");
    }
    let sd = chunk_data(src, 0, &h0)?;
    if !sd.starts_with(b"Ver") {
        rej!("signature data does not start with 'Ver'");
    }
    let digits = sd[3..].iter().take_while(|c| c.is_ascii_digit()).count();
    if digits == 0 || sd.get(3 + digits) != Some(&b'.') {
        rej!("signature data: bad version");
    }
    let major: u64 = std::str::from_utf8(&sd[3..3 + digits]).unwrap().trim_start_matches('0').parse().unwrap_or(
        if sd[3..3 + digits].iter().all(|&c| c == b'0') { 0 } else { u64::MAX },
    );
    if major < 3 {
        rej!("ND2 version {major} < 3");
    }
    // Chunk map
    if size < 40 {
        rej!("file shorter than 40 bytes");
    }
    let tail = src.read(size - 40, 40)?;
    if &tail[..32] != MAP_SIG {
        rej!("missing chunk map signature");
    }
    let m = u64le(&tail[32..]);
    let mh = header(src, m)?;
    let mut mname = chunk_name(src, m, &mh)?;
    if let Some(p) = mname.iter().position(|&c| c == 0) {
        mname.truncate(p);
    }
    if mname != FILEMAP_NAME {
        rej!("chunk map chunk has the wrong name");
    }
    let md = chunk_data(src, m, &mh)?;
    let mut map: HashMap<Vec<u8>, u64> = HashMap::new();
    let mut p = 0usize;
    loop {
        let e = match md[p..].iter().position(|&c| c == b'!') {
            Some(e) => p + e + 1,
            None => rej!("chunk map: record runs past the end / no terminating record"),
        };
        let name = md[p..e].to_vec();
        if name == MAP_SIG {
            break;
        }
        if e + 16 > md.len() {
            rej!("chunk map: record runs past the end of the data");
        }
        let off = u64le(&md[e..]);
        map.insert(name, off);
        p = e + 16;
    }

    // Attributes
    let attr_off = match map.get(&b"ImageAttributesLV!"[..]) {
        Some(&o) => o,
        None => rej!("no ImageAttributesLV! chunk"),
    };
    let ah = header(src, attr_off)?;
    let ad = chunk_data(src, attr_off, &ah)?;
    let at = lv::parse_chunk(&ad)?;
    if std::env::var("VZ_DEBUG").is_ok() {
        eprintln!("ImageAttributesLV: {at:?}");
        eprintln!("map: {:?}", map.iter().map(|(k, v)| (String::from_utf8_lossy(k).to_string(), *v)).collect::<Vec<_>>());
    }
    let a = match lv::member(&at, "SLxImageAttributes")? {
        Some(v) => lv::object(v, "SLxImageAttributes")?,
        None => rej!("no SLxImageAttributes"),
    };
    let req_int = |name: &str| -> Res<u64> {
        match lv::member(a, name)? {
            Some(v) => lv::integer(v, name),
            None => rej!("SLxImageAttributes/{name} missing"),
        }
    };
    let width = req_int("uiWidth")?;
    let height = req_int("uiHeight")?;
    let width_bytes = req_int("uiWidthBytes")?;
    let comp = req_int("uiComp")?;
    let bpc = req_int("uiBpcInMemory")?;
    if width < 1 || height < 1 || comp < 1 {
        rej!("uiWidth/uiHeight/uiComp must be at least 1");
    }
    let bpc_sig = match lv::member(a, "uiBpcSignificant")? {
        Some(v) => lv::number(v, "uiBpcSignificant")?,
        None => rej!("uiBpcSignificant missing"),
    };
    let ecomp = opt_int(a, "eCompression", 2)?;
    let tile_w = opt_int(a, "uiTileWidth", 0)?;
    let tile_h = opt_int(a, "uiTileHeight", 0)?;
    let dtype = match bpc {
        8 => "uint8",
        16 => "uint16",
        32 => "float32",
        _ => rej!("uiBpcInMemory {bpc}"),
    };
    let compressed = match ecomp {
        2 => false,
        0 => true,
        _ => rej!("eCompression {ecomp}"),
    };
    if (tile_w > 0 && tile_w != width) || (tile_h > 0 && tile_h != height) {
        rej!("tiled image ({tile_w}x{tile_h})");
    }

    // Experiment
    let mut loops: Vec<Loop> = Vec::new();
    if let Some(&o) = map.get(&b"ImageMetadataLV!"[..]) {
        let h = header(src, o)?;
        let d = chunk_data(src, o, &h)?;
        let t = lv::parse_chunk(&d)?;
        if std::env::var("VZ_DEBUG").is_ok() {
            eprintln!("ImageMetadataLV: {t:?}");
        }
        if let Some(root) = lv::member(&t, "SLxExperiment")? {
            walk(root, 0, true, &mut loops)?;
        }
    }
    if std::env::var("VZ_DEBUG").is_ok() {
        eprintln!("loops: {loops:?}");
    }
    for i in 0..loops.len() {
        for j in i + 1..loops.len() {
            if loops[i].kind == loops[j].kind {
                rej!("two {:?} loops", loops[i].kind);
            }
        }
    }

    // Picture metadata
    let mut calibration: Option<f64> = None;
    let mut aspect = 1.0;
    let mut plane_count = 0u64;
    let mut planes: HashMap<u64, Plane> = HashMap::new();
    if let Some(&o) = map.get(&b"ImageMetadataSeqLV|0!"[..]) {
        let h = header(src, o)?;
        let d = chunk_data(src, o, &h)?;
        let t = lv::parse_chunk(&d)?;
        if std::env::var("VZ_DEBUG").is_ok() {
            eprintln!("ImageMetadataSeqLV|0: {t:?}");
        }
        if let Some(pm) = lv::member(&t, "SLxPictureMetadata")? {
            let pm = lv::object(pm, "SLxPictureMetadata")?;
            let calibrated = match lv::member(pm, "bCalibrated")? {
                Some(v) => lv::flag_item(Item::V(v), "bCalibrated")?,
                None => false,
            };
            let dcal = match lv::member(pm, "dCalibration")? {
                Some(v) => Some(lv::number(v, "dCalibration")?),
                None => None,
            };
            let dasp = opt_num(pm, "dAspect", 1.0)?;
            if calibrated {
                if let Some(c) = dcal {
                    if c > 0.0 {
                        calibration = Some(c);
                    }
                }
            }
            aspect = if dasp > 0.0 { dasp } else { 1.0 };
            if let Some(sp) = lv::member(pm, "sPicturePlanes")? {
                let sp = lv::object(sp, "sPicturePlanes")?;
                plane_count = opt_int(sp, "uiCount", 0)?;
                if let Some(new) = lv::member(sp, "sPlaneNew")? {
                    let new = lv::object(new, "sPlaneNew")?;
                    if let Lv::Obj(ms) = new {
                        for (name, v) in ms {
                            let Some(ix) = name.strip_prefix('a') else { continue };
                            if ix.is_empty()
                                || !ix.bytes().all(|c| c.is_ascii_digit())
                                || (ix.len() > 1 && ix.starts_with('0'))
                                || ix.len() > 16
                            {
                                continue;
                            }
                            let i: u64 = ix.parse().unwrap();
                            if i >= plane_count {
                                continue;
                            }
                            let pl = lv::object(v, "plane")?;
                            let desc = match lv::member(pl, "sDescription")? {
                                Some(s) => lv::string(s, "sDescription")?.to_string(),
                                None => String::new(),
                            };
                            let color = match lv::member(pl, "uiColor")? {
                                Some(c) => lv::color(c, "uiColor")?,
                                None => 0xFFFFFF,
                            };
                            let comps = opt_int(pl, "uiCompCount", 1)?;
                            planes.insert(i, Plane { desc, color, comps });
                        }
                    }
                }
            }
        }
    }

    // Frames (§4.4)
    let r128 = (width as u128) * (comp as u128) * (bpc as u128) / 8;
    if r128 > MAX_SAFE as u128 {
        rej!("row length above 2^53-1");
    }
    let r = r128 as u64;
    if width_bytes < r {
        rej!("uiWidthBytes {width_bytes} < {r}");
    }
    if compressed && width_bytes != r {
        rej!("compressed frames with padded rows");
    }
    let n_frames: u128 = loops.iter().fold(1u128, |acc, l| acc.saturating_mul(l.count as u128));
    let mut frames: Vec<(u64, u64)> = Vec::new(); // (f, chunk offset)
    for (name, &o) in &map {
        let Some(rest) = name.strip_prefix(&b"ImageDataSeq|"[..]) else { continue };
        let Some(num) = rest.strip_suffix(b"!") else { continue };
        if num.is_empty() || !num.iter().all(|c| c.is_ascii_digit()) || (num.len() > 1 && num[0] == b'0') {
            continue;
        }
        if num.len() > 30 {
            continue;
        }
        let f: u128 = std::str::from_utf8(num).unwrap().parse().unwrap();
        if f >= n_frames {
            continue;
        }
        frames.push((f as u64, o));
    }
    frames.sort();

    // Per-frame ranges
    let mut frame_ranges: Vec<(u64, Vec<Vec<(u64, u64)>>)> = Vec::new(); // f -> blocks
    let mut block_h = height;
    if compressed {
        for &(f, o) in &frames {
            let h = header(src, o)?;
            if h.d <= 8 {
                rej!("compressed frame {f}: data length {}", h.d);
            }
            let start = o as u128 + 16 + h.n as u128 + 8;
            if start > MAX_SAFE as u128 {
                rej!("frame {f}: offset above 2^53-1");
            }
            frame_ranges.push((f, vec![vec![(start as u64, h.d - 8)]]));
        }
    } else if !frames.is_empty() {
        let lo = frames[0];
        let hi = *frames.last().unwrap();
        let hl = header(src, lo.1)?;
        let hh = header(src, hi.1)?;
        if hl.n != hh.n {
            rej!("frames {} and {} have different name lengths", lo.0, hi.0);
        }
        let need = 8u128 + (height as u128) * (width_bytes as u128);
        if (hl.d as u128) < need || (hh.d as u128) < need {
            rej!("frame data shorter than {need} bytes");
        }
        let n = hl.n;
        let mut starts = Vec::new();
        for &(f, o) in &frames {
            let start = o as u128 + 16 + n as u128 + 8;
            let last_end = start + (height as u128 - 1) * width_bytes as u128 + r as u128;
            if last_end > size as u128 {
                rej!("frame {f} extends past the end of the file");
            }
            starts.push((f, start as u64));
        }
        if width_bytes == r {
            for &(f, s) in &starts {
                frame_ranges.push((f, vec![vec![(s, height * r)]]));
            }
        } else {
            // largest divisor h of height with every block payload <= 65519
            let mut divisors: Vec<u64> = Vec::new();
            let mut i = 1u64;
            while i * i <= height {
                if height % i == 0 {
                    divisors.push(i);
                    if i != height / i {
                        divisors.push(height / i);
                    }
                }
                i += 1;
            }
            divisors.sort_unstable_by(|a, b| b.cmp(a));
            let mut chosen = 1;
            'div: for &dv in &divisors {
                if dv == 1 {
                    break;
                }
                // quick lower bound: each element >= 1 + 1 + 2 + 2
                if dv * 6 > MAX_PAYLOAD {
                    continue;
                }
                for &(_, s) in &starts {
                    let mut row = 0;
                    while row < height {
                        let mut sum = 0u64;
                        for k in row..row + dv {
                            sum += concat_elem_len(s + k * width_bytes, r);
                        }
                        if sum > MAX_PAYLOAD {
                            continue 'div;
                        }
                        row += dv;
                    }
                }
                chosen = dv;
                break;
            }
            block_h = chosen;
            for &(f, s) in &starts {
                let mut blocks = Vec::new();
                for j in 0..height / chosen {
                    blocks.push((j * chosen..(j + 1) * chosen).map(|k| (s + k * width_bytes, r)).collect());
                }
                frame_ranges.push((f, blocks));
            }
        }
    }

    // Channels (§4.5)
    let mut channels: Vec<(String, String)> = Vec::new();
    let planes_ok = plane_count >= 1
        && (0..plane_count).all(|i| planes.get(&i).map(|p| p.comps == 1 || p.comps == 3).unwrap_or(false))
        && (0..plane_count).map(|i| planes[&i].comps as u128).sum::<u128>() == comp as u128;
    if planes_ok {
        for i in 0..plane_count {
            let p = &planes[&i];
            if p.comps == 1 {
                let c = p.color;
                channels.push((
                    p.desc.clone(),
                    format!("{:02X}{:02X}{:02X}", c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF),
                ));
            } else {
                for (suf, col) in [("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF")] {
                    channels.push((format!("{} {}", p.desc, suf), col.to_string()));
                }
            }
        }
    } else {
        for k in 0..comp {
            channels.push((format!("C{k}"), "FFFFFF".to_string()));
        }
    }
    let b = if bpc_sig.fract() == 0.0 && bpc_sig >= 1.0 && bpc_sig <= bpc as f64 { bpc_sig as u64 } else { bpc };
    let vmax = (1u64 << b) - 1;
    let omero = obj(vec![(
        "channels",
        J::Arr(
            channels
                .iter()
                .map(|(label, color)| {
                    let mut m = vec![("label", J::s(label)), ("color", J::s(color)), ("active", J::Bool(true))];
                    if bpc != 32 {
                        m.push((
                            "window",
                            obj(vec![
                                ("min", J::Int(0)),
                                ("max", J::UInt(vmax)),
                                ("start", J::Int(0)),
                                ("end", J::UInt(vmax)),
                            ]),
                        ));
                    }
                    obj(m)
                })
                .collect(),
        ),
    )]);

    // Output (§4.6)
    let time = loops.iter().position(|l| l.kind == Kind::Time);
    let pos = loops.iter().position(|l| l.kind == Kind::Position);
    let zl = loops.iter().position(|l| l.kind == Kind::Z);
    let n_pos = pos.map(|i| loops[i].count).unwrap_or(1);
    let has_c = comp > 1;
    let mut dims: Vec<&str> = Vec::new();
    let mut axes = Vec::new();
    let mut scale = Vec::new();
    let mut shape = Vec::new();
    let mut chunk = Vec::new();
    if let Some(i) = time {
        dims.push("t");
        let per = loops[i].param;
        if per > 0.0 {
            axes.push(Axis { name: "t", unit: Some("second") });
            scale.push(per / 1000.0);
        } else {
            axes.push(Axis { name: "t", unit: None });
            scale.push(1.0);
        }
        shape.push(loops[i].count);
        chunk.push(1);
    }
    if has_c {
        dims.push("c");
        axes.push(Axis { name: "c", unit: None });
        scale.push(1.0);
        shape.push(comp);
        chunk.push(comp);
    }
    if let Some(i) = zl {
        dims.push("z");
        let st = loops[i].param;
        if st > 0.0 {
            axes.push(Axis { name: "z", unit: Some("micrometer") });
            scale.push(st);
        } else {
            axes.push(Axis { name: "z", unit: None });
            scale.push(1.0);
        }
        shape.push(loops[i].count);
        chunk.push(1);
    }
    dims.push("y");
    dims.push("x");
    if let Some(c) = calibration {
        axes.push(Axis { name: "y", unit: Some("micrometer") });
        axes.push(Axis { name: "x", unit: Some("micrometer") });
        scale.push(check_finite(c * aspect, "y scale")?);
        scale.push(c);
    } else {
        axes.push(Axis { name: "y", unit: None });
        axes.push(Axis { name: "x", unit: None });
        scale.push(1.0);
        scale.push(1.0);
    }
    shape.push(height);
    shape.push(width);
    chunk.push(block_h);
    chunk.push(width);
    let mut codecs = Vec::new();
    if has_c {
        codecs.push(transpose_codec(&dims));
    }
    codecs.push(bytes_codec(bpc / 8, true));
    if compressed {
        codecs.push(zlib_codec());
    }

    out.json(
        "zarr.json",
        obj(vec![
            ("zarr_format", J::Int(3)),
            ("node_type", J::s("group")),
            (
                "attributes",
                obj(vec![("ome", obj(vec![("version", J::s("0.5")), ("bioformats2raw.layout", J::Int(3))]))]),
            ),
        ]),
    );
    out.json(
        "OME/zarr.json",
        obj(vec![
            ("zarr_format", J::Int(3)),
            ("node_type", J::s("group")),
            (
                "attributes",
                obj(vec![(
                    "ome",
                    obj(vec![
                        ("version", J::s("0.5")),
                        ("series", J::Arr((0..n_pos).map(|p| J::Str(p.to_string())).collect())),
                    ]),
                )]),
            ),
        ]),
    );
    let arr = array_json(&shape, dtype, &chunk, codecs, &dims);
    for p in 0..n_pos {
        out.json(
            format!("{p}/zarr.json"),
            image_group(Some(&format!("position {p}")), &axes, &[scale.clone()], Some(omero.clone())),
        );
        out.json(format!("{p}/0/zarr.json"), arr.clone());
    }
    for (f, blocks) in frame_ranges {
        // coordinates, last loop fastest
        let mut coords = vec![0u64; loops.len()];
        let mut rem = f;
        for i in (0..loops.len()).rev() {
            coords[i] = rem % loops[i].count;
            rem /= loops[i].count;
        }
        let p = pos.map(|i| coords[i]).unwrap_or(0);
        let mut prefix = format!("{p}/0/c");
        if let Some(i) = time {
            prefix.push_str(&format!("/{}", coords[i]));
        }
        if has_c {
            prefix.push_str("/0");
        }
        if let Some(i) = zl {
            prefix.push_str(&format!("/{}", coords[i]));
        }
        for (j, rs) in blocks.into_iter().enumerate() {
            out.ranges(format!("{prefix}/{j}/0"), rs)?;
        }
    }
    Ok(())
}
