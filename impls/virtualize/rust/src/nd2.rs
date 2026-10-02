//! ND2 profile (VIRTUALIZE.md §4).

use std::collections::HashMap;
use std::io::Read;

use crate::common::*;
use crate::rej;

// ------------------------------------------------------------------ LV (§4.2)

#[derive(Clone, Debug)]
enum V {
    Bool(u8),
    /// Integer of LV type 2..=5.
    Int(i128),
    F64(f64),
    Ptr,
    Str(String),
    Bytes(Vec<u8>),
    /// A byte of a byte array, as a member of its list.
    Byte(u8),
    Level(Vec<(String, V)>),
}

fn le_u32(b: &[u8]) -> u32 {
    u32::from_le_bytes(b[..4].try_into().unwrap())
}
fn le_u64(b: &[u8]) -> u64 {
    u64::from_le_bytes(b[..8].try_into().unwrap())
}

struct Lv<'a> {
    d: &'a [u8],
}

impl Lv<'_> {
    fn need(&self, pos: usize, n: usize) -> R<()> {
        if pos.checked_add(n).is_none_or(|e| e > self.d.len()) {
            rej!("LV record is truncated");
        }
        Ok(())
    }

    /// Parses one record at `pos`; returns (name, value, next position).
    fn record(&self, pos: usize, depth: usize) -> R<(String, V, usize)> {
        if depth > 10000 {
            rej!("LV nesting too deep");
        }
        let start = pos;
        self.need(pos, 2)?;
        let typ = self.d[pos];
        let k = self.d[pos + 1] as usize;
        let mut p = pos + 2;
        self.need(p, 2 * k)?;
        let units: Vec<u16> = self.d[p..p + 2 * k]
            .chunks(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .collect();
        let end = units.iter().position(|&u| u == 0).unwrap_or(units.len());
        let name = String::from_utf16_lossy(&units[..end]);
        p += 2 * k;
        let v = match typ {
            1 => {
                self.need(p, 1)?;
                p += 1;
                V::Bool(self.d[p - 1])
            }
            2 => {
                self.need(p, 4)?;
                p += 4;
                V::Int(i32::from_le_bytes(self.d[p - 4..p].try_into().unwrap()) as i128)
            }
            3 => {
                self.need(p, 4)?;
                p += 4;
                V::Int(le_u32(&self.d[p - 4..]) as i128)
            }
            4 => {
                self.need(p, 8)?;
                p += 8;
                V::Int(i64::from_le_bytes(self.d[p - 8..p].try_into().unwrap()) as i128)
            }
            5 => {
                self.need(p, 8)?;
                p += 8;
                V::Int(le_u64(&self.d[p - 8..]) as i128)
            }
            6 => {
                self.need(p, 8)?;
                p += 8;
                V::F64(f64::from_le_bytes(self.d[p - 8..p].try_into().unwrap()))
            }
            7 => {
                self.need(p, 8)?;
                p += 8;
                V::Ptr
            }
            8 => {
                let mut units = Vec::new();
                loop {
                    self.need(p, 2)?;
                    let u = u16::from_le_bytes([self.d[p], self.d[p + 1]]);
                    p += 2;
                    if u == 0 {
                        break;
                    }
                    units.push(u);
                }
                V::Str(String::from_utf16_lossy(&units))
            }
            9 => {
                self.need(p, 8)?;
                let b = le_u64(&self.d[p..]);
                p += 8;
                if b > self.d.len() as u64 {
                    rej!("LV byte array is truncated");
                }
                let b = b as usize;
                self.need(p, b)?;
                p += b;
                V::Bytes(self.d[p - b..p].to_vec())
            }
            11 => {
                self.need(p, 12)?;
                let c = le_u32(&self.d[p..]) as u64;
                let l = le_u64(&self.d[p + 4..]);
                p += 12;
                let mut recs = Vec::new();
                for _ in 0..c {
                    let (n, v, np) = self.record(p, depth + 1)?;
                    recs.push((n, v));
                    p = np;
                }
                if (p - start) as u64 != l {
                    rej!("LV level ends at {} bytes from its start, not {l}", p - start);
                }
                let skip = 8 * c;
                if skip > self.d.len() as u64 {
                    rej!("LV level is truncated");
                }
                self.need(p, skip as usize)?;
                p += skip as usize;
                V::Level(recs)
            }
            t => rej!("LV record type {t} is not supported"),
        };
        Ok((name, v, p))
    }

    fn records(&self) -> R<Vec<(String, V)>> {
        let mut p = 0;
        let mut recs = Vec::new();
        while p < self.d.len() {
            let (n, v, np) = self.record(p, 0)?;
            recs.push((n, v));
            p = np;
        }
        Ok(recs)
    }
}

