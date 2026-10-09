//! CZI codec headers (spec/virtualize/czi.md §3.1–§3.2, spec/virtualize/czi/profile.md §13.4): a
//! subblock's coded size from the first 2^16 bytes of its data at most. The
//! readers are given the bytes read so far; one that needs more says how many
//! (`Err(n)`), and is run again with them.

/// The first bytes of a subblock's data: `bound` = min(its size, 2^16) are
/// readable; `have` of them are here.
pub struct Head<'a> {
    pub have: &'a [u8],
    pub bound: u64,
}

/// A read past what is here: the bytes needed from the data's start.
pub type Need = u64;

impl Head<'_> {
    /// Bytes [o, o + n) of the data: None past the bound, Err when not here yet.
    fn get(&self, o: u64, n: u64) -> Result<Option<&[u8]>, Need> {
        if o + n > self.bound {
            return Ok(None);
        }
        if o + n > self.have.len() as u64 {
            return Err(o + n);
        }
        Ok(Some(&self.have[o as usize..(o + n) as usize]))
    }
}

pub const UNCOMPRESSED: i32 = 0;
pub const JPEG: i32 = 1;
pub const JPEGXR: i32 = 4;
pub const ZSTD0: i32 = 5;
pub const ZSTD1: i32 = 6;

/// (data type, samples p, bytes per pixel q)
pub fn pixel_type(pt: i32) -> Option<(&'static str, u64, u64)> {
    Some(match pt {
        0 => ("uint8", 1, 1),
        1 => ("uint16", 1, 2),
        2 => ("float32", 1, 4),
        3 => ("uint8", 3, 3),
        4 => ("uint16", 3, 6),
        8 => ("float32", 3, 12),
        9 => ("uint8", 4, 4),
        10 => ("complex64", 1, 8),
        11 => ("complex64", 3, 24),
        12 => ("int32", 1, 4),
        13 => ("float64", 1, 8),
        _ => return None,
    })
}

const WIC: [u8; 15] = [0x24, 0xC3, 0xDD, 0x6F, 0x03, 0x4E, 0xFE, 0x4B, 0xB1, 0x85, 0x3D, 0x77, 0x76, 0x8D, 0xC9];
const BGR96: [u8; 16] = [0x8F, 0xD7, 0xFE, 0xE3, 0xDB, 0xE8, 0xCF, 0x4A, 0x84, 0xC1, 0xE9, 0x7F, 0x61, 0x36, 0xB3, 0x27];

fn jxr_format(pt: i32, guid: &[u8]) -> bool {
    let last = |b: u8| guid[..15] == WIC && guid[15] == b;
    match pt {
        0 => last(0x08),
        1 => last(0x0b),
        2 => last(0x11),
        3 => last(0x0c) || last(0x0d),
        4 => last(0x15),
        9 => last(0x0f),
        8 => guid == BGR96,
        _ => false,
    }
}

/// (header length, hi-lo flag) of a Zstd1 subblock's data.
fn zstd1_header(h: &Head) -> Result<Option<(u64, bool)>, Need> {
    let Some(b) = h.get(0, 1)? else { return Ok(None) };
    if b[0] == 1 {
        return Ok(Some((1, false)));
    }
    let Some(b) = h.get(0, 3)? else { return Ok(None) };
    if b[0] != 3 || b[1] != 1 {
        return Ok(None);
    }
    Ok(Some((3, b[2] & 1 != 0)))
}

/// The content size a zstd frame header at `at` declares.
fn zstd_content_size(h: &Head, at: u64) -> Result<Option<u64>, Need> {
    let Some(b) = h.get(at, 5)? else { return Ok(None) };
    if b[..4] != [0x28, 0xb5, 0x2f, 0xfd] {
        return Ok(None);
    }
    let d = b[4];
    if d & 0x08 != 0 || d & 0x03 != 0 {
        return Ok(None);
    }
    let single = d & 0x20 != 0;
    let n = match d >> 6 {
        0 => {
            if single {
                1
            } else {
                0
            }
        }
        1 => 2,
        2 => 4,
        _ => 8,
    };
    if n == 0 {
        return Ok(None);
    }
    let pos = at + 5 + if single { 0 } else { 1 };
    let Some(f) = h.get(pos, n)? else { return Ok(None) };
    let v = f.iter().rev().fold(0u64, |a, &x| (a << 8) | x as u64);
    Ok(Some(if n == 2 { v + 256 } else { v }))
}

/// (width, height) of a JPEG stream's frame header.
fn jpeg_frame(h: &Head, p: u64) -> Result<Option<(u64, u64)>, Need> {
    let Some(b) = h.get(0, 2)? else { return Ok(None) };
    if b != [0xFF, 0xD8] {
        return Ok(None);
    }
    let mut pos = 2;
    loop {
        let Some(b) = h.get(pos, 1)? else { return Ok(None) };
        if b[0] != 0xFF {
            return Ok(None);
        }
        let mut b = Some(b[0]);
        while b == Some(0xFF) {
            pos += 1;
            b = h.get(pos, 1)?.map(|x| x[0]);
        }
        let Some(m) = b else { return Ok(None) };
        pos += 1;
        if (0xD0..=0xD7).contains(&m) || m == 0x01 {
            continue;
        }
        if m == 0xDA || m == 0xD9 {
            return Ok(None);
        }
        let Some(lb) = h.get(pos, 2)? else { return Ok(None) };
        let length = u16::from_be_bytes([lb[0], lb[1]]) as u64;
        if length < 2 {
            return Ok(None);
        }
        if (0xC0..=0xCF).contains(&m) && !matches!(m, 0xC4 | 0xC8 | 0xCC) {
            let Some(f) = h.get(pos + 2, 6)? else { return Ok(None) };
            let (precision, height, width, count) =
                (f[0], u16::from_be_bytes([f[1], f[2]]) as u64, u16::from_be_bytes([f[3], f[4]]) as u64, f[5]);
            if matches!(m, 0xC0..=0xC2) && precision == 8 && height >= 1 && width >= 1 && count as u64 == p {
                return Ok(Some((width, height)));
            }
            return Ok(None);
        }
        pos += length;
    }
}

