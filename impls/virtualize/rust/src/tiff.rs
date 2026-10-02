//! TIFF profile (VIRTUALIZE.md §3).

use std::collections::{HashMap, HashSet};

use crate::common::*;
use crate::rej;
use crate::xml::{self, Ev};

struct Tag {
    tag: u16,
    typ: u16,
    count: u64,
    field: Vec<u8>,
}

struct Ifd {
    tags: Vec<Tag>,
}

impl Ifd {
    /// Of duplicate tags, the first is used (§3.1).
    fn find(&self, tag: u16) -> Option<&Tag> {
        self.tags.iter().find(|t| t.tag == tag)
    }
}

fn type_size(t: u16) -> Option<u64> {
    match t {
        1 | 2 | 6 | 7 => Some(1),
        3 | 8 => Some(2),
        4 | 9 | 11 | 13 => Some(4),
        5 | 10 | 12 | 16 | 17 | 18 => Some(8),
        _ => None,
    }
}

struct Tiff<'a> {
    src: &'a Source,
    le: bool,
    big: bool,
}

impl Tiff<'_> {
    fn u16(&self, b: &[u8]) -> u16 {
        let a = [b[0], b[1]];
        if self.le { u16::from_le_bytes(a) } else { u16::from_be_bytes(a) }
    }
    fn u32(&self, b: &[u8]) -> u32 {
        let a = [b[0], b[1], b[2], b[3]];
        if self.le { u32::from_le_bytes(a) } else { u32::from_be_bytes(a) }
    }
    fn u64(&self, b: &[u8]) -> u64 {
        let a: [u8; 8] = b[..8].try_into().unwrap();
        if self.le { u64::from_le_bytes(a) } else { u64::from_be_bytes(a) }
    }
    fn off(&self, b: &[u8]) -> u64 {
        if self.big { self.u64(b) } else { self.u32(b) as u64 }
    }

    fn read_ifd(&self, off: u64) -> R<Ifd> {
        safe(off, "IFD offset")?;
        let (csz, esz, osz) = if self.big { (8u64, 20u64, 8u64) } else { (2, 12, 4) };
        let cb = self.src.read(off, csz)?;
        let count = if self.big { self.u64(&cb) } else { self.u16(&cb) as u64 };
        let total = count
            .checked_mul(esz)
            .and_then(|v| v.checked_add(csz + osz))
            .ok_or_else(|| E::Reject("IFD too large".into()))?;
        // Bounds check before allocating.
        if off.checked_add(total).is_none_or(|e| e > self.src.size) {
            rej!("IFD at {off} extends past the end of the file");
        }
        let data = self.src.read(off + csz, count * esz)?;
        let mut tags = Vec::with_capacity(count as usize);
        for e in data.chunks(esz as usize) {
            let tag = self.u16(&e[0..2]);
            let typ = self.u16(&e[2..4]);
            let (cnt, field) = if self.big {
                (self.u64(&e[4..12]), e[12..20].to_vec())
            } else {
                (self.u32(&e[4..8]) as u64, e[8..12].to_vec())
            };
            tags.push(Tag { tag, typ, count: cnt, field });
        }
        Ok(Ifd { tags })
    }

    fn next_offset(&self, off: u64) -> R<u64> {
        let (csz, esz, osz) = if self.big { (8u64, 20u64, 8u64) } else { (2, 12, 4) };
        let cb = self.src.read(off, csz)?;
        let count = if self.big { self.u64(&cb) } else { self.u16(&cb) as u64 };
        let nb = self.src.read(off + csz + count * esz, osz)?;
        Ok(self.off(&nb))
    }

    /// The raw value bytes of a tag. An unknown field type rejects.
    fn raw(&self, t: &Tag) -> R<Vec<u8>> {
        let ts = match type_size(t.typ) {
            Some(v) => v,
            None => rej!("tag {} has unknown field type {}", t.tag, t.typ),
        };
        let n = t.count.checked_mul(ts).ok_or_else(|| E::Reject("tag value too large".into()))?;
        let cap = if self.big { 8 } else { 4 };
        if n <= cap {
            Ok(t.field[..n as usize].to_vec())
        } else {
            let off = safe(self.off(&t.field), "value offset")?;
            safe(n, "value length")?;
            self.src.read(off, n)
        }
    }

    /// Unsigned integer values of a tag (BYTE, SHORT, LONG, IFD, LONG8, IFD8).
    fn uints(&self, ifd: &Ifd, tag: u16) -> R<Option<Vec<u64>>> {
        let t = match ifd.find(tag) {
            Some(t) => t,
            None => return Ok(None),
        };
        let raw = self.raw(t)?;
        let v: Vec<u64> = match t.typ {
            1 => raw.iter().map(|&b| b as u64).collect(),
            3 => raw.chunks(2).map(|c| self.u16(c) as u64).collect(),
            4 | 13 => raw.chunks(4).map(|c| self.u32(c) as u64).collect(),
            16 | 18 => raw.chunks(8).map(|c| self.u64(c)).collect(),
            other => rej!("tag {tag} has non-integer field type {other}"),
        };
        Ok(Some(v))
    }

    /// A single-valued integer tag (the first value).
    fn uint(&self, ifd: &Ifd, tag: u16) -> R<Option<u64>> {
        match self.uints(ifd, tag)? {
            None => Ok(None),
            Some(v) if v.is_empty() => rej!("tag {tag} has no values"),
            Some(v) => Ok(Some(v[0])),
        }
    }

    fn req(&self, ifd: &Ifd, tag: u16, what: &str) -> R<u64> {
        match self.uint(ifd, tag)? {
            Some(v) => Ok(v),
            None => rej!("required tag {what} ({tag}) is missing"),
        }
    }

    fn is_tiled(&self, ifd: &Ifd) -> bool {
        ifd.find(322).is_some() && ifd.find(324).is_some()
    }

    /// The IFD's format. `Ok(Err(reason))` when it has no well-defined format
    /// (missing BitsPerSample, unequal BitsPerSample/SampleFormat values).
    fn format(&self, ifd: &Ifd) -> R<Result<Format, String>> {
        let bps = match self.uints(ifd, 258)? {
            None => return Ok(Err("BitsPerSample is missing".into())),
            Some(v) => v,
        };
        if bps.is_empty() {
            rej!("BitsPerSample has no values");
        }
        if bps.iter().any(|&b| b != bps[0]) {
            return Ok(Err("BitsPerSample values differ".into()));
        }
        let sf = match self.uints(ifd, 339)? {
            None => 1,
            Some(v) if v.is_empty() => rej!("SampleFormat has no values"),
            Some(v) => {
                if v.iter().any(|&x| x != v[0]) {
                    return Ok(Err("SampleFormat values differ".into()));
                }
                v[0]
            }
        };
        let spp = self.uint(ifd, 277)?.unwrap_or(1);
        let planar = if spp > 1 {
            let p = self.uint(ifd, 284)?.unwrap_or(1);
            if p != 1 && p != 2 {
                rej!("PlanarConfiguration {p} is not 1 or 2");
            }
            p
        } else {
            1
        };
        let comp = self.uint(ifd, 259)?.unwrap_or(1);
        let pred = self.uint(ifd, 317)?.unwrap_or(1);
        Ok(Ok(Format { bits: bps[0], spp, sf, planar, comp, pred }))
    }

    fn strict_format(&self, ifd: &Ifd) -> R<Format> {
        match self.format(ifd)? {
            Ok(f) => Ok(f),
            Err(e) => reject(e),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
struct Format {
    bits: u64,
    spp: u64,
    sf: u64,
    planar: u64,
    comp: u64,
    pred: u64,
}

// ------------------------------------------------------------------ OME-XML

struct TiffData {
    ifd: Option<u64>,
    first: [Option<u64>; 3], // z, c, t
    plane_count: Option<u64>,
    uuid: Option<String>, // the file identity named by the UUID element
}

struct Ome {
    image_name: Option<String>,
    size_z: Option<u64>,
    size_c: Option<u64>,
    size_t: Option<u64>,
    order: Option<String>,
    phys: [Option<f64>; 3],         // x, y, z
    units: [Option<String>; 3],     // x, y, z
    tiffdata: Vec<TiffData>,
}

fn int_attr(attrs: &[(String, String)], name: &str) -> R<Option<u64>> {
    let v = match attrs.iter().find(|(k, _)| k == name) {
        Some((_, v)) => v,
        None => return Ok(None),
    };
    let t = v.trim_matches(|c| matches!(c, ' ' | '\t' | '\r' | '\n'));
    if t.is_empty() || !t.bytes().all(|b| b.is_ascii_digit()) {
        rej!("attribute {name}={v:?} is not a decimal integer");
    }
    match t.parse::<u64>() {
        Ok(n) => Ok(Some(n)),
        Err(_) => rej!("attribute {name}={v:?} is out of range"),
    }
}

fn attr<'a>(attrs: &'a [(String, String)], name: &str) -> Option<&'a str> {
    attrs.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
}