/// Decodes a metadata chunk's data (handling the compressed record, type 76).
fn decode_lv(data: &[u8]) -> R<V> {
    if !data.is_empty() && data[0] == 76 {
        if data.len() < 12 {
            rej!("compressed LV record is truncated");
        }
        let stream = &data[12..];
        let mut dec = flate2::read::ZlibDecoder::new(stream);
        let mut out = Vec::new();
        if let Err(e) = dec.read_to_end(&mut out) {
            rej!("compressed LV record does not inflate: {e}");
        }
        if dec.total_in() != stream.len() as u64 {
            rej!("zlib stream of compressed LV record does not end at the end of the chunk");
        }
        if !out.is_empty() && out[0] == 76 {
            rej!("nested compressed LV record");
        }
        return Ok(V::Level(Lv { d: &out }.records()?));
    }
    Ok(V::Level(Lv { d: data }.records()?))
}

// Value access (§4.2 "Values").

fn is_list(recs: &[(String, V)]) -> bool {
    !recs.is_empty() && recs.iter().all(|(n, _)| n.is_empty())
}

/// The member `name` of an object (last value of a repeated name).
fn get<'a>(v: &'a V, name: &str) -> R<Option<&'a V>> {
    match v {
        V::Level(recs) => {
            if is_list(recs) {
                return Ok(None);
            }
            Ok(recs.iter().rev().find(|(n, _)| n == name).map(|(_, v)| v))
        }
        _ => rej!("cannot look up member {name:?} of a non-level value"),
    }
}

fn path<'a>(v: &'a V, p: &str) -> R<Option<&'a V>> {
    let mut cur = v;
    for part in p.split('/') {
        match get(cur, part)? {
            Some(x) => cur = x,
            None => return Ok(None),
        }
    }
    Ok(Some(cur))
}

/// The members of an object or list (or the bytes of a byte array).
fn members(v: &V) -> R<Vec<V>> {
    match v {
        V::Level(recs) => {
            if is_list(recs) {
                return Ok(recs.iter().map(|(_, v)| v.clone()).collect());
            }
            let mut order: Vec<&String> = Vec::new();
            let mut last: HashMap<&String, &V> = HashMap::new();
            for (n, v) in recs {
                if !last.contains_key(n) {
                    order.push(n);
                }
                last.insert(n, v);
            }
            Ok(order.into_iter().map(|n| last[n].clone()).collect())
        }
        V::Bytes(b) => Ok(b.iter().map(|&x| V::Byte(x)).collect()),
        _ => rej!("value has no members"),
    }
}

fn number(v: &V, what: &str) -> R<f64> {
    match v {
        V::Int(i) => Ok(*i as f64),
        V::F64(f) => Ok(*f),
        V::Byte(b) => Ok(*b as f64),
        _ => rej!("{what} is not a number"),
    }
}

/// A number used as a non-negative integer.
fn integer(v: &V, what: &str) -> R<u64> {
    match v {
        V::Int(i) => {
            if *i < 0 || *i > u64::MAX as i128 {
                rej!("{what} = {i} is out of range");
            }
            Ok(*i as u64)
        }
        V::Byte(b) => Ok(*b as u64),
        V::F64(f) => {
            if !(f.is_finite() && *f >= 0.0 && f.fract() == 0.0 && *f <= MAX_SAFE as f64) {
                rej!("{what} = {f} is not a non-negative integer");
            }
            Ok(*f as u64)
        }
        _ => rej!("{what} is not a number"),
    }
}

