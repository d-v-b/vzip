// §4 ND2 profile.

use crate::common::*;
use crate::http::Reader;
use crate::lv::{self, V};
use crate::rej;
use std::collections::HashMap;

const MAGIC: u32 = 0x0ABE_CEDA;

struct Hdr {
    n: u64,
    d: u64,
}

fn header(r: &mut Reader, o: u64) -> R<Hdr> {
    if o > MAX53 {
        rej!("chunk offset {} above 2^53-1", o);
    }
    let h = r.read(o, 16)?;
    let magic = u32::from_le_bytes(h[0..4].try_into().unwrap());
    if magic != MAGIC {
        rej!("no chunk magic at offset {}", o);
    }
    let n = u32::from_le_bytes(h[4..8].try_into().unwrap()) as u64;
    let d = u64::from_le_bytes(h[8..16].try_into().unwrap());
    if d > MAX53 {
        rej!("chunk data length above 2^53-1");
    }
    Ok(Hdr { n, d })
}

fn chunk_name(r: &mut Reader, o: u64, h: &Hdr) -> R<Vec<u8>> {
    r.read(o + 16, h.n)
}

fn chunk_data(r: &mut Reader, o: u64, h: &Hdr) -> R<Vec<u8>> {
    r.read(o + 16 + h.n, h.d)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Time,
    Position,
    Z,
}

struct Node {
    etype: u64,
    has_pars: bool,
    count: u64,
    param: f64, // period (ms) or z step
    children: Vec<Node>,
}

struct Loop {
    kind: Kind,
    depth: u64,
    count: u64,
    param: f64,
}

fn validity(list: Option<&V>) -> R<Option<Vec<bool>>> {
    match list {
        None => Ok(None),
        Some(v) => {
            let ms = lv::list_members(v)?;
            let mut out = Vec::with_capacity(ms.len());
            for m in &ms {
                out.push(lv::flag(m)?);
            }
            Ok(Some(out))
        }
    }
}

fn is_valid(v: &Option<Vec<bool>>, i: usize) -> bool {
    match v {
        None => true,
        Some(l) => l.get(i).copied().unwrap_or(false),
    }
}

fn obj_or_list(v: Option<&V>) -> R<Option<Vec<V>>> {
    match v {
        None => Ok(None),
        Some(v) => Ok(Some(lv::members(v)?)),
    }
}

fn check_node(v: &V) -> R<Node> {
    lv::object(v)?;
    let etype = lv::req(lv::opt_integer(v, "eType")?, "eType")?;
    if !matches!(etype, 1 | 2 | 4 | 6 | 8) {
        rej!("experiment eType {}", etype);
    }
    let pars = lv::member(v, "uLoopPars")?;
    if let Some(p) = pars {
        lv::object(p)?;
    }
    let item_valid = validity(lv::member(v, "pItemValid")?)?;
    let mut children = Vec::new();
    if let Some(ms) = obj_or_list(lv::member(v, "ppNextLevelEx")?)? {
        for m in &ms {
            lv::object(m)?;
            children.push(check_node(m)?);
        }
    }
    let mut count = 0u64;
    let mut param = 0.0f64;
    if let Some(p) = pars {
        match etype {
            1 => {
                count = lv::opt_integer(p, "uiCount")?.unwrap_or(0);
                param = lv::opt_number(p, "dPeriod")?.unwrap_or(0.0);
            }
            8 => {
                let valid = validity(lv::member(p, "pPeriodValid")?)?;
                let periods = obj_or_list(lv::member(p, "pPeriod")?)?.unwrap_or_default();
                let mut sum: u64 = 0;
                let mut first: Option<f64> = None;
                for (i, m) in periods.iter().enumerate() {
                    lv::object(m)?;
                    if !is_valid(&valid, i) {
                        continue;
                    }
                    let c = lv::req(lv::opt_integer(m, "uiCount")?, "pPeriod/uiCount")?;
                    let dp = lv::opt_number(m, "dPeriod")?.unwrap_or(0.0);
                    sum = sum.saturating_add(c);
                    if sum > MAX53 {
                        rej!("eType 8 count above 2^53-1");
                    }
                    if first.is_none() {
                        first = Some(dp);
                    }
                }
                count = sum;
                param = first.unwrap_or(0.0);
            }
            2 => {
                let pts = obj_or_list(lv::member(p, "Points")?)?.unwrap_or_default();
                count = (0..pts.len()).filter(|&i| is_valid(&item_valid, i)).count() as u64;
            }
            4 => {
                count = lv::opt_integer(p, "uiCount")?.unwrap_or(0);
                let step = lv::opt_number(p, "dZStep")?.unwrap_or(0.0);
                let low = lv::opt_number(p, "dZLow")?.unwrap_or(0.0);
                let high = lv::opt_number(p, "dZHigh")?.unwrap_or(0.0);
                param = step.abs();
                if param == 0.0 && count > 1 {
                    param = (high - low).abs() / (count - 1) as f64;
                    if !param.is_finite() {
                        rej!("z step is not finite");
                    }
                }
            }
            6 => {
                count = match lv::opt_integer(p, "uiCount")? {
                    Some(c) => c,
                    None => lv::opt_integer(p, "pPlanes/uiCount")?.unwrap_or(0),
                };
            }
            _ => unreachable!(),
        }
    }
    Ok(Node { etype, has_pars: pars.is_some(), count, param, children })
}

