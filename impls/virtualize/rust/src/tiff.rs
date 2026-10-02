//! TIFF profile (§3).
use crate::common::*;
use crate::http::Source;
use crate::json::J;
use crate::rej;
use crate::xml;
use std::collections::{HashMap, HashSet};

const T_WIDTH: u16 = 256;
const T_LENGTH: u16 = 257;
const T_BPS: u16 = 258;
const T_COMPRESSION: u16 = 259;
const T_DESC: u16 = 270;
const T_SPP: u16 = 277;
const T_PLANAR: u16 = 284;
const T_PREDICTOR: u16 = 317;
const T_TW: u16 = 322;
const T_TL: u16 = 323;
const T_TOFF: u16 = 324;
const T_TBC: u16 = 325;
const T_SUBIFDS: u16 = 330;
const T_SF: u16 = 339;

const SCALARS: [u16; 8] = [T_WIDTH, T_LENGTH, T_COMPRESSION, T_SPP, T_PLANAR, T_PREDICTOR, T_TW, T_TL];
const ARRAYS: [u16; 5] = [T_BPS, T_TOFF, T_TBC, T_SUBIFDS, T_SF];

const MAX_IFDS: usize = 100000;
const MAX_PLANES: u64 = 100000;

fn type_size(t: u16) -> Option<u64> {
    Some(match t {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 | 16 | 17 | 18 => 8,
        _ => return None,
    })
}

struct TagEnt {
    typ: u16,
    count: u64,
    data_off: u64, // absolute file offset of the value bytes
}

struct Ifd {
    tags: HashMap<u16, TagEnt>,
    scalars: HashMap<u16, u64>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Format {
    bps: u64,
    spp: u64,
    sf: u64,
    planar: u64,
    compression: u64,
    predictor: u64,
}

struct Size {
    w: u64,
    h: u64,
    tile: Option<(u64, u64)>, // (tile width, tile length)
}

struct Tiff<'a> {
    src: &'a mut Source,
    le: bool,
    big: bool,
    ifds: Vec<Ifd>,
}

fn rd(b: &[u8], le: bool) -> u64 {
    let mut v = 0u64;
    if le {
        for (i, &x) in b.iter().enumerate() {
            v |= (x as u64) << (8 * i);
        }
    } else {
        for &x in b {
            v = (v << 8) | x as u64;
        }
    }
    v
}

impl<'a> Tiff<'a> {
    fn read_ifd(&mut self, off: u64) -> Res<(Ifd, u64)> {
        let (cnt_size, ent_size, off_size) = if self.big { (8u64, 20u64, 8u64) } else { (2, 12, 4) };
        let n = rd(&self.src.read(off, cnt_size)?, self.le);
        let total = (n as u128) * (ent_size as u128);
        if total > MAX_SAFE as u128 {
            rej!("IFD at {off}: entry count too large");
        }
        let ents = self.src.read(off + cnt_size, total as u64)?;
        let next_pos = off + cnt_size + total as u64;
        let next = rd(&self.src.read(next_pos, off_size)?, self.le);
        let mut ifd = Ifd { tags: HashMap::new(), scalars: HashMap::new() };
        for i in 0..n as usize {
            let e = &ents[i * ent_size as usize..(i + 1) * ent_size as usize];
            let tag = rd(&e[0..2], self.le) as u16;
            let is_scalar = SCALARS.contains(&tag);
            if !(is_scalar || ARRAYS.contains(&tag) || tag == T_DESC) {
                continue;
            }
            if ifd.tags.contains_key(&tag) {
                continue; // duplicate: first is used, others ignored
            }
            let typ = rd(&e[2..4], self.le) as u16;
            let ok = if tag == T_DESC {
                matches!(typ, 1..=13 | 16..=18)
            } else {
                matches!(typ, 1 | 3 | 4 | 13 | 16 | 18)
            };
            if !ok {
                rej!("IFD at {off}: tag {tag} has field type {typ}");
            }
            let sz = type_size(typ).unwrap();
            let (count, valfield, valpos) = if self.big {
                (rd(&e[4..12], self.le), &e[12..20], 12u64)
            } else {
                (rd(&e[4..8], self.le), &e[8..12], 8u64)
            };
            let bytes = (count as u128) * (sz as u128);
            let data_off = if bytes <= off_size as u128 {
                off + cnt_size + (i as u64) * ent_size + valpos
            } else {
                let o = rd(valfield, self.le);
                if o > MAX_SAFE || bytes > MAX_SAFE as u128 {
                    rej!("IFD at {off}: tag {tag} value offset/length above 2^53-1");
                }
                if (o as u128) + bytes > self.src.size as u128 {
                    rej!("IFD at {off}: tag {tag} value outside the file");
                }
                o
            };
            if is_scalar && count == 0 {
                rej!("IFD at {off}: scalar tag {tag} has no values");
            }
            let ent = TagEnt { typ, count, data_off };
            if is_scalar {
                let b = self.src.read(data_off, sz)?;
                ifd.scalars.insert(tag, rd(&b, self.le));
            }
            ifd.tags.insert(tag, ent);
        }
        Ok((ifd, next))
    }