fn flag(v: &V, what: &str) -> R<bool> {
    match v {
        V::Bool(b) => Ok(*b != 0),
        V::Int(i) => Ok(*i != 0),
        V::Byte(b) => Ok(*b != 0),
        _ => rej!("{what} is not a flag"),
    }
}

fn num_at(v: &V, p: &str, default: Option<f64>) -> R<f64> {
    match path(v, p)? {
        Some(x) => number(x, p),
        None => match default {
            Some(d) => Ok(d),
            None => rej!("required member {p} is missing"),
        },
    }
}

fn int_at(v: &V, p: &str, default: Option<u64>) -> R<u64> {
    match path(v, p)? {
        Some(x) => integer(x, p),
        None => match default {
            Some(d) => Ok(d),
            None => rej!("required member {p} is missing"),
        },
    }
}

// ------------------------------------------------------------------ chunks (§4.1)

struct Nd2<'a> {
    src: &'a Source,
}

const MAGIC: u32 = 0x0ABE_CEDA;

impl Nd2<'_> {
    /// Reads a chunk header: (name length n, data length d).
    fn header(&self, o: u64) -> R<(u64, u64)> {
        safe(o, "chunk offset")?;
        let h = self.src.read(o, 16)?;
        if le_u32(&h) != MAGIC {
            rej!("no chunk magic at offset {o}");
        }
        let n = le_u32(&h[4..]) as u64;
        let d = safe(le_u64(&h[8..]), "chunk data length")?;
        Ok((n, d))
    }

    fn chunk_data(&self, o: u64) -> R<Vec<u8>> {
        let (n, d) = self.header(o)?;
        let start = o + 16 + n;
        safe(start, "chunk data offset")?;
        self.src.read(start, d)
    }
}

const MAP_SIG: &[u8] = b"ND2 CHUNK MAP SIGNATURE 0000001!";

// ------------------------------------------------------------------ main

#[derive(Clone, Copy, PartialEq, Debug)]
enum Kind {
    Time,
    Position,
    Z,
}

#[derive(Clone, Debug)]
struct Loop {
    etype: u64,
    kind: Kind,
    count: u64,
    depth: usize,
    /// Period (ms) for time loops, step (µm) for z loops.
    param: f64,
}

fn valid(validity: Option<&V>, i: usize, what: &str) -> R<bool> {
    match validity {
        None => Ok(true),
        Some(v) => {
            let m = members(v)?;
            match m.get(i) {
                None => Ok(false),
                Some(x) => flag(x, what),
            }
        }
    }
}

fn flatten(node: &V, depth: usize, loops: &mut Vec<Loop>) -> R<()> {
    let et = int_at(node, "eType", None)?;
    if !matches!(et, 1 | 2 | 4 | 6 | 8) {
        rej!("experiment eType {et} is not supported");
    }
    let pars = path(node, "uLoopPars")?;
    let pars = match pars {
        None => return Ok(()),
        Some(p) => p,
    };
    let (count, param) = match et {
        1 => (int_at(pars, "uiCount", Some(0))?, num_at(pars, "dPeriod", Some(0.0))?),
        8 => {
            let vv = path(pars, "pPeriodValid")?;
            let mut sum = 0u64;
            let mut period: Option<f64> = None;
            if let Some(pp) = path(pars, "pPeriod")? {
                for (i, p) in members(pp)?.iter().enumerate() {
                    if valid(vv, i, "pPeriodValid member")? {
                        sum = sum
                            .checked_add(int_at(p, "uiCount", None)?)
                            .ok_or_else(|| E::Reject("count overflows".into()))?;
                        if period.is_none() {
                            period = Some(num_at(p, "dPeriod", Some(0.0))?);
                        }
                    }
                }
            }
            (sum, period.unwrap_or(0.0))
        }
        2 => {
            let vv = path(node, "pItemValid")?;
            let mut n = 0u64;
            if let Some(pts) = path(pars, "Points")? {
                for i in 0..members(pts)?.len() {
                    if valid(vv, i, "pItemValid member")? {
                        n += 1;
                    }
                }
            }
            (n, 0.0)
        }
        4 => {
            let count = int_at(pars, "uiCount", Some(0))?;
            let mut step = num_at(pars, "dZStep", Some(0.0))?.abs();
            if step == 0.0 && count > 1 {
                let hi = num_at(pars, "dZHigh", Some(0.0))?;
                let lo = num_at(pars, "dZLow", Some(0.0))?;
                step = finite((hi - lo).abs() / (count - 1) as f64, "z step")?;
            }
            (count, finite(step, "z step")?)
        }
        _ => {
            // 6: spectral
            let c = match path(pars, "uiCount")? {
                Some(v) => integer(v, "uiCount")?,
                None => int_at(pars, "pPlanes/uiCount", Some(0))?,
            };
            (c, 0.0)
        }
    };
    if count == 0 {
        return Ok(());
    }
    let child_depth = if et == 6 {
        depth
    } else {
        let kind = match et {
            1 | 8 => Kind::Time,
            2 => Kind::Position,
            _ => Kind::Z,
        };
        let lp = Loop { etype: et, kind, count, depth, param };
        match loops.last() {
            None => loops.push(lp),
            Some(last) if last.depth < depth => loops.push(lp),
            Some(last) if last.depth == depth && last.etype == et && last.count < count => {
                *loops.last_mut().unwrap() = lp;
            }
            _ => {}
        }
        depth + 1
    };
    if let Some(next) = path(node, "ppNextLevelEx")? {
        for child in members(next)? {
            flatten(&child, child_depth, loops)?;
        }
    }
    Ok(())
}

