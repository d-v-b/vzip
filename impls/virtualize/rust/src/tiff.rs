//! TIFF profile (VIRTUALIZE.md §3).

use crate::http::Source;
use crate::{Axis, Entry, Error, Output, array_json, codecs, image_json, reject, unit_of, xml};
use std::collections::{HashMap, HashSet};

const MAX_IFDS: usize = 100_000;

#[derive(Clone)]
struct TagEntry {
    typ: u16,
    count: u64,
    /// The raw value-or-offset field (4 bytes in TIFF, 8 in BigTIFF).
    field: Vec<u8>,
}

struct Ifd {
    tags: HashMap<u16, TagEntry>,
}

struct Reader<'a> {
    src: &'a mut Source,
    le: bool,
    big: bool,
}

fn type_size(t: u16) -> Option<u64> {
    Some(match t {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 | 16 | 17 | 18 => 8,
        _ => return None,
    })
}

impl Reader<'_> {
    fn uint(&self, b: &[u8]) -> u64 {
        let mut v = 0u64;
        if self.le {
            for &x in b.iter().rev() {
                v = (v << 8) | x as u64;
            }
        } else {
            for &x in b {
                v = (v << 8) | x as u64;
            }
        }
        v
    }

    fn read_ifd(&mut self, off: u64) -> Result<Ifd, Error> {
        let (cnt_size, entry_size, field) = if self.big { (8, 20, 8) } else { (2, 12, 4) };
        let cb = self.src.read(off, cnt_size)?;
        let n = self.uint(&cb);
        let body = self.src.read(off + cnt_size, n.checked_mul(entry_size).ok_or(Error::Reject("IFD too large".into()))?)?;
        let mut tags = HashMap::new();
        for i in 0..n as usize {
            let e = &body[i * entry_size as usize..(i + 1) * entry_size as usize];
            let tag = self.uint(&e[0..2]) as u16;
            let typ = self.uint(&e[2..4]) as u16;
            let (count, f) = if self.big {
                (self.uint(&e[4..12]), e[12..20].to_vec())
            } else {
                (self.uint(&e[4..8]), e[8..12].to_vec())
            };
            debug_assert_eq!(f.len(), field);
            // first occurrence wins for duplicate tags
            tags.entry(tag).or_insert(TagEntry { typ, count, field: f });
        }
        Ok(Ifd { tags })
    }

    fn next_ifd_offset(&mut self, off: u64) -> Result<u64, Error> {
        let (cnt_size, entry_size, field) = if self.big { (8u64, 20u64, 8u64) } else { (2, 12, 4) };
        let cb = self.src.read(off, cnt_size)?;
        let n = self.uint(&cb);
        let b = self.src.read(off + cnt_size + n * entry_size, field)?;
        Ok(self.uint(&b))
    }

    fn raw(&mut self, e: &TagEntry) -> Result<Vec<u8>, Error> {
        let Some(sz) = type_size(e.typ) else {
            return reject(format!("unknown TIFF field type {}", e.typ));
        };
        let total = sz.checked_mul(e.count).ok_or(Error::Reject("tag too large".into()))?;
        if total <= e.field.len() as u64 {
            Ok(e.field[..total as usize].to_vec())
        } else {
            let off = self.uint(&e.field);
            self.src.read(off, total)
        }
    }

    /// Integer values of a tag (any integer field type).
    fn ints(&mut self, e: &TagEntry) -> Result<Vec<u64>, Error> {
        let raw = self.raw(e)?;
        let sz = type_size(e.typ).unwrap() as usize;
        let signed = matches!(e.typ, 6 | 8 | 9 | 17);
        if !matches!(e.typ, 1 | 3 | 4 | 6 | 7 | 8 | 9 | 13 | 16 | 17 | 18) {
            return reject(format!("TIFF tag has non-integer type {}", e.typ));
        }
        raw.chunks(sz)
            .map(|c| {
                let v = self.uint(c);
                if signed && (v >> (sz * 8 - 1)) & 1 == 1 {
                    reject("negative value in TIFF integer tag")
                } else {
                    Ok(v)
                }
            })
            .collect()
    }
}

#[derive(Clone, PartialEq, Debug)]
struct Format {
    bits: u64,
    spp: u64,
    sample_format: u64,
    planar: u64,
    compression: u64,
    predictor: u64,
}

#[derive(Clone, Debug)]
struct Info {
    width: u64,
    length: u64,
    format: Format,
    tiled: bool,
    tile_w: u64,
    tile_l: u64,
}

