// §3 TIFF profile.

use crate::common::*;
use crate::http::Reader;
use crate::rej;
use crate::xml::{self, Tok};
use std::collections::{HashMap, HashSet};

const TAGS: [u16; 14] = [256, 257, 258, 259, 270, 277, 284, 317, 322, 323, 324, 325, 330, 339];
const SCALARS: [u16; 8] = [256, 257, 259, 277, 284, 317, 322, 323];

fn type_size(t: u16) -> Option<u64> {
    Some(match t {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 | 16 | 17 | 18 => 8,
        _ => return None,
    })
}

struct TagV {
    typ: u16,
    count: u64,
    pos: u64,       // absolute file offset of the value bytes
    ints: Vec<u64>, // values, for tags other than ImageDescription
}

struct Ifd {
    tags: HashMap<u16, TagV>,
}

impl Ifd {
    fn has(&self, t: u16) -> bool {
        self.tags.contains_key(&t)
    }
    fn scalar(&self, t: u16) -> Option<u64> {
        self.tags.get(&t).map(|v| v.ints[0])
    }
    fn array(&self, t: u16) -> Option<&[u64]> {
        self.tags.get(&t).map(|v| v.ints.as_slice())
    }
}

struct Tiff<'a> {
    r: &'a mut Reader,
    le: bool,
    big: bool,
    seen: HashSet<u64>,
    nread: u64,
}