/// (ImageWidth, ImageHeight) of a JPEG XR file whose pixel format the pixel type admits.
fn jpegxr_size(h: &Head, pt: i32) -> Result<Option<(u64, u64)>, Need> {
    let Some(b) = h.get(0, 8)? else { return Ok(None) };
    if b[..4] != [0x49, 0x49, 0xbc, 0x01] {
        return Ok(None);
    }
    let ifd = u32::from_le_bytes(b[4..8].try_into().unwrap()) as u64;
    let Some(c) = h.get(ifd, 2)? else { return Ok(None) };
    let count = u16::from_le_bytes([c[0], c[1]]) as u64;
    let mut found: Vec<(u16, [u8; 12])> = Vec::new();
    for k in 0..count {
        let Some(e) = h.get(ifd + 2 + 12 * k, 12)? else { return Ok(None) };
        let tag = u16::from_le_bytes([e[0], e[1]]);
        if matches!(tag, 0xBC01 | 0xBC80 | 0xBC81) && !found.iter().any(|f| f.0 == tag) {
            found.push((tag, e.try_into().unwrap()));
            if found.len() == 3 {
                break;
            }
        }
    }
    if found.len() < 3 {
        return Ok(None);
    }
    let entry = |t: u16| found.iter().find(|f| f.0 == t).unwrap().1;
    let fmt = entry(0xBC01);
    let typ = u16::from_le_bytes([fmt[2], fmt[3]]);
    let n = u32::from_le_bytes(fmt[4..8].try_into().unwrap());
    if typ != 1 || n != 16 {
        return Ok(None);
    }
    let at = u32::from_le_bytes(fmt[8..12].try_into().unwrap()) as u64;
    let Some(guid) = h.get(at, 16)? else { return Ok(None) };
    if !jxr_format(pt, guid) {
        return Ok(None);
    }
    let mut size = [0u64; 2];
    for (i, t) in [0xBC80u16, 0xBC81].into_iter().enumerate() {
        let e = entry(t);
        let typ = u16::from_le_bytes([e[2], e[3]]);
        let n = u32::from_le_bytes(e[4..8].try_into().unwrap());
        if n != 1 || !(typ == 3 || typ == 4) {
            return Ok(None);
        }
        let v = if typ == 3 {
            u16::from_le_bytes([e[8], e[9]]) as u64
        } else {
            u32::from_le_bytes(e[8..12].try_into().unwrap()) as u64
        };
        if v < 1 {
            return Ok(None);
        }
        size[i] = v;
    }
    Ok(Some((size[0], size[1])))
}

/// (coded width, coded height, hi-lo flag, header length) of a subblock whose
/// stored size is w × h and whose data is n bytes, or None when it has no coded
/// size; Err when its codec header needs more of the data than `head` holds.
pub fn coded_size(pt: i32, comp: i32, w: u64, h: u64, n: u64, head: &Head) -> Result<Option<(u64, u64, bool, u64)>, Need> {
    let Some((_, p, q)) = pixel_type(pt) else { return Ok(None) };
    let pixels = (w as u128) * (h as u128) * (q as u128);
    match comp {
        UNCOMPRESSED => Ok((n as u128 >= pixels).then_some((w, h, false, 0))),
        ZSTD0 => Ok((zstd_content_size(head, 0)?.map(|x| x as u128) == Some(pixels)).then_some((w, h, false, 0))),
        ZSTD1 => {
            let Some((len, hilo)) = zstd1_header(head)? else { return Ok(None) };
            if n <= len || (hilo && !(pt == 1 || pt == 4)) {
                return Ok(None);
            }
            Ok((zstd_content_size(head, len)?.map(|x| x as u128) == Some(pixels)).then_some((w, h, hilo, len)))
        }
        JPEG => Ok(jpeg_frame(head, p)?.map(|(a, b)| (a, b, false, 0))),
        JPEGXR => Ok(jpegxr_size(head, pt)?.map(|(a, b)| (a, b, false, 0))),
        _ => Ok(None),
    }
}

/// The codecs after `transpose` (spec/virtualize/czi.md §3.1), as JSON.
pub fn codec_chain(pt: i32, comp: i32, hilo: bool) -> serde_json::Value {
    use serde_json::json;
    let dt = pixel_type(pt).map(|x| x.0).unwrap_or("uint8");
    let bytes = if dt == "uint8" { json!({"name": "bytes"}) } else { json!({"name": "bytes", "configuration": {"endian": "little"}}) };
    let zstd = json!({"name": "zstd", "configuration": {"level": 0, "checksum": false}});
    match comp {
        JPEG => json!([{"name": "imagecodecs_jpeg"}]),
        JPEGXR => json!([{"name": "imagecodecs_jpegxr"}]),
        ZSTD0 | ZSTD1 if hilo => json!([bytes, {"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}}, zstd]),
        ZSTD0 | ZSTD1 => json!([bytes, zstd]),
        _ => json!([bytes]),
    }
}