fn info(r: &mut Reader, ifd: &Ifd) -> Result<Info, Error> {
    let mut one = |tag: u16, name: &str, default: Option<u64>| -> Result<Vec<u64>, Error> {
        match ifd.tags.get(&tag) {
            Some(e) => {
                let v = r.ints(&e.clone())?;
                if v.is_empty() {
                    return reject(format!("TIFF tag {name} has no values"));
                }
                Ok(v)
            }
            None => match default {
                Some(d) => Ok(vec![d]),
                None => reject(format!("TIFF tag {name} ({tag}) is required")),
            },
        }
    };
    let width = one(256, "ImageWidth", None)?[0];
    let length = one(257, "ImageLength", None)?[0];
    let bps = one(258, "BitsPerSample", None)?;
    if bps.iter().any(|&b| b != bps[0]) {
        return reject("BitsPerSample values differ");
    }
    let compression = one(259, "Compression", Some(1))?[0];
    let spp = one(277, "SamplesPerPixel", Some(1))?[0];
    let planar = if spp > 1 { one(284, "PlanarConfiguration", Some(1))?[0] } else { 1 };
    let predictor = one(317, "Predictor", Some(1))?[0];
    let sample_format = one(339, "SampleFormat", Some(1))?[0];
    let tiled = ifd.tags.contains_key(&322) && ifd.tags.contains_key(&324);
    let (tile_w, tile_l) = if tiled {
        (one(322, "TileWidth", None)?[0], one(323, "TileLength", None)?[0])
    } else {
        (0, 0)
    };
    Ok(Info {
        width,
        length,
        format: Format { bits: bps[0], spp, sample_format, planar, compression, predictor },
        tiled,
        tile_w,
        tile_l,
    })
}

fn parse_uint_attr(attrs: &[(String, String)], name: &str) -> Result<Option<u64>, Error> {
    match xml::attr(attrs, name) {
        None => Ok(None),
        Some(v) => match v.trim().parse::<u64>() {
            Ok(n) => Ok(Some(n)),
            Err(_) => reject(format!("OME-XML attribute {name}={v:?} is not a non-negative integer")),
        },
    }
}

fn parse_float_attr(attrs: &[(String, String)], name: &str) -> Result<Option<f64>, Error> {
    match xml::attr(attrs, name) {
        None => Ok(None),
        Some(v) => {
            let t = v.trim();
            // Only decimal numbers (no "inf"/"nan" spellings).
            let ok = !t.is_empty()
                && t.chars().all(|c| c.is_ascii_digit() || matches!(c, '+' | '-' | '.' | 'e' | 'E'));
            match t.parse::<f64>() {
                Ok(x) if ok && x.is_finite() => Ok(Some(x)),
                _ => reject(format!("OME-XML attribute {name}={v:?} is not a finite number")),
            }
        }
    }
}