fn uint(b: &[u8], le: bool) -> u64 {
    let mut v: u64 = 0;
    if le {
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

impl Tiff<'_> {
    fn check_offset(&mut self, off: u64) -> R<()> {
        let min = if self.big { 16 } else { 8 };
        if off < min {
            rej!("IFD offset {} below {}", off, min);
        }
        if off > MAX53 {
            rej!("IFD offset {} above 2^53-1", off);
        }
        if !self.seen.insert(off) {
            rej!("IFD offset {} read twice", off);
        }
        self.nread += 1;
        if self.nread > 100000 {
            rej!("more than 100000 IFDs");
        }
        Ok(())
    }

    /// Read an IFD; returns it and (for the main chain) its next-IFD offset.
    fn read_ifd(&mut self, off: u64, want_next: bool) -> R<(Ifd, u64)> {
        let le = self.le;
        let (cnt_size, esize, vsize) = if self.big { (8u64, 20u64, 8u64) } else { (2, 12, 4) };
        let count = uint(&self.r.read(off, cnt_size)?, le);
        let total = (count as u128) * (esize as u128);
        if total > self.r.size as u128 {
            rej!("IFD at {} with {} entries runs past the file", off, count);
        }
        let ebase = off + cnt_size;
        let ents = self.r.read(ebase, total as u64)?;
        let mut tags: HashMap<u16, TagV> = HashMap::new();
        for e in 0..count as usize {
            let eb = &ents[e * esize as usize..(e + 1) * esize as usize];
            let tag = uint(&eb[0..2], le) as u16;
            if !TAGS.contains(&tag) || tags.contains_key(&tag) {
                continue; // unknown tag, or a duplicate: ignored
            }
            let typ = uint(&eb[2..4], le) as u16;
            let ok = if tag == 270 {
                matches!(typ, 1..=13 | 16..=18)
            } else {
                matches!(typ, 1 | 3 | 4 | 13 | 16 | 18)
            };
            if !ok {
                rej!("tag {} has field type {}", tag, typ);
            }
            let tcount = if self.big { uint(&eb[4..12], le) } else { uint(&eb[4..8], le) };
            let vfield_pos = ebase + e as u64 * esize + if self.big { 12 } else { 8 };
            let tsize = type_size(typ).unwrap();
            let bytes = tcount as u128 * tsize as u128;
            let pos = if bytes <= vsize as u128 {
                vfield_pos
            } else {
                let vf = &eb[(esize - vsize) as usize..];
                let p = uint(vf, le);
                if p > MAX53 {
                    rej!("tag {} value offset {} above 2^53-1", tag, p);
                }
                p
            };
            if pos as u128 + bytes > self.r.size as u128 {
                rej!("tag {} value lies outside the file", tag);
            }
            if SCALARS.contains(&tag) && tcount < 1 {
                rej!("scalar tag {} has no values", tag);
            }
            let mut ints = Vec::new();
            if tag != 270 {
                let raw = self.r.read(pos, bytes as u64)?;
                ints.reserve(tcount as usize);
                for c in raw.chunks(tsize as usize) {
                    let v = uint(c, le);
                    if v > MAX53 {
                        rej!("tag {} value {} above 2^53-1", tag, v);
                    }
                    ints.push(v);
                }
            } else if matches!(typ, 16..=18) {
                let raw = self.r.read(pos, bytes as u64)?;
                for c in raw.chunks(8) {
                    let v = uint(c, le);
                    let mag = if typ == 17 { (v as i64).unsigned_abs() } else { v };
                    if mag > MAX53 {
                        rej!("ImageDescription integer value above 2^53-1 in magnitude");
                    }
                }
            }
            tags.insert(tag, TagV { typ, count: tcount, pos, ints });
        }
        let mut next = 0;
        if want_next {
            next = uint(&self.r.read(ebase + total as u64, vsize)?, le);
        }
        Ok((Ifd { tags }, next))
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Format {
    bps: u64,
    spp: u64,
    sf: u64,
    pc: u64,
    comp: u64,
    pred: u64,
}

fn format(ifd: &Ifd) -> R<Format> {
    let bps = match ifd.array(258) {
        Some(v) if !v.is_empty() => v,
        _ => rej!("BitsPerSample missing or empty"),
    };
    if bps.iter().any(|&b| b != bps[0]) || bps[0] < 1 {
        rej!("BitsPerSample values differ or are 0");
    }
    let spp = ifd.scalar(277).unwrap_or(1);
    if spp < 1 {
        rej!("SamplesPerPixel is 0");
    }
    let sf = match ifd.array(339) {
        None => 1,
        Some(v) => {
            if v.is_empty() {
                rej!("SampleFormat has no values");
            }
            if v.iter().any(|&x| x != v[0]) {
                rej!("SampleFormat values differ");
            }
            v[0]
        }
    };
    let pc = if spp == 1 {
        1
    } else {
        let p = ifd.scalar(284).unwrap_or(1);
        if p != 1 && p != 2 {
            rej!("PlanarConfiguration {}", p);
        }
        p
    };
    Ok(Format {
        bps: bps[0],
        spp,
        sf,
        pc,
        comp: ifd.scalar(259).unwrap_or(1),
        pred: ifd.scalar(317).unwrap_or(1),
    })
}

struct Img<'a> {
    w: u64,
    h: u64,
    tw: u64,
    th: u64,
    fmt: Format,
    offsets: &'a [u64],
    counts: &'a [u64],
}

/// Format and size of an IFD that is a plane, a level or a candidate; it must be tiled.
fn image(ifd: &Ifd) -> R<Img<'_>> {
    let fmt = format(ifd)?;
    let (w, h) = match (ifd.scalar(256), ifd.scalar(257)) {
        (Some(w), Some(h)) if w >= 1 && h >= 1 => (w, h),
        _ => rej!("ImageWidth/ImageLength missing or 0"),
    };
    let tiled = ifd.has(322) && ifd.has(324);
    if !tiled {
        rej!("image is not tiled");
    }
    let (tw, th) = match (ifd.scalar(322), ifd.scalar(323)) {
        (Some(a), Some(b)) if a >= 1 && b >= 1 => (a, b),
        _ => rej!("TileWidth/TileLength missing or 0"),
    };
    let (offsets, counts) = match (ifd.array(324), ifd.array(325)) {
        (Some(o), Some(c)) => (o, c),
        _ => rej!("TileOffsets/TileByteCounts missing"),
    };
    Ok(Img { w, h, tw, th, fmt, offsets, counts })
}

struct Ome {
    image_name: Option<String>,
    pixels: Option<xml::Tag>,
    tiffdata: Vec<(xml::Tag, Option<String>)>, // tag, file identifier named by its UUID
}

fn parse_ome(x: &str) -> Option<Ome> {
    let toks = xml::scan(x);
    let tags: Vec<&xml::Tag> = toks
        .iter()
        .filter_map(|t| match t {
            Tok::Tag(t) => Some(t),
            _ => None,
        })
        .collect();
    if !tags.iter().any(|t| !t.is_end && t.name == "OME") {
        return None;
    }
    let image_name = tags.iter().find(|t| !t.is_end && t.name == "Image").and_then(|t| t.attr("Name"));
    let pi = tags.iter().position(|t| !t.is_end && t.name == "Pixels");
    let mut tiffdata = Vec::new();
    let mut pixels = None;
    if let Some(pi) = pi {
        let p = tags[pi];
        pixels = Some(clone_tag(p));
        if !p.self_closing {
            let mut j = pi + 1;
            while j < tags.len() {
                let t = tags[j];
                if t.is_end && t.name == "Pixels" {
                    break;
                }
                if !t.is_end && t.name == "TiffData" {
                    let mut ident = None;
                    if !t.self_closing {
                        // first UUID start tag before the next TiffData end/start or Pixels end tag
                        let mut k = j + 1;
                        while k < tags.len() {
                            let u = tags[k];
                            if (u.name == "TiffData") || (u.is_end && u.name == "Pixels") {
                                break;
                            }
                            if !u.is_end && u.name == "UUID" {
                                ident = Some(match u.attr("FileName") {
                                    Some(f) => f,
                                    None => uuid_text(x, &toks, u),
                                });
                                break;
                            }
                            k += 1;
                        }
                    }
                    tiffdata.push((clone_tag(t), ident));
                }
                j += 1;
            }
        }
    }
    Some(Ome { image_name, pixels, tiffdata })
}

fn clone_tag(t: &xml::Tag) -> xml::Tag {
    xml::Tag {
        start: t.start,
        end: t.end,
        is_end: t.is_end,
        self_closing: t.self_closing,
        name: t.name.clone(),
        attrs: t.attrs.clone(),
    }
}

fn uuid_text(x: &str, toks: &[Tok], u: &xml::Tag) -> String {
    if u.self_closing {
        return String::new();
    }
    // Find the next tag after u; collect text with skipped sections removed.
    let mut raw = String::new();
    let mut pos = u.end;
    let mut found_end = x.len();
    for t in toks {
        match t {
            Tok::Skip(s, e) if *s >= u.end => {
                raw.push_str(&x[pos..*s]);
                pos = *e;
            }
            Tok::Tag(t) if t.start >= u.end => {
                found_end = t.start;
                break;
            }
            _ => {}
        }
    }
    if pos < found_end {
        raw.push_str(&x[pos..found_end]);
    }
    let d = xml::decode_refs(&raw);
    d.trim_matches(|c| matches!(c, ' ' | '\t' | '\r' | '\n')).to_string()
}

fn int_attr(t: &xml::Tag, name: &str, min: u64) -> R<Option<u64>> {
    let v = match t.attr(name) {
        None => return Ok(None),
        Some(v) => v,
    };
    let s = v.trim_matches(|c| matches!(c, ' ' | '\t' | '\r' | '\n'));
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        rej!("attribute {}={:?} is not an integer", name, v);
    }
    let n = s.trim_start_matches('0');
    if n.len() > 16 {
        rej!("attribute {}={:?} too large", name, v);
    }
    let n: u64 = if n.is_empty() { 0 } else { n.parse().unwrap() };
    if n > MAX53 {
        rej!("attribute {}={:?} too large", name, v);
    }
    if n < min {
        rej!("attribute {}={:?} below {}", name, v, min);
    }
    Ok(Some(n))
}