    fn values(&mut self, ifd: usize, tag: u16) -> Res<Option<Vec<u64>>> {
        let (typ, count, off) = match self.ifds[ifd].tags.get(&tag) {
            Some(e) => (e.typ, e.count, e.data_off),
            None => return Ok(None),
        };
        let sz = type_size(typ).unwrap();
        let b = self.src.read(off, count * sz)?;
        Ok(Some(b.chunks(sz as usize).map(|c| rd(c, self.le)).collect()))
    }

    fn scalar(&self, ifd: usize, tag: u16) -> Option<u64> {
        self.ifds[ifd].scalars.get(&tag).copied()
    }

    fn has(&self, ifd: usize, tag: u16) -> bool {
        self.ifds[ifd].tags.contains_key(&tag)
    }

    fn format(&mut self, ifd: usize) -> Res<Format> {
        let bps = match self.values(ifd, T_BPS)? {
            None => rej!("IFD {ifd}: BitsPerSample missing"),
            Some(v) if v.is_empty() => rej!("IFD {ifd}: BitsPerSample has no values"),
            Some(v) => {
                if v.iter().any(|&x| x != v[0]) {
                    rej!("IFD {ifd}: BitsPerSample values differ");
                }
                if v[0] < 1 {
                    rej!("IFD {ifd}: BitsPerSample 0");
                }
                v[0]
            }
        };
        let spp = self.scalar(ifd, T_SPP).unwrap_or(1);
        if spp < 1 {
            rej!("IFD {ifd}: SamplesPerPixel 0");
        }
        let sf = match self.values(ifd, T_SF)? {
            None => 1,
            Some(v) if v.is_empty() => rej!("IFD {ifd}: SampleFormat has no values"),
            Some(v) => {
                if v.iter().any(|&x| x != v[0]) {
                    rej!("IFD {ifd}: SampleFormat values differ");
                }
                v[0]
            }
        };
        let planar = if spp == 1 {
            1
        } else {
            let p = self.scalar(ifd, T_PLANAR).unwrap_or(1);
            if p != 1 && p != 2 {
                rej!("IFD {ifd}: PlanarConfiguration {p}");
            }
            p
        };
        Ok(Format {
            bps,
            spp,
            sf,
            planar,
            compression: self.scalar(ifd, T_COMPRESSION).unwrap_or(1),
            predictor: self.scalar(ifd, T_PREDICTOR).unwrap_or(1),
        })
    }

    fn is_tiled(&self, ifd: usize) -> bool {
        self.has(ifd, T_TW) && self.has(ifd, T_TOFF)
    }

    fn size(&self, ifd: usize) -> Res<Size> {
        let w = self.scalar(ifd, T_WIDTH);
        let h = self.scalar(ifd, T_LENGTH);
        let (w, h) = match (w, h) {
            (Some(w), Some(h)) if w >= 1 && h >= 1 => (w, h),
            _ => rej!("IFD {ifd}: missing or zero ImageWidth/ImageLength"),
        };
        let tile = if self.is_tiled(ifd) {
            let tw = self.scalar(ifd, T_TW).unwrap();
            let tl = match self.scalar(ifd, T_TL) {
                Some(v) => v,
                None => rej!("IFD {ifd}: TileLength missing"),
            };
            if !self.has(ifd, T_TBC) {
                rej!("IFD {ifd}: TileByteCounts missing");
            }
            if tw < 1 || tl < 1 {
                rej!("IFD {ifd}: zero tile size");
            }
            Some((tw, tl))
        } else {
            None
        };
        Ok(Size { w, h, tile })
    }
}