fn hex_color(rgb: u32) -> String {
    format!("{:06X}", rgb & 0xFFFFFF)
}

pub fn virtualize(src: &Source, out: &mut Output) -> R<()> {
    let nd = Nd2 { src };

    // Signature.
    let (n, d) = nd.header(0)?;
    if n != 32 || d != 64 {
        rej!("signature chunk has name length {n} and data length {d}");
    }
    let name = src.read(16, 32)?;
    if name != b"ND2 FILE SIGNATURE CHUNK NAME01!" {
        rej!("bad signature chunk name");
    }
    let sig = src.read(48, 64)?;
    if !sig.starts_with(b"Ver") {
        rej!("signature data does not start with Ver");
    }
    let digits: Vec<u8> = sig[3..].iter().take_while(|b| b.is_ascii_digit()).cloned().collect();
    if digits.is_empty() || sig.get(3 + digits.len()) != Some(&b'.') {
        rej!("signature version is malformed");
    }
    let major = std::str::from_utf8(&digits).unwrap().parse::<u64>().unwrap_or(u64::MAX);
    if major < 3 {
        rej!("ND2 version {major} is not supported");
    }

    // Chunk map.
    if src.size < 40 {
        rej!("file shorter than 40 bytes");
    }
    let tail = src.read(src.size - 40, 40)?;
    if &tail[..32] != MAP_SIG {
        rej!("no chunk map signature at the end of the file");
    }
    let m = le_u64(&tail[32..]);
    safe(m, "chunk map offset")?;
    let (mn, md) = nd.header(m)?;
    let mname = src.read(m + 16, mn)?;
    let trimmed: &[u8] = {
        let mut e = mname.len();
        while e > 0 && mname[e - 1] == 0 {
            e -= 1;
        }
        &mname[..e]
    };
    if trimmed != b"ND2 FILEMAP SIGNATURE NAME 0001!" {
        rej!("chunk map chunk has the wrong name");
    }
    let mdata = src.read(m + 16 + mn, md)?;
    let mut map: HashMap<Vec<u8>, u64> = HashMap::new();
    let mut p = 0usize;
    loop {
        let bang = match mdata[p..].iter().position(|&b| b == b'!') {
            Some(i) => p + i,
            None => rej!("chunk map is truncated"),
        };
        let rname = mdata[p..=bang].to_vec();
        p = bang + 1;
        if rname == MAP_SIG {
            break;
        }
        if p + 16 > mdata.len() {
            rej!("chunk map record is truncated");
        }
        let off = le_u64(&mdata[p..]);
        p += 16;
        map.insert(rname, off);
    }

    let lv_chunk = |name: &str| -> R<Option<V>> {
        match map.get(name.as_bytes()) {
            None => Ok(None),
            Some(&o) => Ok(Some(decode_lv(&nd.chunk_data(o)?)?)),
        }
    };

    // §4.3 attributes.
    let attrs_lv = match lv_chunk("ImageAttributesLV!")? {
        Some(v) => v,
        None => rej!("ImageAttributesLV! chunk is missing"),
    };
    let a = match path(&attrs_lv, "SLxImageAttributes")? {
        Some(a) => a,
        None => rej!("SLxImageAttributes is missing"),
    };
    let width = int_at(a, "uiWidth", None)?;
    let height = int_at(a, "uiHeight", None)?;
    let width_bytes = int_at(a, "uiWidthBytes", None)?;
    let comp = int_at(a, "uiComp", None)?;
    let bpc = int_at(a, "uiBpcInMemory", None)?;
    let bpc_sig = int_at(a, "uiBpcSignificant", None)?;
    if width < 1 || height < 1 || comp < 1 {
        rej!("uiWidth, uiHeight and uiComp must be at least 1");
    }
    let ecomp = int_at(a, "eCompression", Some(2))?;
    let tile_w = int_at(a, "uiTileWidth", Some(0))?;
    let tile_h = int_at(a, "uiTileHeight", Some(0))?;
    let dtype = match bpc {
        8 => "uint8",
        16 => "uint16",
        32 => "float32",
        b => rej!("uiBpcInMemory {b} is not supported"),
    };
    let compressed = match ecomp {
        2 => false,
        0 => true,
        c => rej!("eCompression {c} is not supported"),
    };
    if (tile_w > 0 && tile_w != width) || (tile_h > 0 && tile_h != height) {
        rej!("tiled ND2 files are not supported");
    }

    // Experiment.
    let mut loops: Vec<Loop> = Vec::new();
    if let Some(meta) = lv_chunk("ImageMetadataLV!")? {
        let exp = match path(&meta, "SLxExperiment")? {
            Some(e) => e,
            None => rej!("SLxExperiment is missing"),
        };
        flatten(exp, 0, &mut loops)?;
    }
    for (i, l) in loops.iter().enumerate() {
        if loops[..i].iter().any(|x| x.kind == l.kind) {
            rej!("two {:?} loops", l.kind);
        }
    }

    // Picture metadata.
    let pic_lv = lv_chunk("ImageMetadataSeqLV|0!")?;
    let pic = match &pic_lv {
        Some(v) => path(v, "SLxPictureMetadata")?,
        None => None,
    };
    let mut calibration: Option<(f64, f64)> = None;
    let mut channels: Option<Vec<(String, String)>> = None;
    if let Some(pm) = pic {
        let cal = match path(pm, "bCalibrated")? {
            Some(v) => flag(v, "bCalibrated")?,
            None => false,
        };
        if cal {
            if let Some(dc) = path(pm, "dCalibration")? {
                let dc = number(dc, "dCalibration")?;
                if dc > 0.0 {
                    let mut asp = num_at(pm, "dAspect", Some(1.0))?;
                    if !(asp > 0.0) {
                        asp = 1.0;
                    }
                    calibration = Some((dc, asp));
                }
            }
        }
        let nplanes = int_at(pm, "sPicturePlanes/uiCount", Some(0))?;
        if nplanes >= 1 {
            let mut planes = Vec::new();
            let mut all = true;
            for i in 0..nplanes {
                match path(pm, &format!("sPicturePlanes/sPlaneNew/a{i}"))? {
                    Some(pl) => planes.push(pl),
                    None => {
                        all = false;
                        break;
                    }
                }
            }
            if all {
                let mut cc = Vec::new();
                for pl in &planes {
                    cc.push(int_at(pl, "uiCompCount", Some(1))?);
                }
                if cc.iter().all(|&c| c == 1 || c == 3) && cc.iter().sum::<u64>() == comp {
                    let mut ch = Vec::new();
                    for (pl, &c) in planes.iter().zip(&cc) {
                        let desc = match path(pl, "sDescription")? {
                            None => String::new(),
                            Some(V::Str(s)) => s.clone(),
                            Some(_) => rej!("sDescription is not a string"),
                        };
                        if c == 1 {
                            let col = int_at(pl, "uiColor", Some(0xFFFFFF))?;
                            if col > u32::MAX as u64 {
                                rej!("uiColor is out of range");
                            }
                            let col = col as u32;
                            let rgb = ((col & 0xFF) << 16) | (col & 0xFF00) | ((col >> 16) & 0xFF);
                            ch.push((desc, hex_color(rgb)));
                        } else {
                            ch.push((format!("{desc} R"), "FF0000".into()));
                            ch.push((format!("{desc} G"), "00FF00".into()));
                            ch.push((format!("{desc} B"), "0000FF".into()));
                        }
                    }
                    channels = Some(ch);
                }
            }
        }
    }
    let channels = channels.unwrap_or_else(|| (0..comp).map(|k| (format!("C{k}"), "FFFFFF".to_string())).collect());

    // §4.4 frames.
    let r = width
        .checked_mul(comp)
        .and_then(|v| v.checked_mul(bpc / 8))
        .ok_or_else(|| E::Reject("row size overflows".into()))?;
    if width_bytes < r {
        rej!("uiWidthBytes {width_bytes} is less than {r}");
    }
    if compressed && width_bytes != r {
        rej!("uiWidthBytes {width_bytes} differs from {r} in a compressed file");
    }
    let mut nframes: u128 = 1;
    for l in &loops {
        nframes = nframes.saturating_mul(l.count as u128);
    }
    let mut frames: Vec<(u64, u64)> = Vec::new(); // (f, chunk offset)
    for (k, &o) in &map {
        if let Some(rest) = k.strip_prefix(b"ImageDataSeq|".as_slice()) {
            if let Some(num) = rest.strip_suffix(b"!".as_slice()) {
                let canonical = !num.is_empty()
                    && num.iter().all(|b| b.is_ascii_digit())
                    && (num.len() == 1 || num[0] != b'0');
                if canonical {
                    if let Ok(f) = std::str::from_utf8(num).unwrap().parse::<u64>() {
                        if (f as u128) < nframes {
                            frames.push((f, o));
                        }
                    }
                }
            }
        }
    }
    frames.sort();

    let mut frame_ranges: Vec<(u64, Vec<(u64, u64)>)> = Vec::new();
    if !frames.is_empty() {
        if !compressed {
            let frame_bytes = height
                .checked_mul(width_bytes)
                .and_then(|v| v.checked_add(8))
                .ok_or_else(|| E::Reject("frame size overflows".into()))?;
            let (lo, hi) = (frames[0], *frames.last().unwrap());
            let (n0, d0) = nd.header(lo.1)?;
            let (n1, d1) = nd.header(hi.1)?;
            if n0 != n1 {
                rej!("frames {} and {} have different name lengths", lo.0, hi.0);
            }
            if d0 < frame_bytes || d1 < frame_bytes {
                rej!("frame data is shorter than {frame_bytes} bytes");
            }
            for &(f, o) in &frames {
                let start = o
                    .checked_add(16 + n0 + 8)
                    .ok_or_else(|| E::Reject("offset overflows".into()))?;
                let ranges = if width_bytes == r {
                    vec![(start, height * r)]
                } else {
                    (0..height).map(|row| (start + row * width_bytes, r)).collect()
                };
                frame_ranges.push((f, ranges));
            }
        } else {
            for &(f, o) in &frames {
                let (n, d) = nd.header(o)?;
                if d <= 8 {
                    rej!("compressed frame {f} has data length {d}");
                }
                frame_ranges.push((f, vec![(o + 16 + n + 8, d - 8)]));
            }
        }
    }

    // §4.6 output.
    let time = loops.iter().find(|l| l.kind == Kind::Time);
    let pos = loops.iter().find(|l| l.kind == Kind::Position);
    let zl = loops.iter().find(|l| l.kind == Kind::Z);
    let npos = pos.map(|l| l.count).unwrap_or(1);

    out.json(
        "zarr.json",
        obj(vec![
            ("zarr_format", J::U(3)),
            ("node_type", s("group")),
            (
                "attributes",
                obj(vec![("ome", obj(vec![("version", s("0.5")), ("bioformats2raw.layout", J::U(3))]))]),
            ),
        ]),
    );
    out.json(
        "OME/zarr.json",
        obj(vec![
            ("zarr_format", J::U(3)),
            ("node_type", s("group")),
            (
                "attributes",
                obj(vec![(
                    "ome",
                    obj(vec![
                        ("version", s("0.5")),
                        ("series", J::A((0..npos).map(|p| J::S(p.to_string())).collect())),
                    ]),
                )]),
            ),
        ]),
    );

    let mut axes = Vec::new();
    let mut scale = Vec::new();
    let mut shape = Vec::new();
    let mut chunk = Vec::new();
    if let Some(t) = time {
        let pos_period = t.param > 0.0;
        axes.push(Axis { name: "t", unit: if pos_period { Some("second") } else { None } });
        scale.push(if pos_period { finite(t.param / 1000.0, "time scale")? } else { 1.0 });
        shape.push(t.count);
        chunk.push(1);
    }
    let c_axis = comp > 1;
    if c_axis {
        axes.push(Axis { name: "c", unit: None });
        scale.push(1.0);
        shape.push(comp);
        chunk.push(comp);
    }
    if let Some(z) = zl {
        let pos_step = z.param > 0.0;
        axes.push(Axis { name: "z", unit: if pos_step { Some("micrometer") } else { None } });
        scale.push(if pos_step { z.param } else { 1.0 });
        shape.push(z.count);
        chunk.push(1);
    }
    match calibration {
        Some((dc, asp)) => {
            axes.push(Axis { name: "y", unit: Some("micrometer") });
            scale.push(finite(dc * asp, "y scale")?);
            axes.push(Axis { name: "x", unit: Some("micrometer") });
            scale.push(dc);
        }
        None => {
            axes.push(Axis { name: "y", unit: None });
            scale.push(1.0);
            axes.push(Axis { name: "x", unit: None });
            scale.push(1.0);
        }
    }
    shape.extend([height, width]);
    chunk.extend([height, width]);
    let dims: Vec<&str> = axes.iter().map(|a| a.name).collect();

    let mut codecs = Vec::new();
    if c_axis {
        codecs.push(transpose_codec(axes.len(), time.is_some() as usize));
    }
    codecs.push(bytes_codec((bpc / 8) as u32, true));
    if compressed {
        codecs.push(zlib_codec());
    }

    let b = if bpc_sig >= 1 && bpc_sig <= bpc { bpc_sig } else { bpc };
    let omero_channels: Vec<J> = channels
        .iter()
        .map(|(label, color)| {
            let mut m = vec![("label", s(label)), ("color", s(color)), ("active", J::Bool(true))];
            if dtype != "float32" {
                let v = (1u64 << b) - 1;
                m.push((
                    "window",
                    obj(vec![("min", J::U(0)), ("max", J::U(v)), ("start", J::U(0)), ("end", J::U(v))]),
                ));
            }
            obj(m)
        })
        .collect();
    let omero = obj(vec![("channels", J::A(omero_channels))]);

    for p in 0..npos {
        out.json(
            &format!("{p}/zarr.json"),
            image_json(Some(format!("position {p}")), &axes, &[scale.clone()], Some(omero.clone())),
        );
        out.json(&format!("{p}/0/zarr.json"), array_json(&shape, dtype, &chunk, codecs.clone(), &dims));
    }

    for (f, ranges) in frame_ranges {
        // Row-major index over the loops (last fastest).
        let mut rem = f;
        let mut idx = vec![0u64; loops.len()];
        for (i, l) in loops.iter().enumerate().rev() {
            idx[i] = rem % l.count;
            rem /= l.count;
        }
        let get_idx = |k: Kind| loops.iter().position(|l| l.kind == k).map(|i| idx[i]);
        let p = get_idx(Kind::Position).unwrap_or(0);
        let mut coords = Vec::new();
        if let Some(tt) = get_idx(Kind::Time) {
            coords.push(tt);
        }
        if c_axis {
            coords.push(0);
        }
        if let Some(z) = get_idx(Kind::Z) {
            coords.push(z);
        }
        coords.extend([0, 0]);
        let key = format!(
            "{p}/0/c/{}",
            coords.iter().map(|v| v.to_string()).collect::<Vec<_>>().join("/")
        );
        out.refs(key, ranges, src.size)?;
    }
    Ok(())
}
