//! JPEG 2000 decoder for Neuroglancer, built on hayro-jpeg2000.
//!
//! `decode` returns a buffer holding a 16-byte header of little-endian u32s
//! `[width, height, num_components, body_len]`, followed by either the
//! decoded samples (`width * height * num_components` samples of
//! `bytes_per_sample` bytes, little-endian, interleaved by component in
//! row-major order) or, if `width` is 0, a UTF-8 error message. The caller
//! frees it with `free(ptr, 16 + body_len)`.

use std::alloc::{alloc, dealloc, Layout};
use std::slice;

use hayro_jpeg2000::{DecodeSettings, DecoderContext, Image};

const HEADER: usize = 16;

#[no_mangle]
pub fn malloc(size: usize) -> *mut u8 {
    let layout = Layout::from_size_align(size.max(1), 1).unwrap();
    unsafe { alloc(layout) }
}

#[no_mangle]
pub fn free(ptr: *mut u8, size: usize) {
    let layout = Layout::from_size_align(size.max(1), 1).unwrap();
    unsafe { dealloc(ptr, layout) }
}

fn finish(header: [u32; 4], body: &[u8]) -> *mut u8 {
    let size = HEADER + body.len();
    let ptr = malloc(size);
    if ptr.is_null() {
        return ptr;
    }
    let out = unsafe { slice::from_raw_parts_mut(ptr, size) };
    for (i, word) in header.iter().enumerate() {
        out[4 * i..4 * i + 4].copy_from_slice(&word.to_le_bytes());
    }
    out[HEADER..].copy_from_slice(body);
    ptr
}

/// The codestream inside `data`: `data` itself, or the contents of the
/// `jp2c` box of a JP2 file.
fn codestream(data: &[u8]) -> Option<&[u8]> {
    if data.starts_with(&[0xff, 0x4f, 0xff, 0x51]) {
        return Some(data);
    }
    let mut at = 0usize;
    while at + 8 <= data.len() {
        let len = u32::from_be_bytes(data[at..at + 4].try_into().ok()?) as usize;
        let kind = &data[at + 4..at + 8];
        let (header, len) = match len {
            0 => (8, data.len() - at),
            1 => {
                let len = u64::from_be_bytes(data.get(at + 8..at + 16)?.try_into().ok()?);
                (16, usize::try_from(len).ok()?)
            }
            _ => (8, len),
        };
        if len < header || at + len > data.len() {
            return None;
        }
        if kind == b"jp2c" {
            return Some(&data[at + header..at + len]);
        }
        at += len;
    }
    None
}

/// Whether each component is signed, from the SIZ marker segment.
/// hayro-jpeg2000 0.4 ignores this, and always undoes the DC level shift of
/// unsigned samples (adds 2^(precision - 1)).
fn signed_components(data: &[u8]) -> Option<Vec<(bool, u32)>> {
    let cs = codestream(data)?;
    let count = u16::from_be_bytes(cs.get(40..42)?.try_into().ok()?) as usize;
    (0..count)
        .map(|i| {
            let ssiz = *cs.get(42 + 3 * i)?;
            Some((ssiz & 0x80 != 0, (ssiz & 0x7f) as u32 + 1))
        })
        .collect()
}

fn decode_samples(
    data: &[u8],
    bytes_per_sample: u32,
    signed: bool,
) -> Result<(u32, u32, u32, Vec<u8>), String> {
    let (lo, hi) = match (bytes_per_sample, signed) {
        (1, false) => (0.0, u8::MAX as f32),
        (1, true) => (i8::MIN as f32, i8::MAX as f32),
        (2, false) => (0.0, u16::MAX as f32),
        (2, true) => (i16::MIN as f32, i16::MAX as f32),
        _ => return Err(format!("unsupported sample size: {bytes_per_sample} bytes")),
    };
    let settings = DecodeSettings {
        resolve_palette_indices: false,
        strict: false,
        target_resolution: None,
    };
    let image = Image::new(data, &settings).map_err(|e| format!("{e:?}"))?;
    let (width, height) = (image.width(), image.height());
    let mut context = DecoderContext::default();
    let decoded = image.decode(&mut context).map_err(|e| format!("{e:?}"))?;
    let components = decoded.components();
    let pixels = width as usize * height as usize;
    for c in components {
        if c.bit_depth() as u32 > 8 * bytes_per_sample {
            return Err(format!(
                "a component has {} bits, more than the data type's {}",
                c.bit_depth(),
                8 * bytes_per_sample
            ));
        }
        if c.samples().len() != pixels {
            return Err("components differ in size (subsampling is not supported)".into());
        }
    }
    let n = components.len();
    let signedness = signed_components(data)
        .filter(|s| s.len() == n)
        .ok_or("cannot read the SIZ marker segment")?;
    let size = bytes_per_sample as usize;
    let mut out = vec![0u8; pixels * n * size];
    for (ci, c) in components.iter().enumerate() {
        let (is_signed, precision) = signedness[ci];
        let shift = if is_signed { (1u32 << (precision - 1)) as f32 } else { 0.0 };
        for (i, &v) in c.samples().iter().enumerate() {
            let v = (v - shift).round().clamp(lo, hi);
            let at = (i * n + ci) * size;
            match (bytes_per_sample, signed) {
                (1, false) => out[at] = v as u8,
                (1, true) => out[at] = v as i8 as u8,
                (2, false) => out[at..at + 2].copy_from_slice(&(v as u16).to_le_bytes()),
                _ => out[at..at + 2].copy_from_slice(&(v as i16).to_le_bytes()),
            }
        }
    }
    Ok((width, height, n as u32, out))
}

/// Decodes the JPEG 2000 codestream or JP2 file at `ptr[..len]`.
#[no_mangle]
pub fn decode(ptr: *const u8, len: usize, bytes_per_sample: u32, signed: u32) -> *mut u8 {
    let data = unsafe { slice::from_raw_parts(ptr, len) };
    match decode_samples(data, bytes_per_sample, signed != 0) {
        Ok((width, height, components, samples)) => {
            finish([width, height, components, samples.len() as u32], &samples)
        }
        Err(message) => finish([0, 0, 0, message.len() as u32], message.as_bytes()),
    }
}