#[derive(Default)]
struct TiffData {
    ifd: Option<u64>,
    first: [Option<u64>; 3], // Z, C, T
    plane_count: Option<u64>,
}

struct Ome {
    d: Vec<u8>,
    image_name: Option<String>,
    size_z: Option<u64>,
    size_c: Option<u64>,
    size_t: Option<u64>,
    dim_order: Option<[u8; 3]>,
    phys: [Option<f64>; 3],         // X, Y, Z
    phys_unit: [Option<String>; 3], // X, Y, Z
    tiffdata: Vec<TiffData>,
    files: HashSet<String>,
}

fn parse_int_attr(v: &str, what: &str, min1: bool) -> Res<u64> {
    let t = xml::trim_ws(v);
    if t.is_empty() || !t.bytes().all(|c| c.is_ascii_digit()) {
        rej!("OME-XML: {what}={v:?} is not an integer");
    }
    let t = t.trim_start_matches('0');
    if t.len() > 16 {
        rej!("OME-XML: {what}={v:?} above 2^53-1");
    }
    let n: u64 = if t.is_empty() { 0 } else { t.parse().unwrap() };
    if n > MAX_SAFE {
        rej!("OME-XML: {what}={v:?} above 2^53-1");
    }
    if min1 && n < 1 {
        rej!("OME-XML: {what}={v:?} must be at least 1");
    }
    Ok(n)
}

fn parse_phys(v: &str) -> Option<f64> {
    let b = v.as_bytes();
    let mut p = 0;
    if p < b.len() && (b[p] == b'+' || b[p] == b'-') {
        p += 1;
    }
    let int_start = p;
    while p < b.len() && b[p].is_ascii_digit() {
        p += 1;
    }
    let int_digits = p - int_start;
    let mut frac_digits = 0;
    if p < b.len() && b[p] == b'.' {
        p += 1;
        let fs = p;
        while p < b.len() && b[p].is_ascii_digit() {
            p += 1;
        }
        frac_digits = p - fs;
    }
    if int_digits == 0 && frac_digits == 0 {
        return None;
    }
    if p < b.len() && (b[p] == b'e' || b[p] == b'E') {
        p += 1;
        if p < b.len() && (b[p] == b'+' || b[p] == b'-') {
            p += 1;
        }
        let es = p;
        while p < b.len() && b[p].is_ascii_digit() {
            p += 1;
        }
        if p == es {
            return None;
        }
    }
    if p != b.len() {
        return None;
    }
    let f: f64 = v.parse().ok()?;
    if f.is_finite() && f > 0.0 { Some(f) } else { None }
}