/// `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?`, finite and positive.
fn physical_size(v: Option<&str>) -> Option<f64> {
    let v = v?;
    let b = v.as_bytes();
    let mut i = 0;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    let d0 = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    let int_digits = i - d0;
    let mut frac_digits = 0;
    if i < b.len() && b[i] == b'.' {
        i += 1;
        let f0 = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        frac_digits = i - f0;
    }
    if int_digits == 0 && frac_digits == 0 {
        return None;
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        let e0 = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == e0 {
            return None;
        }
    }
    if i != b.len() {
        return None;
    }
    let x: f64 = v.parse().ok()?;
    if x.is_finite() && x > 0.0 { Some(x) } else { None }
}

fn parse_ome(x: &str) -> R<Ome> {
    let evs = xml::scan(x);
    let mut ome = Ome {
        image_name: None,
        size_z: None,
        size_c: None,
        size_t: None,
        order: None,
        phys: [None; 3],
        units: [None, None, None],
        tiffdata: Vec::new(),
    };
    for e in &evs {
        if let Ev::Start { name, attrs, .. } = e {
            if name == "Image" {
                ome.image_name = attr(attrs, "Name").map(|s| s.to_string());
                break;
            }
        }
    }
    let pix = evs.iter().position(|e| matches!(e, Ev::Start { name, .. } if name == "Pixels"));
    if let Some(pi) = pix {
        let (attrs, sc) = match &evs[pi] {
            Ev::Start { attrs, self_closing, .. } => (attrs, *self_closing),
            _ => unreachable!(),
        };
        ome.size_z = int_attr(attrs, "SizeZ")?;
        ome.size_c = int_attr(attrs, "SizeC")?;
        ome.size_t = int_attr(attrs, "SizeT")?;
        for (n, v) in [("SizeZ", ome.size_z), ("SizeC", ome.size_c), ("SizeT", ome.size_t)] {
            if v == Some(0) {
                rej!("{n} is 0");
            }
        }
        ome.order = attr(attrs, "DimensionOrder").map(|s| s.to_string());
        for (k, a) in ["X", "Y", "Z"].iter().enumerate() {
            ome.phys[k] = physical_size(attr(attrs, &format!("PhysicalSize{a}")));
            ome.units[k] = attr(attrs, &format!("PhysicalSize{a}Unit")).map(|s| s.to_string());
        }
        if !sc {
            let mut i = pi + 1;
            while i < evs.len() {
                match &evs[i] {
                    Ev::End { name } if name == "Pixels" => break,
                    Ev::Start { name, attrs, self_closing } if name == "TiffData" => {
                        let mut td = TiffData {
                            ifd: int_attr(attrs, "IFD")?,
                            first: [
                                int_attr(attrs, "FirstZ")?,
                                int_attr(attrs, "FirstC")?,
                                int_attr(attrs, "FirstT")?,
                            ],
                            plane_count: int_attr(attrs, "PlaneCount")?,
                            uuid: None,
                        };
                        if td.plane_count == Some(0) {
                            rej!("PlaneCount is 0");
                        }
                        if !*self_closing {
                            // Look for the UUID start tag before </TiffData>.
                            let mut j = i + 1;
                            while j < evs.len() {
                                match &evs[j] {
                                    Ev::End { name } if name == "TiffData" || name == "Pixels" => break,
                                    Ev::Start { name, .. } if name == "TiffData" => break,
                                    Ev::Start { name, attrs, self_closing } if name == "UUID" => {
                                        let file_name = attr(attrs, "FileName").map(|s| s.to_string());
                                        let mut text = String::new();
                                        if !*self_closing {
                                            let mut k = j + 1;
                                            while k < evs.len() {
                                                match &evs[k] {
                                                    Ev::Text(t) => text.push_str(t),
                                                    _ => break,
                                                }
                                                k += 1;
                                            }
                                        }
                                        let text = xml::decode_entities(&text);
                                        td.uuid = Some(match file_name {
                                            Some(f) => format!("F:{f}"),
                                            None => format!("U:{text}"),
                                        });
                                        break;
                                    }
                                    _ => {}
                                }
                                j += 1;
                            }
                        }
                        ome.tiffdata.push(td);
                    }
                    _ => {}
                }
                i += 1;
            }
        }
    }
    Ok(ome)
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

// ------------------------------------------------------------------ main

pub fn virtualize(src: &Source, out: &mut Output) -> R<()> {
    let h = src.read(0, 4)?;
    let le = h[0] == 0x49;
    let mut t = Tiff { src, le, big: false };
    let magic = t.u16(&h[2..4]);
    let first = if magic == 43 {
        t.big = true;
        let b = src.read(4, 12)?;
        let osz = t.u16(&b[0..2]);
        let res = t.u16(&b[2..4]);
        if osz != 8 || res != 0 {
            rej!("BigTIFF header has offset size {osz} and reserved word {res}");
        }
        t.u64(&b[4..12])
    } else {
        t.u32(&src.read(4, 4)?) as u64
    };

    // §3.1 IFDs: the main chain, then the SubIFDs of each main-chain IFD.
    let mut visited: HashSet<u64> = HashSet::new();
    let mut nread = 0usize;
    let mut visit = |off: u64| -> R<()> {
        nread += 1;
        if nread > 100000 {
            rej!("more than 100000 IFDs");
        }
        if !visited.insert(off) {
            rej!("IFD offset {off} is read twice (cycle)");
        }
        Ok(())
    };
    let mut main: Vec<Ifd> = Vec::new();
    let mut off = first;
    while off != 0 {
        visit(off)?;
        let ifd = t.read_ifd(off)?;
        main.push(ifd);
        off = t.next_offset(off)?;
    }
    if main.is_empty() {
        rej!("the TIFF has no IFDs");
    }
    let mut subs: Vec<Vec<Ifd>> = Vec::new();
    for ifd in &main {
        let offs = t.uints(ifd, 330)?.unwrap_or_default();
        let mut v = Vec::new();
        for o in offs {
            visit(o)?;
            v.push(t.read_ifd(o)?);
        }
        subs.push(v);
    }
    let ifd0 = &main[0];

    // §3.2 OME-XML.
    let mut d_bytes: Option<Vec<u8>> = None;
    let mut ome: Option<Ome> = None;
    if let Some(tag) = ifd0.find(270) {
        let raw = t.raw(tag)?; // unknown field type rejects
        if tag.typ == 2 {
            let d = match raw.iter().position(|&b| b == 0) {
                Some(p) => raw[..p].to_vec(),
                None => raw,
            };
            if let Ok(text) = std::str::from_utf8(&d) {
                if xml::has_ome_start_tag(text) {
                    ome = Some(parse_ome(text)?);
                    d_bytes = Some(d.clone());
                }
            }
        }
    }

    let fmt0 = t.strict_format(ifd0)?;
    let spp = fmt0.spp;
    if spp == 0 {
        rej!("SamplesPerPixel is 0");
    }
    let interleaved = spp > 1 && fmt0.planar == 1;
    let planar = spp > 1 && fmt0.planar == 2;

    // §3.5 data type and codecs.
    let bits = fmt0.bits;
    let dtype = match (fmt0.sf, bits) {
        (1, 8 | 16 | 32 | 64) => format!("uint{bits}"),
        (2, 8 | 16 | 32 | 64) => format!("int{bits}"),
        (3, 32 | 64) => format!("float{bits}"),
        (sf, b) => rej!("SampleFormat {sf} with BitsPerSample {b} is not supported"),
    };
    let item = (bits / 8) as u32;
    let mut codecs = Vec::new();
    let a2b_and_comp: Vec<J> = match (fmt0.comp, fmt0.pred) {
        (1, 1) => vec![bytes_codec(item, le)],
        (8 | 32946, 1) => vec![bytes_codec(item, le), zlib_codec()],
        (50000, 1) => vec![bytes_codec(item, le), zstd_codec()],
        (33003 | 33004 | 33005 | 34712, _) => vec![obj(vec![("name", s("imagecodecs_jpeg2k"))])],
        (c, p) => rej!("Compression {c} with Predictor {p} is not supported"),
    };

    // §3.3 planes. Index planes as (t, c, z) row-major.
    let (size_z, size_t, channels, cp);
    let mut plane_ifd: Vec<usize>;
    match &ome {
        None => {
            size_z = 1u64;
            size_t = 1u64;
            channels = spp;
            cp = 1u64;
            plane_ifd = vec![0];
        }
        Some(o) => {
            if let Some(ord) = &o.order {
                let ok = ord.len() == 5
                    && ord.starts_with("XY")
                    && {
                        let mut l: Vec<u8> = ord.as_bytes()[2..].to_vec();
                        l.sort();
                        l == b"CTZ"
                    };
                if !ok {
                    rej!("DimensionOrder {ord:?} is not XY followed by a permutation of ZCT");
                }
            }
            size_z = o.size_z.unwrap_or(1);
            size_t = o.size_t.unwrap_or(1);
            let mut size_c = o.size_c.unwrap_or(spp);
            if spp > 1 {
                if size_c == 1 {
                    size_c = spp;
                } else if size_c != spp {
                    rej!("SizeC {size_c} differs from SamplesPerPixel {spp}");
                }
            }
            channels = size_c;
            cp = if spp > 1 { 1 } else { size_c };

            // Multi-file datasets.
            let ids: HashSet<&String> = o.tiffdata.iter().filter_map(|td| td.uuid.as_ref()).collect();
            if ids.len() > 1 {
                rej!("TiffData UUIDs name {} distinct files", ids.len());
            }

            let nplanes = size_z
                .checked_mul(cp)
                .and_then(|v| v.checked_mul(size_t))
                .ok_or_else(|| E::Reject("plane count overflows".into()))?;
            let implicit = [TiffData { ifd: None, first: [None; 3], plane_count: None, uuid: None }];
            let tds: &[TiffData] = if o.tiffdata.is_empty() { &implicit } else { &o.tiffdata };
            // Each TiffData maps at most `main.len()` planes to existing IFDs.
            if nplanes > (tds.len() as u64).saturating_mul(main.len() as u64) {
                rej!("{nplanes} planes cannot all be mapped to the {} IFDs", main.len());
            }
            // Stepping order: letters after XY, fastest first.
            let ord = o.order.clone().unwrap_or_else(|| "XYZCT".to_string());
            let dims: Vec<(usize, u64)> = ord.as_bytes()[2..]
                .iter()
                .map(|&ch| match ch {
                    b'Z' => (0usize, size_z),
                    b'C' => (1usize, cp),
                    _ => (2usize, size_t),
                })
                .collect();
            let sizes = [size_z, cp, size_t];
            const UNMAPPED: usize = usize::MAX;
            const MISSING: usize = usize::MAX - 1;
            plane_ifd = vec![UNMAPPED; nplanes as usize];
            let single = tds.len() == 1;
            for td in tds {
                let mut pos = [0u64; 3];
                for k in 0..3 {
                    pos[k] = td.first[k].unwrap_or(0);
                    if pos[k] >= sizes[k] {
                        rej!("TiffData First{} = {} is out of range", ["Z", "C", "T"][k], pos[k]);
                    }
                }
                let count = match td.plane_count {
                    Some(c) => c,
                    None if single && td.ifd.is_none() => nplanes,
                    None => 1,
                };
                // Linear index in stepping order.
                let mut lin = 0u64;
                let mut mul = 1u64;
                for &(k, sz) in &dims {
                    lin += pos[k] * mul;
                    mul *= sz;
                }
                let mut ifd_idx = td.ifd.unwrap_or(0);
                let mut done = 0u64;
                while done < count && lin < nplanes {
                    let mut rem = lin;
                    let mut p = [0u64; 3];
                    for &(k, sz) in &dims {
                        p[k] = rem % sz;
                        rem /= sz;
                    }
                    let (z, c, tt) = (p[0], p[1], p[2]);
                    let idx = ((tt * cp + c) * size_z + z) as usize;
                    plane_ifd[idx] = if ifd_idx < main.len() as u64 { ifd_idx as usize } else { MISSING };
                    lin += 1;
                    done += 1;
                    ifd_idx = ifd_idx.saturating_add(1);
                }
            }
            for (i, &v) in plane_ifd.iter().enumerate() {
                if v == UNMAPPED || v == MISSING {
                    rej!("plane {i} is not mapped to an existing IFD");
                }
            }
        }
    }
    let nplanes = plane_ifd.len();

    // §3.4 levels: levels[level][plane] = &Ifd.
    let mut levels: Vec<Vec<&Ifd>> = vec![plane_ifd.iter().map(|&i| &main[i]).collect()];
    let ifd0_subs = t.uints(ifd0, 330)?.unwrap_or_default();
    if !ifd0_subs.is_empty() {
        let s_count = ifd0_subs.len();
        for k in 1..=s_count {
            let mut lv = Vec::with_capacity(nplanes);
            for &pi in &plane_ifd {
                if subs[pi].len() < s_count {
                    rej!("plane IFD {pi} has {} SubIFDs, fewer than {s_count}", subs[pi].len());
                }
                lv.push(&subs[pi][k - 1]);
            }
            levels.push(lv);
        }
    } else if ome.is_none() {
        let mut last_w = t.req(ifd0, 256, "ImageWidth")?;
        let mut last_h = t.req(ifd0, 257, "ImageLength")?;
        for ifd in &main[1..] {
            if !t.is_tiled(ifd) {
                continue;
            }
            let f = match t.format(ifd)? {
                Ok(f) => f,
                Err(_) => continue,
            };
            if f != fmt0 {
                continue;
            }
            let (w, hh) = match (t.uint(ifd, 256)?, t.uint(ifd, 257)?) {
                (Some(w), Some(hh)) => (w, hh),
                _ => continue,
            };
            if w < last_w && hh < last_h {
                levels.push(vec![ifd]);
                last_w = w;
                last_h = hh;
            }
        }
    }

    // Validate levels and collect geometry.
    struct Geo {
        w: u64,
        h: u64,
        tw: u64,
        tl: u64,
    }
    let mut geos: Vec<Geo> = Vec::new();
    for (li, lv) in levels.iter().enumerate() {
        let mut g: Option<Geo> = None;
        for ifd in lv {
            if !t.is_tiled(ifd) {
                rej!("an image of level {li} is not tiled");
            }
            let w = t.req(ifd, 256, "ImageWidth")?;
            let hh = t.req(ifd, 257, "ImageLength")?;
            let tw = t.req(ifd, 322, "TileWidth")?;
            let tl = t.req(ifd, 323, "TileLength")?;
            if ifd.find(325).is_none() {
                rej!("TileByteCounts is missing");
            }
            if t.strict_format(ifd)? != fmt0 {
                rej!("an image of level {li} does not have IFD 0's format");
            }
            if tw == 0 || tl == 0 {
                rej!("tile size is 0");
            }
            match &g {
                None => g = Some(Geo { w, h: hh, tw, tl }),
                Some(g0) => {
                    if g0.w != w || g0.h != hh || g0.tw != tw || g0.tl != tl {
                        rej!("planes of level {li} differ in size or tile size");
                    }
                }
            }
        }
        geos.push(g.unwrap());
    }

    // §3.6 output.
    let ch_axis = channels > 1;
    let mut axes: Vec<Axis> = Vec::new();
    let mut dims: Vec<&str> = Vec::new();
    let unit_for = |k: usize| -> Option<&'static str> {
        let o = ome.as_ref()?;
        o.phys[k]?;
        match &o.units[k] {
            None => Some("micrometer"),
            Some(sym) => unit_of(sym),
        }
    };
    if size_t > 1 {
        axes.push(Axis { name: "t", unit: None });
    }
    if ch_axis {
        axes.push(Axis { name: "c", unit: None });
    }
    if size_z > 1 {
        axes.push(Axis { name: "z", unit: unit_for(2) });
    }
    axes.push(Axis { name: "y", unit: unit_for(1) });
    axes.push(Axis { name: "x", unit: unit_for(0) });
    for a in &axes {
        dims.push(a.name);
    }
    let (px, py, pz) = match &ome {
        Some(o) => (o.phys[0].unwrap_or(1.0), o.phys[1].unwrap_or(1.0), o.phys[2].unwrap_or(1.0)),
        None => (1.0, 1.0, 1.0),
    };
    let (w0, h0) = (geos[0].w as f64, geos[0].h as f64);
    let mut scales = Vec::new();
    for g in &geos {
        let mut sc = Vec::new();
        if size_t > 1 {
            sc.push(1.0);
        }
        if ch_axis {
            sc.push(1.0);
        }
        if size_z > 1 {
            sc.push(pz);
        }
        sc.push(finite(py * finite(h0 / g.h as f64, "scale")?, "scale")?);
        sc.push(finite(px * finite(w0 / g.w as f64, "scale")?, "scale")?);
        scales.push(sc);
    }
    let name = ome.as_ref().and_then(|o| o.image_name.clone()).filter(|n| !n.is_empty());
    out.json("zarr.json", image_json(name, &axes, &scales, None));

    if interleaved {
        let ci = axes.iter().position(|a| a.name == "c").unwrap();
        codecs.push(transpose_codec(axes.len(), ci));
    }
    codecs.extend(a2b_and_comp);

    let sample_planes = if planar { spp } else { 1 };
    for (li, (lv, g)) in levels.iter().zip(&geos).enumerate() {
        let mut shape = Vec::new();
        let mut chunk = Vec::new();
        if size_t > 1 {
            shape.push(size_t);
            chunk.push(1);
        }
        if ch_axis {
            shape.push(channels);
            chunk.push(if interleaved { spp } else { 1 });
        }
        if size_z > 1 {
            shape.push(size_z);
            chunk.push(1);
        }
        shape.extend([g.h, g.w]);
        chunk.extend([g.tl, g.tw]);
        out.json(&format!("{li}/zarr.json"), array_json(&shape, &dtype, &chunk, codecs.clone(), &dims));

        let across = g.w.div_ceil(g.tw);
        let down = g.h.div_ceil(g.tl);
        let tcount = across
            .checked_mul(down)
            .ok_or_else(|| E::Reject("tile count overflows".into()))?;
        let expected = tcount
            .checked_mul(sample_planes)
            .ok_or_else(|| E::Reject("tile count overflows".into()))?;
        // Cache tile arrays per IFD (an IFD may be used by several planes).
        let mut cache: HashMap<*const Ifd, (Vec<u64>, Vec<u64>)> = HashMap::new();
        for tt in 0..size_t {
            for c in 0..cp {
                for z in 0..size_z {
                    let pidx = ((tt * cp + c) * size_z + z) as usize;
                    let ifd = lv[pidx];
                    let key = ifd as *const Ifd;
                    if !cache.contains_key(&key) {
                        let offs = t.uints(ifd, 324)?.unwrap();
                        let cnts = t.uints(ifd, 325)?.unwrap();
                        if offs.len() as u64 != expected || cnts.len() as u64 != expected {
                            rej!(
                                "TileOffsets/TileByteCounts have {}/{} values, expected {expected}",
                                offs.len(),
                                cnts.len()
                            );
                        }
                        cache.insert(key, (offs, cnts));
                    }
                    let (offs, cnts) = &cache[&key];
                    for sidx in 0..sample_planes {
                        for j in 0..tcount {
                            let k = (sidx * tcount + j) as usize;
                            let n = cnts[k];
                            if n == 0 {
                                continue;
                            }
                            let mut coords: Vec<u64> = Vec::new();
                            if size_t > 1 {
                                coords.push(tt);
                            }
                            if ch_axis {
                                coords.push(if planar {
                                    sidx
                                } else if interleaved {
                                    0
                                } else {
                                    c
                                });
                            }
                            if size_z > 1 {
                                coords.push(z);
                            }
                            coords.push(j / across);
                            coords.push(j % across);
                            let key = format!(
                                "{li}/c/{}",
                                coords.iter().map(|v| v.to_string()).collect::<Vec<_>>().join("/")
                            );
                            out.refs(key, vec![(offs[k], n)], src.size)?;
                        }
                    }
                }
            }
        }
    }

    if let Some(d) = d_bytes {
        out.entries.push(("OME/METADATA.ome.xml".to_string(), Entry::Bytes(d)));
    }
    Ok(())
}