fn phys_size(t: Option<&xml::Tag>, name: &str) -> Option<f64> {
    let v = t?.attr(name)?;
    let b = v.as_bytes();
    let mut i = 0;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    let ds = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    let int_digits = i - ds;
    if int_digits > 0 {
        if i < b.len() && b[i] == b'.' {
            i += 1;
            while i < b.len() && b[i].is_ascii_digit() {
                i += 1;
            }
        }
    } else {
        if i < b.len() && b[i] == b'.' {
            i += 1;
        } else {
            return None;
        }
        let fs = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == fs {
            return None;
        }
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        let es = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == es {
            return None;
        }
    }
    if i != b.len() {
        return None;
    }
    let f: f64 = parse_decimal(&v)?;
    if f.is_finite() && f > 0.0 { Some(f) } else { None }
}

/// Correctly rounded conversion of a string already matched by the PhysicalSize grammar.
fn parse_decimal(v: &str) -> Option<f64> {
    // Rust's parser accepts this grammar except possibly "1." forms with exponent;
    // normalize "1.e5" -> "1e5" and "1." -> "1" to be safe.
    let s = v.replace(".e", "e").replace(".E", "E");
    let s = s.strip_suffix('.').unwrap_or(&s).to_string();
    s.parse::<f64>().ok()
}