fn flatten(n: &Node, depth: u64, loops: &mut Vec<Loop>) {
    if !n.has_pars || n.count == 0 {
        return;
    }
    if n.etype == 6 {
        for c in &n.children {
            flatten(c, depth, loops);
        }
        return;
    }
    let kind = match n.etype {
        1 | 8 => Kind::Time,
        2 => Kind::Position,
        _ => Kind::Z,
    };
    let lp = Loop { kind, depth, count: n.count, param: n.param };
    match loops.last() {
        None => loops.push(lp),
        Some(l) if l.depth < depth => loops.push(lp),
        Some(l) if l.depth == depth && l.kind == kind && l.count < n.count => {
            *loops.last_mut().unwrap() = lp;
        }
        _ => {}
    }
    for c in &n.children {
        flatten(c, depth + 1, loops);
    }
}

fn read_lv_chunk(r: &mut Reader, map: &HashMap<Vec<u8>, u64>, name: &str) -> R<Option<V>> {
    let o = match map.get(name.as_bytes()) {
        None => return Ok(None),
        Some(&o) => o,
    };
    let h = header(r, o)?;
    let d = chunk_data(r, o, &h)?;
    Ok(Some(lv::parse_chunk(&d)?))
}

struct Plane {
    desc: String,
    color: u32,
    comps: u64,
}