fn unit_of(sym: &str) -> Option<&'static str> {
    Some(match sym {
        "\u{b5}m" | "\u{3bc}m" | "um" => "micrometer",
        "nm" => "nanometer",
        "mm" => "millimeter",
        "cm" => "centimeter",
        "m" => "meter",
        "\u{c5}" | "\u{212b}" => "angstrom",
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

fn parse_ome(d: Vec<u8>) -> Res<Option<Ome>> {
    let x = match std::str::from_utf8(&d) {
        Ok(s) => s.to_string(),
        Err(_) => return Ok(None),
    };
    let xb = x.as_bytes();
    let sc = xml::scan(xb);
    let tags = &sc.tags;
    if !tags.iter().any(|t| t.is_start("OME")) {
        return Ok(None);
    }
    let image_name = tags.iter().find(|t| t.is_start("Image")).and_then(|t| t.attr("Name"));
    let mut ome = Ome {
        d: Vec::new(),
        image_name,
        size_z: None,
        size_c: None,
        size_t: None,
        dim_order: None,
        phys: [None; 3],
        phys_unit: [None, None, None],
        tiffdata: Vec::new(),
        files: HashSet::new(),
    };
    if let Some(pi) = tags.iter().position(|t| t.is_start("Pixels")) {
        let px = &tags[pi];
        if let Some(v) = px.attr("SizeZ") {
            ome.size_z = Some(parse_int_attr(&v, "SizeZ", true)?);
        }
        if let Some(v) = px.attr("SizeC") {
            ome.size_c = Some(parse_int_attr(&v, "SizeC", true)?);
        }
        if let Some(v) = px.attr("SizeT") {
            ome.size_t = Some(parse_int_attr(&v, "SizeT", true)?);
        }
        if let Some(v) = px.attr("DimensionOrder") {
            let b = v.as_bytes();
            let ok = b.len() == 5 && &b[..2] == b"XY" && {
                let mut s = [b[2], b[3], b[4]];
                s.sort();
                &s == b"CTZ"
            };
            if !ok {
                rej!("OME-XML: DimensionOrder {v:?}");
            }
            ome.dim_order = Some([b[2], b[3], b[4]]);
        }
        for (i, a) in ["X", "Y", "Z"].iter().enumerate() {
            ome.phys[i] = px.attr(&format!("PhysicalSize{a}")).and_then(|v| parse_phys(&v));
            ome.phys_unit[i] = px.attr(&format!("PhysicalSize{a}Unit"));
        }
        if !px.self_closing {
            let end = tags[pi + 1..]
                .iter()
                .position(|t| t.is_end_of("Pixels"))
                .map(|p| p + pi + 1)
                .unwrap_or(tags.len());
            let tds: Vec<usize> = (pi + 1..end).filter(|&i| tags[i].is_start("TiffData")).collect();
            for &ti in &tds {
                let t = &tags[ti];
                let mut td = TiffData::default();
                if let Some(v) = t.attr("IFD") {
                    td.ifd = Some(parse_int_attr(&v, "IFD", false)?);
                }
                for (k, a) in ["FirstZ", "FirstC", "FirstT"].iter().enumerate() {
                    if let Some(v) = t.attr(a) {
                        td.first[k] = Some(parse_int_attr(&v, a, false)?);
                    }
                }
                if let Some(v) = t.attr("PlaneCount") {
                    td.plane_count = Some(parse_int_attr(&v, "PlaneCount", true)?);
                }
                if !t.self_closing {
                    // first UUID start tag before the next TiffData end/start or Pixels end
                    let mut j = ti + 1;
                    while j < tags.len() {
                        let u = &tags[j];
                        if u.is_end_of("TiffData") || u.is_start("TiffData") || u.is_end_of("Pixels") {
                            break;
                        }
                        if u.is_start("UUID") {
                            let id = match u.attr("FileName") {
                                Some(f) => f,
                                None => {
                                    if u.self_closing {
                                        String::new()
                                    } else {
                                        let s = u.end;
                                        let e = tags.get(j + 1).map(|t| t.start).unwrap_or(xb.len());
                                        let mut raw = String::new();
                                        let mut p = s;
                                        for &(a, b) in &sc.skipped {
                                            if b <= s || a >= e {
                                                continue;
                                            }
                                            raw.push_str(&x[p..a.max(p)]);
                                            p = b.min(e);
                                        }
                                        raw.push_str(&x[p.min(e)..e]);
                                        xml::trim_ws(&xml::decode_refs(&raw)).to_string()
                                    }
                                }
                            };
                            ome.files.insert(id);
                            break;
                        }
                        j += 1;
                    }
                }
                ome.tiffdata.push(td);
            }
        }
    }
    ome.d = d;
    Ok(Some(ome))
}

struct Level {
    ifds: Vec<usize>, // per plane (canonical plane order)
    w: u64,
    h: u64,
    tw: u64,
    tl: u64,
}

pub fn virtualize(src: &mut Source, out: &mut Output) -> Res<()> {
    let hdr = src.read(0, 8)?;
    let le = hdr[0] == b'I';
    let magic = rd(&hdr[2..4], le);
    let (big, first) = if magic == 42 {
        (false, rd(&hdr[4..8], le))
    } else {
        let h = src.read(0, 16)?;
        if rd(&h[4..6], le) != 8 || rd(&h[6..8], le) != 0 {
            rej!("BigTIFF: offset size must be 8 and reserved word 0");
        }
        (true, rd(&h[8..16], le))
    };
    let min_off = if big { 16 } else { 8 };
    let mut t = Tiff { src, le, big, ifds: Vec::new() };
    let mut seen: HashSet<u64> = HashSet::new();
    let mut check_off = |o: u64, n_read: usize| -> Res<()> {
        if o < min_off {
            rej!("IFD offset {o} below {min_off}");
        }
        if o > MAX_SAFE {
            rej!("IFD offset {o} above 2^53-1");
        }
        if !seen.insert(o) {
            rej!("IFD offset {o} read twice");
        }
        if n_read >= MAX_IFDS {
            rej!("more than {MAX_IFDS} IFDs");
        }
        Ok(())
    };
    // main chain
    let mut main: Vec<usize> = Vec::new();
    let mut off = first;
    while off != 0 {
        check_off(off, t.ifds.len())?;
        let (ifd, next) = t.read_ifd(off)?;
        t.ifds.push(ifd);
        main.push(t.ifds.len() - 1);
        off = next;
    }
    if main.is_empty() {
        rej!("TIFF has no IFDs");
    }
    // SubIFDs
    let mut subs: Vec<Vec<usize>> = Vec::new();
    for mi in 0..main.len() {
        let mut v = Vec::new();
        if let Some(offs) = t.values(main[mi], T_SUBIFDS)? {
            for o in offs {
                check_off(o, t.ifds.len())?;
                let (ifd, _) = t.read_ifd(o)?;
                t.ifds.push(ifd);
                v.push(t.ifds.len() - 1);
            }
        }
        subs.push(v);
    }
    let ifd0 = main[0];
    let fmt0 = t.format(ifd0)?;
    let spp = fmt0.spp;

    // OME-XML
    let mut ome: Option<Ome> = None;
    if let Some(e) = t.ifds[ifd0].tags.get(&T_DESC) {
        if e.typ == 2 {
            let (o, n) = (e.data_off, e.count);
            let mut d = t.src.read(o, n)?;
            if let Some(p) = d.iter().position(|&c| c == 0) {
                d.truncate(p);
            }
            ome = parse_ome(d)?;
        }
    }

    // Planes (§3.3)
    let (size_z, size_c, size_t, cp);
    let mut plane_ifd: Vec<usize>; // canonical index (t*cp + c)*size_z + z -> arena idx
    if let Some(om) = &ome {
        size_z = om.size_z.unwrap_or(1);
        size_t = om.size_t.unwrap_or(1);
        let mut sc = om.size_c.unwrap_or(spp);
        if spp > 1 {
            if sc == 1 {
                sc = spp;
            } else if sc != spp {
                rej!("SizeC {sc} differs from SamplesPerPixel {spp}");
            }
        }
        size_c = sc;
        cp = if spp > 1 { 1 } else { size_c };
        let count = (size_z as u128) * (cp as u128) * (size_t as u128);
        if count > MAX_PLANES as u128 {
            rej!("plane count {count} above {MAX_PLANES}");
        }
        let count = count as u64;
        if om.files.len() > 1 {
            rej!("multi-file dataset ({} files)", om.files.len());
        }
        let order = om.dim_order.unwrap_or(*b"ZCT");
        let size_of = |l: u8| match l {
            b'Z' => size_z,
            b'C' => cp,
            _ => size_t,
        };
        let implicit = [TiffData::default()];
        let tds: &[TiffData] = if om.tiffdata.is_empty() { &implicit } else { &om.tiffdata };
        let mut map: Vec<Option<u64>> = vec![None; count as usize];
        for td in tds {
            let fz = td.first[0].unwrap_or(0);
            let fc = td.first[1].unwrap_or(0);
            let ft = td.first[2].unwrap_or(0);
            if fz >= size_z || fc >= cp || ft >= size_t {
                rej!("TiffData First* out of range");
            }
            let ifd = td.ifd.unwrap_or(0);
            let pc = td.plane_count.unwrap_or(if tds.len() == 1 && td.ifd.is_none() { count } else { 1 });
            let pos_of = |l: u8| match l {
                b'Z' => fz,
                b'C' => fc,
                _ => ft,
            };
            let s0 = size_of(order[0]);
            let s1 = size_of(order[1]);
            let start = pos_of(order[0]) + s0 * (pos_of(order[1]) + s1 * pos_of(order[2]));
            let n = pc.min(count - start);
            for i in 0..n {
                let lin = start + i;
                let a = lin % s0;
                let b = (lin / s0) % s1;
                let c = lin / s0 / s1;
                let (mut z, mut ch, mut tt) = (0, 0, 0);
                for (l, v) in [(order[0], a), (order[1], b), (order[2], c)] {
                    match l {
                        b'Z' => z = v,
                        b'C' => ch = v,
                        _ => tt = v,
                    }
                }
                let canon = (tt * cp + ch) * size_z + z;
                map[canon as usize] = Some(ifd + i);
            }
        }
        plane_ifd = Vec::with_capacity(count as usize);
        for (i, m) in map.iter().enumerate() {
            match m {
                Some(k) if (*k as u128) < main.len() as u128 => plane_ifd.push(main[*k as usize]),
                Some(k) => rej!("plane {i} mapped to IFD {k}, past the main chain"),
                None => rej!("plane {i} is not mapped to an IFD"),
            }
        }
    } else {
        size_z = 1;
        size_t = 1;
        size_c = spp;
        cp = 1;
        plane_ifd = vec![ifd0];
    }
    let _ = &mut plane_ifd;

    // Levels (§3.4)
    let mut level_ifds: Vec<Vec<usize>> = vec![plane_ifd.clone()];
    let s = subs[0].len();
    if s > 0 {
        // main-chain index of each plane IFD
        let main_idx: HashMap<usize, usize> = main.iter().enumerate().map(|(i, &a)| (a, i)).collect();
        for k in 0..s {
            let mut v = Vec::new();
            for &p in &plane_ifd {
                let sl = &subs[main_idx[&p]];
                if sl.len() < s {
                    rej!("plane IFD has {} SubIFDs, IFD 0 has {s}", sl.len());
                }
                v.push(sl[k]);
            }
            level_ifds.push(v);
        }
    } else if ome.is_none() {
        let sz0 = t.size(ifd0)?;
        let (mut lw, mut lh) = (sz0.w, sz0.h);
        for &mi in &main[1..] {
            if t.is_tiled(mi) && t.has(mi, T_BPS) {
                let f = t.format(mi)?;
                let sz = t.size(mi)?;
                if f == fmt0 && sz.w < lw && sz.h < lh {
                    lw = sz.w;
                    lh = sz.h;
                    level_ifds.push(vec![mi]);
                }
            }
        }
    }
    let mut levels: Vec<Level> = Vec::new();
    for (li, ifds) in level_ifds.into_iter().enumerate() {
        let mut geo: Option<(u64, u64, u64, u64, Format)> = None;
        for &i in &ifds {
            let f = t.format(i)?;
            let sz = t.size(i)?;
            let (tw, tl) = match sz.tile {
                Some(x) => x,
                None => rej!("level {li}: image is not tiled"),
            };
            if f != fmt0 {
                rej!("level {li}: format {f:?} differs from IFD 0's {fmt0:?}");
            }
            let g = (sz.w, sz.h, tw, tl, f);
            match geo {
                None => geo = Some(g),
                Some(g0) if g0 != g => rej!("level {li}: planes differ in size or format"),
                _ => {}
            }
        }
        let g = geo.unwrap();
        levels.push(Level { ifds, w: g.0, h: g.1, tw: g.2, tl: g.3 });
    }

    // Data type and codecs (§3.5)
    let dtype = match (fmt0.sf, fmt0.bps) {
        (1, 8 | 16 | 32 | 64) => format!("uint{}", fmt0.bps),
        (2, 8 | 16 | 32 | 64) => format!("int{}", fmt0.bps),
        (3, 32 | 64) => format!("float{}", fmt0.bps),
        (sf, b) => rej!("unsupported SampleFormat {sf} with BitsPerSample {b}"),
    };
    let interleaved = spp > 1 && fmt0.planar == 1;
    let planar = spp > 1 && fmt0.planar == 2;
    let n_ch = size_c;
    let has_t = size_t > 1;
    let has_c = n_ch > 1;
    let has_z = size_z > 1;
    let mut dims: Vec<&str> = Vec::new();
    if has_t {
        dims.push("t");
    }
    if has_c {
        dims.push("c");
    }
    if has_z {
        dims.push("z");
    }
    dims.push("y");
    dims.push("x");
    let mut codecs = Vec::new();
    if interleaved {
        codecs.push(transpose_codec(&dims));
    }
    match (fmt0.compression, fmt0.predictor) {
        (1, 1) => codecs.push(bytes_codec(fmt0.bps / 8, le)),
        (8 | 32946, 1) => {
            codecs.push(bytes_codec(fmt0.bps / 8, le));
            codecs.push(zlib_codec());
        }
        (50000, 1) => {
            codecs.push(bytes_codec(fmt0.bps / 8, le));
            codecs.push(zstd_codec());
        }
        (33003 | 33004 | 33005 | 34712, _) => codecs.push(crate::json::obj(vec![("name", J::s("imagecodecs_jpeg2k"))])),
        (c, p) => rej!("unsupported Compression {c} with Predictor {p}"),
    }

    // Output (§3.6)
    let unit_for = |i: usize| -> Option<&'static str> {
        let om = ome.as_ref()?;
        om.phys[i]?;
        let sym = om.phys_unit[i].clone().unwrap_or_else(|| "\u{b5}m".to_string());
        unit_of(&sym)
    };
    let phys = |i: usize| -> f64 { ome.as_ref().and_then(|o| o.phys[i]).unwrap_or(1.0) };
    let mut axes = Vec::new();
    if has_t {
        axes.push(Axis { name: "t", unit: None });
    }
    if has_c {
        axes.push(Axis { name: "c", unit: None });
    }
    if has_z {
        axes.push(Axis { name: "z", unit: unit_for(2) });
    }
    axes.push(Axis { name: "y", unit: unit_for(1) });
    axes.push(Axis { name: "x", unit: unit_for(0) });
    let (w0, h0) = (levels[0].w, levels[0].h);
    let mut scales = Vec::new();
    for lv in &levels {
        let mut sc = Vec::new();
        if has_t {
            sc.push(1.0);
        }
        if has_c {
            sc.push(1.0);
        }
        if has_z {
            sc.push(phys(2));
        }
        sc.push(check_finite(phys(1) * (h0 as f64 / lv.h as f64), "y scale")?);
        sc.push(check_finite(phys(0) * (w0 as f64 / lv.w as f64), "x scale")?);
        scales.push(sc);
    }
    let name = ome.as_ref().and_then(|o| o.image_name.clone()).filter(|n| !n.is_empty());
    out.json("zarr.json", image_group(name.as_deref(), &axes, &scales, None));

    for (li, lv) in levels.iter().enumerate() {
        let mut shape = Vec::new();
        let mut chunk = Vec::new();
        if has_t {
            shape.push(size_t);
            chunk.push(1);
        }
        if has_c {
            shape.push(n_ch);
            chunk.push(if interleaved { spp } else { 1 });
        }
        if has_z {
            shape.push(size_z);
            chunk.push(1);
        }
        shape.push(lv.h);
        shape.push(lv.w);
        chunk.push(lv.tl);
        chunk.push(lv.tw);
        out.json(format!("{li}/zarr.json"), array_json(&shape, &dtype, &chunk, codecs.clone(), &dims));

        let across = lv.w.div_ceil(lv.tw);
        let down = lv.h.div_ceil(lv.tl);
        let tcount = (across as u128) * (down as u128);
        let nsp = if planar { spp } else { 1 };
        for tt in 0..size_t {
            for c in 0..cp {
                for z in 0..size_z {
                    let canon = ((tt * cp + c) * size_z + z) as usize;
                    let ifd = lv.ifds[canon];
                    let offs = t.values(ifd, T_TOFF)?.unwrap();
                    let cnts = t.values(ifd, T_TBC)?.unwrap();
                    let need = tcount * nsp as u128;
                    if offs.len() as u128 != need || cnts.len() as u128 != need {
                        rej!(
                            "level {li}: TileOffsets/TileByteCounts have {}/{} values, expected {need}",
                            offs.len(),
                            cnts.len()
                        );
                    }
                    let tcount = tcount as u64;
                    for s in 0..nsp {
                        for j in 0..tcount {
                            let k = (s * tcount + j) as usize;
                            let n = cnts[k];
                            if n == 0 {
                                continue;
                            }
                            let mut key = format!("{li}/c");
                            if has_t {
                                key.push_str(&format!("/{tt}"));
                            }
                            if has_c {
                                let cc = if spp == 1 {
                                    c
                                } else if planar {
                                    s
                                } else {
                                    0
                                };
                                key.push_str(&format!("/{cc}"));
                            }
                            if has_z {
                                key.push_str(&format!("/{z}"));
                            }
                            key.push_str(&format!("/{}/{}", j / across, j % across));
                            out.ranges(key, vec![(offs[k], n)])?;
                        }
                    }
                }
            }
        }
    }
    if let Some(om) = ome {
        out.bytes("OME/METADATA.ome.xml", om.d);
    }
    Ok(())
}