pub fn virtualize(src: &mut Source) -> Result<Output, Error> {
    let hdr = src.read(0, 8.min(src.size))?;
    if hdr.len() < 8 {
        return reject("file too short for a TIFF header");
    }
    let le = hdr[0] == b'I';
    let mut r = Reader { src, le, big: false };
    let magic = r.uint(&hdr[2..4]);
    let first = match magic {
        42 => r.uint(&hdr[4..8]),
        43 => {
            r.big = true;
            let h = r.src.read(0, 16)?;
            if r.uint(&h[4..6]) != 8 || r.uint(&h[6..8]) != 0 {
                return reject("BigTIFF offset size is not 8");
            }
            r.uint(&h[8..16])
        }
        _ => return reject("bad TIFF magic"),
    };

    // §3.1: the main chain and each IFD's SubIFDs
    let mut ifds: Vec<Ifd> = Vec::new();
    let mut main: Vec<usize> = Vec::new();
    let mut subs: Vec<Vec<usize>> = Vec::new(); // per main-chain index
    let mut seen = HashSet::new();
    let mut off = first;
    while off != 0 {
        if !seen.insert(off) {
            return reject("IFD cycle");
        }
        if ifds.len() >= MAX_IFDS {
            return reject("more than 100000 IFDs");
        }
        let ifd = r.read_ifd(off)?;
        let sub_offsets = match ifd.tags.get(&330) {
            Some(e) => r.ints(&e.clone())?,
            None => Vec::new(),
        };
        let next = r.next_ifd_offset(off)?;
        ifds.push(ifd);
        main.push(ifds.len() - 1);
        let mut mine = Vec::new();
        for so in sub_offsets {
            if ifds.len() >= MAX_IFDS {
                return reject("more than 100000 IFDs");
            }
            ifds.push(r.read_ifd(so)?);
            mine.push(ifds.len() - 1);
        }
        subs.push(mine);
        off = next;
    }
    if main.is_empty() {
        return reject("TIFF has no IFDs");
    }

    let first_info = info(&mut r, &ifds[main[0]])?;
    let spp = first_info.format.spp;
    if spp == 0 {
        return reject("SamplesPerPixel is 0");
    }

    // §3.2 OME-XML
    let mut ome_xml: Option<String> = None;
    if let Some(e) = ifds[main[0]].tags.get(&270).cloned() {
        let raw = r.raw(&e)?;
        let upto = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
        let bytes = &raw[..upto];
        if bytes.windows(4).any(|w| w == b"<OME") {
            match String::from_utf8(bytes.to_vec()) {
                Ok(x) => ome_xml = Some(x),
                Err(_) => return reject("OME-XML is not valid UTF-8"),
            }
        }
    }
    let ome = match &ome_xml {
        Some(x) => Some(xml::extract(x)?),
        None => None,
    };

    // §3.3 planes
    let (size_z, size_c, size_t, cp, plane_ifd): (u64, u64, u64, u64, Vec<usize>);
    let mut phys: [Option<f64>; 3] = [None; 3]; // x, y, z
    let mut units: [Option<&'static str>; 3] = [None; 3];
    match &ome {
        None => {
            size_z = 1;
            size_t = 1;
            size_c = spp;
            cp = 1;
            plane_ifd = vec![0];
        }
        Some(o) => {
            let px = &o.pixels;
            size_z = parse_uint_attr(px, "SizeZ")?.unwrap_or(1);
            size_t = parse_uint_attr(px, "SizeT")?.unwrap_or(1);
            let mut sc = parse_uint_attr(px, "SizeC")?.unwrap_or(spp);
            if spp > 1 {
                if sc == 1 {
                    sc = spp;
                } else if sc != spp {
                    return reject(format!("SizeC {sc} differs from SamplesPerPixel {spp}"));
                }
            }
            size_c = sc;
            if size_z == 0 || size_c == 0 || size_t == 0 {
                return reject("OME-XML SizeZ, SizeC or SizeT is 0");
            }
            cp = if spp > 1 { 1 } else { size_c };
            for (i, a) in ["X", "Y", "Z"].iter().enumerate() {
                phys[i] = parse_float_attr(px, &format!("PhysicalSize{a}"))?;
                if phys[i].is_some() {
                    let sym = xml::attr(px, &format!("PhysicalSize{a}Unit")).unwrap_or("\u{b5}m");
                    units[i] = unit_of(sym);
                }
            }
            let order = xml::attr(px, "DimensionOrder").unwrap_or("XYZCT");
            let rest = order.strip_prefix("XY").unwrap_or("");
            let mut letters: Vec<char> = rest.chars().collect();
            let fastest_first = letters.clone();
            letters.sort();
            if letters != ['C', 'T', 'Z'] {
                return reject(format!("bad DimensionOrder {order:?}"));
            }
            let size_of = |c: char| match c {
                'Z' => size_z,
                'C' => cp,
                _ => size_t,
            };
            let total = size_z
                .checked_mul(size_t)
                .and_then(|x| x.checked_mul(cp))
                .ok_or(Error::Reject("too many planes".into()))?;
            if total > 10_000_000 {
                return reject("too many planes");
            }
            // linear position index (in DimensionOrder) of (z, c, t)
            let linear = |z: u64, c: u64, t: u64| -> u64 {
                let mut idx = 0;
                for &d in fastest_first.iter().rev() {
                    let v = match d {
                        'Z' => z,
                        'C' => c,
                        _ => t,
                    };
                    idx = idx * size_of(d) + v;
                }
                idx
            };
            let mut map: Vec<Option<u64>> = vec![None; total as usize];
            let tds: Vec<&xml::TiffData> = o.tiffdata.iter().collect();
            let implicit = xml::TiffData::default();
            let tds = if tds.is_empty() { vec![&implicit] } else { tds };
            let only = tds.len() == 1;
            for td in &tds {
                if td.uuid_filename.is_some() {
                    return reject("TiffData refers to another file (UUID FileName)");
                }
                let a = &td.attrs;
                let ifd0 = parse_uint_attr(a, "IFD")?;
                let fz = parse_uint_attr(a, "FirstZ")?.unwrap_or(0);
                let fc = parse_uint_attr(a, "FirstC")?.unwrap_or(0);
                let ft = parse_uint_attr(a, "FirstT")?.unwrap_or(0);
                if fz >= size_z || fc >= cp || ft >= size_t {
                    return reject("TiffData start position out of range");
                }
                let count = match parse_uint_attr(a, "PlaneCount")? {
                    Some(n) => n,
                    None if only && ifd0.is_none() => total,
                    None => 1,
                };
                let start = linear(fz, fc, ft);
                let ifd0 = ifd0.unwrap_or(0);
                for k in 0..count {
                    let p = start + k;
                    if p >= total {
                        break;
                    }
                    map[p as usize] = Some(ifd0 + k);
                }
            }
            // canonical plane order: index (z, c, t) -> z + Z*(c + Cp*t)
            let mut pi = Vec::with_capacity(total as usize);
            for t in 0..size_t {
                for c in 0..cp {
                    for z in 0..size_z {
                        let m = map[linear(z, c, t) as usize];
                        match m {
                            Some(i) if (i as usize) < main.len() => pi.push(i as usize),
                            Some(i) => return reject(format!("plane mapped to IFD {i}, which does not exist")),
                            None => return reject(format!("plane z={z} c={c} t={t} is not mapped to an IFD")),
                        }
                    }
                }
            }
            plane_ifd = pi;
        }
    }
    let plane_index = |z: u64, c: u64, t: u64| (z + size_z * (c + cp * t)) as usize;

    // §3.4 pyramid levels: each level is a list of IFD indices (into `ifds`), one per plane
    let mut levels: Vec<Vec<usize>> = vec![plane_ifd.iter().map(|&m| main[m]).collect()];
    if !subs[0].is_empty() {
        for k in 1..=subs[0].len() {
            let mut lv = Vec::new();
            for &m in &plane_ifd {
                match subs[m].get(k - 1) {
                    Some(&i) => lv.push(i),
                    None => return reject(format!("IFD {m} has no SubIFD {k}")),
                }
            }
            levels.push(lv);
        }
    } else if ome.is_none() {
        let (mut pw, mut pl) = (first_info.width, first_info.length);
        for &m in &main[1..] {
            let Ok(inf) = info(&mut r, &ifds[m]) else { continue };
            if inf.tiled && inf.format == first_info.format && inf.width < pw && inf.length < pl {
                levels.push(vec![m]);
                pw = inf.width;
                pl = inf.length;
            }
        }
    }

    // validate levels
    let mut level_info: Vec<Info> = Vec::new();
    for (li, lv) in levels.iter().enumerate() {
        let mut first: Option<Info> = None;
        for &i in lv {
            let inf = info(&mut r, &ifds[i])?;
            if !inf.tiled {
                return reject(format!("level {li} image is not tiled"));
            }
            if inf.format != first_info.format {
                return reject(format!("level {li} image format {:?} differs from the first IFD's", inf.format));
            }
            if inf.tile_w == 0 || inf.tile_l == 0 {
                return reject("tile size is 0");
            }
            match &first {
                None => first = Some(inf),
                Some(f) => {
                    if (f.width, f.length, f.tile_w, f.tile_l) != (inf.width, inf.length, inf.tile_w, inf.tile_l) {
                        return reject(format!("planes of level {li} differ in size or tiling"));
                    }
                }
            }
        }
        level_info.push(first.unwrap());
    }

    // §3.5 data type and codecs
    let f = &first_info.format;
    let kind = match f.sample_format {
        1 => "uint",
        2 => "int",
        3 => "float",
        x => return reject(format!("SampleFormat {x} not supported")),
    };
    let ok_bits = if kind == "float" { matches!(f.bits, 32 | 64) } else { matches!(f.bits, 8 | 16 | 32 | 64) };
    if !ok_bits {
        return reject(format!("BitsPerSample {} not supported for {kind}", f.bits));
    }
    let dtype = format!("{kind}{}", f.bits);
    let (jpeg2k, compressor) = match (f.compression, f.predictor) {
        (1, 1) => (false, None),
        (8 | 32946, 1) => (false, Some("zlib")),
        (50000, 1) => (false, Some("zstd")),
        (33003 | 33004 | 33005 | 34712, _) => (true, None),
        (c, p) => return reject(format!("Compression {c} with Predictor {p} not supported")),
    };
    if spp > 1 && !matches!(f.planar, 1 | 2) {
        return reject(format!("PlanarConfiguration {} not supported", f.planar));
    }
    let interleaved = spp > 1 && f.planar == 1;
    let planar_samples = spp > 1 && f.planar == 2;

    // §3.6 output
    let has_t = size_t > 1;
    let has_c = size_c > 1;
    let has_z = size_z > 1;
    let mut axes = Vec::new();
    let mut dims: Vec<&str> = Vec::new();
    if has_t {
        axes.push(Axis { name: "t", unit: None });
        dims.push("t");
    }
    if has_c {
        axes.push(Axis { name: "c", unit: None });
        dims.push("c");
    }
    if has_z {
        axes.push(Axis { name: "z", unit: units[2] });
        dims.push("z");
    }
    axes.push(Axis { name: "y", unit: units[1] });
    axes.push(Axis { name: "x", unit: units[0] });
    dims.push("y");
    dims.push("x");
    let codec_list = codecs(&dims, interleaved && has_c, (f.bits / 8) as u32, if le { "little" } else { "big" }, jpeg2k, compressor);
    if interleaved && !has_c {
        unreachable!("spp > 1 implies a c axis");
    }

    let mut out = Output::new();
    let (w0, h0) = (level_info[0].width as f64, level_info[0].length as f64);
    let mut scales = Vec::new();
    for (li, lv) in levels.iter().enumerate() {
        let inf = &level_info[li];
        let mut shape = Vec::new();
        let mut chunk = Vec::new();
        let mut scale = Vec::new();
        if has_t {
            shape.push(size_t);
            chunk.push(1);
            scale.push(1.0);
        }
        if has_c {
            shape.push(size_c);
            chunk.push(if interleaved { spp } else { 1 });
            scale.push(1.0);
        }
        if has_z {
            shape.push(size_z);
            chunk.push(1);
            scale.push(phys[2].unwrap_or(1.0));
        }
        shape.push(inf.length);
        shape.push(inf.width);
        chunk.push(inf.tile_l);
        chunk.push(inf.tile_w);
        scale.push(phys[1].unwrap_or(1.0) * (h0 / inf.length as f64));
        scale.push(phys[0].unwrap_or(1.0) * (w0 / inf.width as f64));
        scales.push(scale);
        out.insert(
            format!("{li}/zarr.json"),
            Entry::Json(array_json(&shape, &dtype, &chunk, codec_list.clone(), &dims)),
        );

        let across = inf.width.div_ceil(inf.tile_w);
        let down = inf.length.div_ceil(inf.tile_l);
        let tpp = across * down;
        let nsp = if planar_samples { spp } else { 1 };
        for t in 0..size_t {
            for c in 0..cp {
                for z in 0..size_z {
                    let i = lv[plane_index(z, c, t)];
                    let ifd = &ifds[i];
                    let (Some(oe), Some(be)) = (ifd.tags.get(&324).cloned(), ifd.tags.get(&325).cloned()) else {
                        return reject("TileOffsets or TileByteCounts missing");
                    };
                    let offs = r.ints(&oe)?;
                    let cnts = r.ints(&be)?;
                    if offs.len() as u64 != tpp * nsp || cnts.len() as u64 != tpp * nsp {
                        return reject(format!(
                            "IFD has {} tile offsets and {} byte counts, expected {}",
                            offs.len(),
                            cnts.len(),
                            tpp * nsp
                        ));
                    }
                    for k in 0..(tpp * nsp) {
                        let n = cnts[k as usize];
                        if n == 0 {
                            continue;
                        }
                        let (smp, j) = (k / tpp, k % tpp);
                        let mut key = format!("{li}/c");
                        if has_t {
                            key.push_str(&format!("/{t}"));
                        }
                        if has_c {
                            let cc = if spp > 1 {
                                if planar_samples { smp } else { 0 }
                            } else {
                                c
                            };
                            key.push_str(&format!("/{cc}"));
                        }
                        if has_z {
                            key.push_str(&format!("/{z}"));
                        }
                        key.push_str(&format!("/{}/{}", j / across, j % across));
                        out.insert(key, Entry::Ranges(vec![(offs[k as usize], n)]));
                    }
                }
            }
        }
    }
    let name = ome.as_ref().and_then(|o| o.image_name.clone());
    out.insert("zarr.json".into(), Entry::Json(image_json(name.as_deref(), &axes, &scales, None)?));
    if let Some(x) = ome_xml {
        out.insert("OME/METADATA.ome.xml".into(), Entry::Bytes(x.into_bytes()));
    }
    Ok(out)
}