pub fn virtualize(r: &mut Reader) -> R<Output> {
    let fsize = r.size;
    // §4.1 signature
    let h0 = header(r, 0)?;
    let name0 = chunk_name(r, 0, &h0)?;
    if h0.n != 32 || h0.d != 64 || name0 != b"ND2 FILE SIGNATURE CHUNK NAME01!" {
        rej!("bad signature chunk");
    }
    let sig = chunk_data(r, 0, &h0)?;
    if !sig.starts_with(b"Ver") {
        rej!("signature data does not start with Ver");
    }
    let digits: Vec<u8> = sig[3..].iter().take_while(|b| b.is_ascii_digit()).copied().collect();
    if digits.is_empty() || sig.get(3 + digits.len()) != Some(&b'.') {
        rej!("bad version string");
    }
    let s = String::from_utf8(digits).unwrap();
    let s = s.trim_start_matches('0');
    if s.len() < 2 && s.parse::<u64>().unwrap_or(0) < 3 {
        rej!("ND2 major version below 3");
    }

    // chunk map
    if fsize < 40 {
        rej!("file shorter than 40 bytes");
    }
    let tail = r.read(fsize - 40, 40)?;
    if &tail[..32] != b"ND2 CHUNK MAP SIGNATURE 0000001!" {
        rej!("no chunk map signature");
    }
    let m = u64::from_le_bytes(tail[32..40].try_into().unwrap());
    let hm = header(r, m)?;
    let mname = chunk_name(r, m, &hm)?;
    let mname: &[u8] = match mname.iter().position(|&b| b == 0) {
        Some(p) => &mname[..p],
        None => &mname,
    };
    if mname != b"ND2 FILEMAP SIGNATURE NAME 0001!" {
        rej!("chunk map chunk has the wrong name");
    }
    let md = chunk_data(r, m, &hm)?;
    let mut map: HashMap<Vec<u8>, u64> = HashMap::new();
    let mut names: Vec<Vec<u8>> = Vec::new();
    let mut p = 0usize;
    loop {
        let bang = match md[p..].iter().position(|&b| b == b'!') {
            Some(i) => p + i,
            None => rej!("chunk map data without its last record"),
        };
        let name = md[p..=bang].to_vec();
        if name == b"ND2 CHUNK MAP SIGNATURE 0000001!" {
            break;
        }
        if bang + 1 + 16 > md.len() {
            rej!("chunk map record runs past the end of the data");
        }
        let off = u64::from_le_bytes(md[bang + 1..bang + 9].try_into().unwrap());
        if !map.contains_key(&name) {
            names.push(name.clone());
        }
        map.insert(name, off);
        p = bang + 17;
    }

    // §4.3 attributes
    let attrs_chunk = match read_lv_chunk(r, &map, "ImageAttributesLV!")? {
        Some(v) => v,
        None => rej!("no ImageAttributesLV! chunk"),
    };
    let a = lv::req(lv::member(&attrs_chunk, "SLxImageAttributes")?, "SLxImageAttributes")?;
    lv::object(a)?;
    let width = lv::req(lv::opt_integer(a, "uiWidth")?, "uiWidth")?;
    let height = lv::req(lv::opt_integer(a, "uiHeight")?, "uiHeight")?;
    let width_bytes = lv::req(lv::opt_integer(a, "uiWidthBytes")?, "uiWidthBytes")?;
    let comp = lv::req(lv::opt_integer(a, "uiComp")?, "uiComp")?;
    let bpc = lv::req(lv::opt_integer(a, "uiBpcInMemory")?, "uiBpcInMemory")?;
    let bpc_sig = lv::req(lv::opt_number(a, "uiBpcSignificant")?, "uiBpcSignificant")?;
    let ecomp = lv::opt_integer(a, "eCompression")?.unwrap_or(2);
    let tile_w = lv::opt_integer(a, "uiTileWidth")?.unwrap_or(0);
    let tile_h = lv::opt_integer(a, "uiTileHeight")?.unwrap_or(0);
    if width < 1 || height < 1 || comp < 1 {
        rej!("uiWidth, uiHeight or uiComp is 0");
    }
    let dtype = match bpc {
        8 => "uint8",
        16 => "uint16",
        32 => "float32",
        _ => rej!("uiBpcInMemory {}", bpc),
    };
    let compressed = match ecomp {
        2 => false,
        0 => true,
        _ => rej!("eCompression {}", ecomp),
    };
    if (tile_w > 0 && tile_w != width) || (tile_h > 0 && tile_h != height) {
        rej!("tiled image");
    }

    // experiment
    let mut loops: Vec<Loop> = Vec::new();
    if let Some(mc) = read_lv_chunk(r, &map, "ImageMetadataLV!")? {
        if let Some(root) = lv::member(&mc, "SLxExperiment")? {
            if std::env::var("VZ_DEBUG").is_ok() {
                eprintln!("{:#?}", root);
            }
            let tree = check_node(root)?;
            flatten(&tree, 0, &mut loops);
        }
    }
    if std::env::var("VZ_DEBUG").is_ok() {
        for l in &loops {
            eprintln!("loop {:?} depth {} count {} param {}", l.kind, l.depth, l.count, l.param);
        }
    }
    for i in 0..loops.len() {
        for j in 0..i {
            if loops[i].kind == loops[j].kind {
                rej!("two {:?} loops", loops[i].kind);
            }
        }
    }

    // picture metadata
    let mut calibrated = false;
    let mut cal = 0.0;
    let mut aspect = 1.0;
    let mut plane_count = 0u64;
    let mut planes: HashMap<u64, Plane> = HashMap::new();
    if let Some(pc) = read_lv_chunk(r, &map, "ImageMetadataSeqLV|0!")? {
        if let Some(pm) = lv::member(&pc, "SLxPictureMetadata")? {
            lv::object(pm)?;
            let bcal = lv::opt_flag(pm, "bCalibrated")?.unwrap_or(false);
            let dcal = lv::opt_number(pm, "dCalibration")?;
            let asp = lv::opt_number(pm, "dAspect")?.unwrap_or(1.0);
            if let Some(sp) = lv::member(pm, "sPicturePlanes")? {
                lv::object(sp)?;
                plane_count = lv::opt_integer(sp, "uiCount")?.unwrap_or(0);
                if let Some(spn) = lv::member(sp, "sPlaneNew")? {
                    lv::object(spn)?;
                    if let V::Obj(ms) = spn {
                        for (n, v) in ms {
                            let idx = match n.strip_prefix('a') {
                                Some(d) if !d.is_empty()
                                    && d.bytes().all(|b| b.is_ascii_digit())
                                    && (d == "0" || !d.starts_with('0'))
                                    && d.len() <= 16 =>
                                {
                                    d.parse::<u64>().unwrap()
                                }
                                _ => continue,
                            };
                            if idx >= plane_count {
                                continue;
                            }
                            lv::object(v)?;
                            let desc = match lv::member(v, "sDescription")? {
                                Some(s) => lv::string(s)?,
                                None => String::new(),
                            };
                            let color = match lv::member(v, "uiColor")? {
                                Some(c) => lv::color(c)?,
                                None => 0xFFFFFF,
                            };
                            let comps = lv::opt_integer(v, "uiCompCount")?.unwrap_or(1);
                            planes.insert(idx, Plane { desc, color, comps });
                        }
                    }
                }
            }
            if let Some(dc) = dcal {
                if bcal && dc > 0.0 {
                    calibrated = true;
                    cal = dc;
                }
            }
            aspect = if asp > 0.0 { asp } else { 1.0 };
        }
    }

    // §4.4 frames
    let mut nframes: u128 = 1;
    for l in &loops {
        nframes *= l.count as u128;
        if nframes > MAX53 as u128 {
            rej!("frame count above 2^53-1");
        }
    }
    let nframes = nframes as u64;
    let rlen = width as u128 * comp as u128 * (bpc / 8) as u128;
    if (width_bytes as u128) < rlen {
        rej!("uiWidthBytes below the row length");
    }
    let rlen = rlen as u64;
    let mut frames: Vec<(u64, u64)> = Vec::new(); // (f, chunk offset)
    for n in &names {
        if let Some(rest) = n.strip_prefix(b"ImageDataSeq|".as_slice()) {
            if let Some(num) = rest.strip_suffix(b"!".as_slice()) {
                if num.is_empty() || !num.iter().all(|b| b.is_ascii_digit()) || (num.len() > 1 && num[0] == b'0') {
                    continue;
                }
                if num.len() > 16 {
                    continue; // >= 10^16 > 2^53 > N
                }
                let f: u64 = std::str::from_utf8(num).unwrap().parse().unwrap();
                if f < nframes {
                    frames.push((f, map[n]));
                }
            }
        }
    }
    frames.sort();

    // per frame: list of chunks (block index, ranges)
    let mut chunk_h = height;
    let mut frame_chunks: Vec<(u64, Vec<(u64, Vec<(u64, u64)>)>)> = Vec::new();
    let frame_bytes = height as u128 * width_bytes as u128;
    if !compressed {
        let mut n0 = 0u64;
        if let (Some(&(_, lo)), Some(&(_, hi))) = (frames.first(), frames.last()) {
            let hl = header(r, lo)?;
            let hh = header(r, hi)?;
            if hl.n != hh.n {
                rej!("lowest and highest frames have different name lengths");
            }
            if (hl.d as u128) < 8 + frame_bytes || (hh.d as u128) < 8 + frame_bytes {
                rej!("frame data shorter than the image");
            }
            n0 = hl.n;
        }
        let mut starts = Vec::new();
        for &(f, o) in &frames {
            let start = o as u128 + 16 + n0 as u128 + 8;
            if start > MAX53 as u128 {
                rej!("frame offset above 2^53-1");
            }
            starts.push((f, start as u64));
        }
        if width_bytes == rlen {
            let len = height as u128 * rlen as u128;
            if len > MAX53 as u128 {
                rej!("frame length above 2^53-1");
            }
            for (f, s) in starts {
                frame_chunks.push((f, vec![(0, vec![(s, len as u64)])]));
            }
        } else {
            // Row blocks.
            let max_start = starts.iter().map(|&(_, s)| s).max();
            let row_off = |s: u64, row: u64| -> u128 { s as u128 + row as u128 * width_bytes as u128 };
            let block_payload = |s: u64, j: u64, h: u64| -> u128 {
                let mut tot: u128 = 0;
                if h == 1 {
                    let o = row_off(s, j);
                    return range_msg_len(o.min(u64::MAX as u128) as u64, rlen) as u128;
                }
                for row in j * h..j * h + h {
                    let o = row_off(s, row).min(u64::MAX as u128) as u64;
                    let rm = range_msg_len(o, rlen) as u128;
                    tot += 1 + if rm < 128 { 1 } else { 2 } + rm;
                    if tot > 65519 {
                        return tot;
                    }
                }
                tot
            };
            let mut h = 1;
            match max_start {
                None => h = height,
                Some(ms) => {
                    // Payload grows with the offsets, so the last block of the
                    // frame with the highest start is the largest.
                    let lim = height.min(65519);
                    let mut cand = lim;
                    while cand >= 1 {
                        if height % cand == 0 {
                            let jl = height / cand - 1;
                            if block_payload(ms, jl, cand) <= 65519 {
                                h = cand;
                                break;
                            }
                        }
                        cand -= 1;
                    }
                }
            }
            chunk_h = h;
            for (f, s) in starts {
                let mut blocks = Vec::new();
                for j in 0..height / h {
                    let mut rs = Vec::with_capacity(h as usize);
                    for row in j * h..j * h + h {
                        let o = row_off(s, row);
                        if o > MAX53 as u128 {
                            rej!("row offset above 2^53-1");
                        }
                        rs.push((o as u64, rlen));
                    }
                    blocks.push((j, rs));
                }
                frame_chunks.push((f, blocks));
            }
        }
    } else {
        if width_bytes != rlen {
            rej!("compressed frames with padded rows");
        }
        for &(f, o) in &frames {
            let hd = header(r, o)?;
            if hd.d <= 8 {
                rej!("compressed frame {} with data length {}", f, hd.d);
            }
            let start = o as u128 + 16 + hd.n as u128 + 8;
            if start > MAX53 as u128 {
                rej!("frame offset above 2^53-1");
            }
            frame_chunks.push((f, vec![(0, vec![(start as u64, hd.d - 8)])]));
        }
    }

    // §4.5 channels
    let mut chans: Vec<(String, String)> = Vec::new();
    let mut labeled = false;
    if plane_count >= 1 && (0..plane_count).all(|i| planes.contains_key(&i)) {
        let ok = (0..plane_count).all(|i| matches!(planes[&i].comps, 1 | 3))
            && (0..plane_count).map(|i| planes[&i].comps as u128).sum::<u128>() == comp as u128;
        if ok {
            labeled = true;
            for i in 0..plane_count {
                let p = &planes[&i];
                if p.comps == 1 {
                    let c = p.color;
                    let hex = format!("{:02X}{:02X}{:02X}", c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF);
                    chans.push((p.desc.clone(), hex));
                } else {
                    chans.push((format!("{} R", p.desc), "FF0000".into()));
                    chans.push((format!("{} G", p.desc), "00FF00".into()));
                    chans.push((format!("{} B", p.desc), "0000FF".into()));
                }
            }
        }
    }
    if !labeled {
        if comp > 10_000_000 {
            // The specification accepts this, but the output cannot be built.
            return Err(E::Fail(format!("uiComp {} is too large to write one omero channel each", comp)));
        }
        for k in 0..comp {
            chans.push((format!("C{}", k), "FFFFFF".into()));
        }
    }
    let window_bits = if bpc_sig.fract() == 0.0 && bpc_sig >= 1.0 && bpc_sig <= bpc as f64 {
        bpc_sig as u64
    } else {
        bpc
    };
    let omero_channels: Vec<Json> = chans
        .iter()
        .map(|(l, c)| {
            let mut m = vec![("label", Json::s(l)), ("color", Json::s(c)), ("active", Json::Bool(true))];
            if dtype != "float32" {
                let v = (1u64 << window_bits) - 1;
                m.push((
                    "window",
                    Json::obj(vec![
                        ("min", Json::i(0)),
                        ("max", Json::i(v)),
                        ("start", Json::i(0)),
                        ("end", Json::i(v)),
                    ]),
                ));
            }
            Json::obj(m)
        })
        .collect();
    let omero = Json::obj(vec![("channels", Json::Arr(omero_channels))]);

    // §4.6 output
    let lt = loops.iter().position(|l| l.kind == Kind::Time);
    let lp = loops.iter().position(|l| l.kind == Kind::Position);
    let lz = loops.iter().position(|l| l.kind == Kind::Z);
    let npos = lp.map(|i| loops[i].count).unwrap_or(1);
    let mut dims: Vec<&'static str> = Vec::new();
    let mut shape = Vec::new();
    let mut chunk = Vec::new();
    let mut axes: Vec<Axis> = Vec::new();
    if let Some(i) = lt {
        dims.push("t");
        shape.push(loops[i].count);
        chunk.push(1);
        let per = loops[i].param;
        let (u, s) = if per > 0.0 { (Some("second"), per / 1000.0) } else { (None, 1.0) };
        axes.push(Axis { name: "t", unit: u, scales: vec![s] });
    }
    if comp > 1 {
        dims.push("c");
        shape.push(comp);
        chunk.push(comp);
        axes.push(Axis { name: "c", unit: None, scales: vec![1.0] });
    }
    if let Some(i) = lz {
        dims.push("z");
        shape.push(loops[i].count);
        chunk.push(1);
        let st = loops[i].param;
        let (u, s) = if st > 0.0 { (Some("micrometer"), st) } else { (None, 1.0) };
        axes.push(Axis { name: "z", unit: u, scales: vec![s] });
    }
    dims.extend(["y", "x"]);
    shape.extend([height, width]);
    chunk.extend([chunk_h, width]);
    if calibrated {
        axes.push(Axis { name: "y", unit: Some("micrometer"), scales: vec![cal * aspect] });
        axes.push(Axis { name: "x", unit: Some("micrometer"), scales: vec![cal] });
    } else {
        axes.push(Axis { name: "y", unit: None, scales: vec![1.0] });
        axes.push(Axis { name: "x", unit: None, scales: vec![1.0] });
    }
    let dim_strings: Vec<String> = dims.iter().map(|d| d.to_string()).collect();
    let mut codecs = Vec::new();
    if comp > 1 {
        codecs.push(transpose_codec(&dim_strings));
    }
    codecs.push(bytes_codec(bpc / 8, true));
    if compressed {
        codecs.push(zlib_codec());
    }
    let meta = ArrayMeta { shape, data_type: dtype.into(), chunk_shape: chunk, codecs, dims: dim_strings };
    let array = array_json(&meta);

    let mut out = Output { entries: Vec::new() };
    out.entries.push((
        "zarr.json".into(),
        Entry::Json(Json::obj(vec![
            ("zarr_format", Json::i(3)),
            ("node_type", Json::s("group")),
            (
                "attributes",
                Json::obj(vec![(
                    "ome",
                    Json::obj(vec![("version", Json::s("0.5")), ("bioformats2raw.layout", Json::i(3))]),
                )]),
            ),
        ])),
    ));
    out.entries.push((
        "OME/zarr.json".into(),
        Entry::Json(Json::obj(vec![
            ("zarr_format", Json::i(3)),
            ("node_type", Json::s("group")),
            (
                "attributes",
                Json::obj(vec![(
                    "ome",
                    Json::obj(vec![
                        ("version", Json::s("0.5")),
                        ("series", Json::Arr((0..npos).map(|p| Json::Str(p.to_string())).collect())),
                    ]),
                )]),
            ),
        ])),
    ));
    for p in 0..npos {
        let img = image_json(Some(&format!("position {}", p)), &axes, 1, Some(omero.clone()))?;
        out.entries.push((format!("{}/zarr.json", p), Entry::Json(img)));
        out.entries.push((format!("{}/0/zarr.json", p), Entry::Json(array.clone())));
    }
    for (f, blocks) in frame_chunks {
        // coordinates over the loops, last fastest
        let mut idx = vec![0u64; loops.len()];
        let mut rem = f;
        for i in (0..loops.len()).rev() {
            idx[i] = rem % loops[i].count;
            rem /= loops[i].count;
        }
        let p = lp.map(|i| idx[i]).unwrap_or(0);
        let mut prefix = format!("{}/0/c", p);
        if let Some(i) = lt {
            prefix.push_str(&format!("/{}", idx[i]));
        }
        if comp > 1 {
            prefix.push_str("/0");
        }
        if let Some(i) = lz {
            prefix.push_str(&format!("/{}", idx[i]));
        }
        for (j, rs) in blocks {
            out.entries.push((format!("{}/{}/0", prefix, j), Entry::Ranges(rs)));
        }
    }
    check_output(&out, fsize)?;
    Ok(out)
}