fn unit(sym: &str) -> Option<&'static str> {
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

pub fn virtualize(r: &mut Reader) -> R<Output> {
    let h = r.read(0, 8)?;
    let le = h[0] == b'I';
    let magic = uint(&h[2..4], le);
    let big = magic == 43;
    let first = if big {
        if uint(&h[4..6], le) != 8 || uint(&h[6..8], le) != 0 {
            rej!("BigTIFF header with offset size != 8 or reserved != 0");
        }
        uint(&r.read(8, 8)?, le)
    } else {
        uint(&h[4..8], le)
    };
    let mut t = Tiff { r, le, big, seen: HashSet::new(), nread: 0 };

    // §3.1 IFDs
    if first == 0 {
        rej!("no IFDs");
    }
    let mut main: Vec<Ifd> = Vec::new();
    let mut off = first;
    while off != 0 {
        t.check_offset(off)?;
        let (ifd, next) = t.read_ifd(off, true)?;
        main.push(ifd);
        off = next;
    }
    let mut subs: Vec<Vec<Ifd>> = Vec::new();
    for i in 0..main.len() {
        let offs: Vec<u64> = main[i].array(330).map(|v| v.to_vec()).unwrap_or_default();
        let mut v = Vec::new();
        for o in offs {
            t.check_offset(o)?;
            v.push(t.read_ifd(o, false)?.0);
        }
        subs.push(v);
    }
    let r = t.r;
    let fsize = r.size;

    let ifd0 = &main[0];
    let fmt0 = format(ifd0)?;
    let spp = fmt0.spp;

    // §3.2 OME-XML
    let mut d_bytes: Option<Vec<u8>> = None;
    let mut ome: Option<Ome> = None;
    if let Some(tv) = ifd0.tags.get(&270) {
        if tv.typ == 2 {
            let raw = r.read(tv.pos, tv.count)?;
            let d: Vec<u8> = match raw.iter().position(|&b| b == 0) {
                Some(p) => raw[..p].to_vec(),
                None => raw,
            };
            if let Ok(x) = std::str::from_utf8(&d) {
                if let Some(o) = parse_ome(x) {
                    ome = Some(o);
                    d_bytes = Some(d.clone());
                }
            }
        }
    }

    // §3.3 planes
    let (size_z, size_c, size_t, cp, planes): (u64, u64, u64, u64, Vec<u64>);
    let (mut px, mut py, mut pz) = (None, None, None);
    let (mut ux, mut uy, mut uz) = (None, None, None);
    let mut name = None;
    match &ome {
        None => {
            size_z = 1;
            size_t = 1;
            size_c = spp;
            cp = 1;
            planes = vec![0];
        }
        Some(o) => {
            let p = o.pixels.as_ref();
            let sz = match p {
                Some(p) => int_attr(p, "SizeZ", 1)?,
                None => None,
            };
            let sc = match p {
                Some(p) => int_attr(p, "SizeC", 1)?,
                None => None,
            };
            let st = match p {
                Some(p) => int_attr(p, "SizeT", 1)?,
                None => None,
            };
            let dim_order = match p.and_then(|p| p.attr("DimensionOrder")) {
                None => "XYZCT".to_string(),
                Some(d) => {
                    let ok = d.len() == 5 && d.starts_with("XY") && {
                        let mut l: Vec<char> = d[2..].chars().collect();
                        l.sort();
                        l == vec!['C', 'T', 'Z']
                    };
                    if !ok {
                        rej!("bad DimensionOrder {:?}", d);
                    }
                    d
                }
            };
            px = phys_size(p, "PhysicalSizeX");
            py = phys_size(p, "PhysicalSizeY");
            pz = phys_size(p, "PhysicalSizeZ");
            let u = |n: &str| -> Option<&'static str> {
                match p.and_then(|p| p.attr(n)) {
                    None => Some("micrometer"),
                    Some(s) => unit(&s),
                }
            };
            ux = px.and(u("PhysicalSizeXUnit"));
            uy = py.and(u("PhysicalSizeYUnit"));
            uz = pz.and(u("PhysicalSizeZUnit"));
            name = o.image_name.clone().filter(|n| !n.is_empty());

            size_z = sz.unwrap_or(1);
            size_t = st.unwrap_or(1);
            let mut c = sc.unwrap_or(spp);
            if spp > 1 {
                if c == 1 {
                    c = spp;
                } else if c != spp {
                    rej!("SizeC {} differs from SamplesPerPixel {}", c, spp);
                }
            }
            size_c = c;
            cp = if spp > 1 { 1 } else { size_c };
            let count = size_z as u128 * cp as u128 * size_t as u128;
            if count > 100000 {
                rej!("{} planes (more than 100000)", count);
            }
            let count = count as u64;
            // Multi-file check.
            let mut files: HashSet<&str> = HashSet::new();
            for (_, id) in &o.tiffdata {
                if let Some(id) = id {
                    files.insert(id.as_str());
                }
            }
            if files.len() > 1 {
                rej!("TiffData elements name {} files", files.len());
            }
            // Stepping order.
            let letters: Vec<char> = dim_order[2..].chars().collect();
            let dsize = |c: char| match c {
                'Z' => size_z,
                'C' => cp,
                _ => size_t,
            };
            let mut map: Vec<Option<u64>> = vec![None; count as usize];
            let implicit = xml::Tag {
                start: 0,
                end: 0,
                is_end: false,
                self_closing: true,
                name: "TiffData".into(),
                attrs: vec![],
            };
            let tds: Vec<&xml::Tag> = if o.tiffdata.is_empty() {
                vec![&implicit]
            } else {
                o.tiffdata.iter().map(|(t, _)| t).collect()
            };
            let ntd = tds.len();
            for td in tds {
                let fz = int_attr(td, "FirstZ", 0)?.unwrap_or(0);
                let fc = int_attr(td, "FirstC", 0)?.unwrap_or(0);
                let ft = int_attr(td, "FirstT", 0)?.unwrap_or(0);
                let ifd_attr = int_attr(td, "IFD", 0)?;
                let pc_attr = int_attr(td, "PlaneCount", 1)?;
                if fz >= size_z || fc >= cp || ft >= size_t {
                    rej!("TiffData First* out of range");
                }
                let ifd = ifd_attr.unwrap_or(0);
                let pcount = match pc_attr {
                    Some(v) => v,
                    None => {
                        if ntd == 1 && ifd_attr.is_none() {
                            count
                        } else {
                            1
                        }
                    }
                };
                let first = |c: char| match c {
                    'Z' => fz,
                    'C' => fc,
                    _ => ft,
                };
                let start = first(letters[0])
                    + dsize(letters[0]) * (first(letters[1]) + dsize(letters[1]) * first(letters[2]));
                let mut i = 0u64;
                while i < pcount {
                    let pos = start + i;
                    if pos >= count {
                        break;
                    }
                    // decompose pos (first letter fastest)
                    let mut rem = pos;
                    let (mut z, mut c, mut tt) = (0, 0, 0);
                    for &l in &letters {
                        let s = dsize(l);
                        let v = rem % s;
                        rem /= s;
                        match l {
                            'Z' => z = v,
                            'C' => c = v,
                            _ => tt = v,
                        }
                    }
                    let pidx = ((tt * cp + c) * size_z + z) as usize;
                    map[pidx] = Some(ifd + i);
                    i += 1;
                }
            }
            let mut pl = Vec::with_capacity(map.len());
            for m in map {
                match m {
                    Some(i) if i < main.len() as u64 => pl.push(i),
                    _ => rej!("a plane is not mapped to an existing IFD"),
                }
            }
            planes = pl;
        }
    }

    // §3.4 levels: each level is a list of IFDs, one per plane.
    let mut levels: Vec<Vec<&Ifd>> = vec![planes.iter().map(|&i| &main[i as usize]).collect()];
    let s = ifd0.array(330).map(|v| v.len()).unwrap_or(0);
    if s > 0 {
        for k in 1..=s {
            let mut lv = Vec::new();
            for &pi in &planes {
                let sv = &subs[pi as usize];
                if sv.len() < s {
                    rej!("plane IFD {} has fewer than {} SubIFDs", pi, s);
                }
                lv.push(&sv[k - 1]);
            }
            levels.push(lv);
        }
    } else if ome.is_none() {
        let l0 = image(&main[0])?;
        let (mut lw, mut lh) = (l0.w, l0.h);
        for ifd in &main[1..] {
            if ifd.has(322) && ifd.has(324) && ifd.has(258) {
                let im = image(ifd)?;
                if im.fmt == fmt0 && im.w < lw && im.h < lh {
                    lw = im.w;
                    lh = im.h;
                    levels.push(vec![ifd]);
                }
            }
        }
    }
    let mut limgs: Vec<Vec<Img>> = Vec::new();
    for lv in &levels {
        let mut imgs = Vec::new();
        for ifd in lv {
            let im = image(ifd)?;
            if im.fmt != fmt0 {
                rej!("an image's format differs from IFD 0's");
            }
            imgs.push(im);
        }
        let a = &imgs[0];
        for b in &imgs[1..] {
            if (b.w, b.h, b.tw, b.th) != (a.w, a.h, a.tw, a.th) {
                rej!("planes of a level differ in size");
            }
        }
        limgs.push(imgs);
    }

    // §3.5 data type and codecs
    let kind = match fmt0.sf {
        1 => "uint",
        2 => "int",
        3 => "float",
        v => rej!("SampleFormat {}", v),
    };
    let ok_bits = if kind == "float" { matches!(fmt0.bps, 32 | 64) } else { matches!(fmt0.bps, 8 | 16 | 32 | 64) };
    if !ok_bits {
        rej!("BitsPerSample {} for {}", fmt0.bps, kind);
    }
    let dtype = format!("{}{}", kind, fmt0.bps);
    let interleaved = spp > 1 && fmt0.pc == 1;
    let planar = spp > 1 && fmt0.pc == 2;
    let item = fmt0.bps / 8;

    // §3.6 axes
    let chan = size_c;
    let mut dims: Vec<&'static str> = Vec::new();
    if size_t > 1 {
        dims.push("t");
    }
    if chan > 1 {
        dims.push("c");
    }
    if size_z > 1 {
        dims.push("z");
    }
    dims.push("y");
    dims.push("x");
    let dim_strings: Vec<String> = dims.iter().map(|d| d.to_string()).collect();

    let mut codecs = Vec::new();
    if interleaved {
        codecs.push(transpose_codec(&dim_strings));
    }
    match (fmt0.comp, fmt0.pred) {
        (1, 1) => codecs.push(bytes_codec(item, le)),
        (8 | 32946, 1) => {
            codecs.push(bytes_codec(item, le));
            codecs.push(zlib_codec());
        }
        (50000, 1) => {
            codecs.push(bytes_codec(item, le));
            codecs.push(zstd_codec());
        }
        (33003 | 33004 | 33005 | 34712, _) => {
            codecs.push(Json::obj(vec![("name", Json::s("imagecodecs_jpeg2k"))]));
        }
        (c, p) => rej!("Compression {} with Predictor {}", c, p),
    }

    let mut out = Output { entries: Vec::new() };
    let (w0, h0) = (limgs[0][0].w, limgs[0][0].h);
    let mut axes: Vec<Axis> = Vec::new();
    for &d in &dims {
        let (u, mut scales) = match d {
            "z" => (uz, vec![]),
            "y" => (uy, vec![]),
            "x" => (ux, vec![]),
            _ => (None, vec![]),
        };
        for imgs in &limgs {
            let im = &imgs[0];
            let v = match d {
                "y" => py.unwrap_or(1.0) * (h0 as f64 / im.h as f64),
                "x" => px.unwrap_or(1.0) * (w0 as f64 / im.w as f64),
                "z" => pz.unwrap_or(1.0),
                _ => 1.0,
            };
            scales.push(v);
        }
        axes.push(Axis { name: d, unit: u, scales });
    }
    out.entries.push((
        "zarr.json".into(),
        Entry::Json(image_json(name.as_deref(), &axes, limgs.len(), None)?),
    ));

    let nsp = if planar { spp } else { 1 };
    for (li, imgs) in limgs.iter().enumerate() {
        let im0 = &imgs[0];
        let mut shape = Vec::new();
        let mut chunk = Vec::new();
        for &d in &dims {
            match d {
                "t" => {
                    shape.push(size_t);
                    chunk.push(1);
                }
                "c" => {
                    shape.push(chan);
                    chunk.push(if interleaved { spp } else { 1 });
                }
                "z" => {
                    shape.push(size_z);
                    chunk.push(1);
                }
                "y" => {
                    shape.push(im0.h);
                    chunk.push(im0.th);
                }
                _ => {
                    shape.push(im0.w);
                    chunk.push(im0.tw);
                }
            }
        }
        let meta = ArrayMeta {
            shape,
            data_type: dtype.clone(),
            chunk_shape: chunk,
            codecs: codecs.clone(),
            dims: dim_strings.clone(),
        };
        out.entries.push((format!("{}/zarr.json", li), Entry::Json(array_json(&meta))));
        let rows = im0.h.div_ceil(im0.th);
        let cols = im0.w.div_ceil(im0.tw);
        let tcount = rows as u128 * cols as u128;
        for (pi, im) in imgs.iter().enumerate() {
            // plane index pi = (t*cp + c)*size_z + z
            let z = pi as u64 % size_z;
            let c = (pi as u64 / size_z) % cp;
            let tt = pi as u64 / size_z / cp;
            let need = tcount * nsp as u128;
            if im.offsets.len() as u128 != need || im.counts.len() as u128 != need {
                rej!("TileOffsets/TileByteCounts have the wrong count");
            }
            for sidx in 0..nsp {
                for j in 0..tcount as u64 {
                    let k = (sidx * tcount as u64 + j) as usize;
                    let n = im.counts[k];
                    if n == 0 {
                        continue;
                    }
                    let mut key = format!("{}/c", li);
                    for &d in &dims {
                        let v = match d {
                            "t" => tt,
                            "c" => {
                                if interleaved {
                                    0
                                } else if planar {
                                    sidx
                                } else {
                                    c
                                }
                            }
                            "z" => z,
                            "y" => j / cols,
                            _ => j % cols,
                        };
                        key.push('/');
                        key.push_str(&v.to_string());
                    }
                    out.entries.push((key, Entry::Ranges(vec![(im.offsets[k], n)])));
                }
            }
        }
    }
    if let Some(d) = d_bytes {
        out.entries.push(("OME/METADATA.ome.xml".into(), Entry::Bytes(d)));
    }
    check_output(&out, fsize)?;
    Ok(out)
}
