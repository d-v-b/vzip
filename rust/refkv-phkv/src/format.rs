//! The bytes of a phkv store (spec/bindings/phkv.md §17
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#17-container>, §19
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#19-records>, §20
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#20-properties>): what the
//! reader and the writer share, with no I/O.

use crate::ptrhash::{Params, HASH_XXH64};

pub const MAGIC: &[u8; 4] = b"PHKV";
pub const VERSION: u16 = 0;
pub const SUPERBLOCK_SIZE: u64 = 64;
pub const DIR_ENTRY_SIZE: u64 = 40;
pub const FLAG_HYBRID: u16 = 1;
pub const FLAG_MUST_UNDERSTAND: u8 = 1;
pub const EMPTY_SLOT: u64 = u64::MAX;
/// The largest reference payload (spec/bindings/phkv.md §19.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#193-reference-records>).
pub const MAX_PAYLOAD: u64 = 1 << 24;
/// The hybrid profile's entry (spec/bindings/phkv.md §21
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#21-hybrid-profile>).
pub const HYBRID_KEY: &str = "__vz__/phkv";

/// The probe distances (spec/bindings/phkv.md §17.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#172-finding-the-superblock>):
/// 0, then 2^10 to 2^30.
pub fn probe_distances() -> impl Iterator<Item = u64> {
    std::iter::once(0).chain((10..=30).map(|k| 1u64 << k))
}

/// The smallest probe distance of at least `n` bytes.
pub fn probe_distance_at_least(n: u64) -> Option<u64> {
    probe_distances().find(|&d| d >= n)
}

/// A section type (spec/bindings/phkv.md §17.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#174-sections>).
pub mod section {
    pub const SOURCES: u32 = 1;
    pub const RECORDS: u32 = 2;
    pub const PAGES: u32 = 3;
    pub const PTRHASH: u32 = 4;
    pub const SLOTS: u32 = 5;
    pub const PROPERTY: u32 = 6;
    pub const METADATA: u32 = 7;
}

/// How stored bytes encode their value (spec/bindings/phkv.md §17.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#175-codecs>).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Codec {
    None = 0,
    Deflate = 1,
    Zstd = 2,
}

impl Codec {
    pub fn from_u8(c: u8) -> Option<Codec> {
        match c {
            0 => Some(Codec::None),
            1 => Some(Codec::Deflate),
            2 => Some(Codec::Zstd),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Codec::None => "none",
            Codec::Deflate => "deflate",
            Codec::Zstd => "zstd",
        }
    }
}

/// The zstd level the writer uses: the library's default.
pub const ZSTD_LEVEL: i32 = 3;

/// `data` encoded with `codec`.
pub fn encode(codec: Codec, data: &[u8]) -> Vec<u8> {
    match codec {
        Codec::None => data.to_vec(),
        Codec::Deflate => refkv_vzip::deflate(data),
        Codec::Zstd => {
            let mut c = zstd::bulk::Compressor::new(ZSTD_LEVEL).expect("zstd");
            c.include_checksum(false).expect("zstd");
            c.include_contentsize(true).expect("zstd");
            c.compress(data).expect("zstd")
        }
    }
}

/// `stored` decoded with `codec`, if it decodes cleanly to exactly `size` bytes; it stops once the
/// output passes `size`.
pub fn decode(codec: Codec, stored: &[u8], size: u64) -> Result<Vec<u8>, String> {
    match codec {
        Codec::None => {
            if stored.len() as u64 != size {
                return Err(format!("{} stored bytes for a value of {size}", stored.len()));
            }
            Ok(stored.to_vec())
        }
        Codec::Deflate => inflate(stored, size),
        Codec::Zstd => unzstd(stored, size),
    }
}

fn inflate(stored: &[u8], size: u64) -> Result<Vec<u8>, String> {
    let cap = usize::try_from(size).map_err(|_| "value too large".to_string())?;
    let mut d = flate2::Decompress::new(false);
    let mut out = Vec::with_capacity(cap.min(1 << 26));
    loop {
        if out.len() == out.capacity() {
            if out.len() > cap {
                return Err(format!("inflates to more than {size} bytes"));
            }
            out.reserve((out.capacity()).clamp(1 << 10, 1 << 26));
        }
        let before = (d.total_in(), d.total_out());
        let st = d
            .decompress_vec(&stored[d.total_in() as usize..], &mut out, flate2::FlushDecompress::None)
            .map_err(|e| format!("does not inflate: {e}"))?;
        if out.len() as u64 > size {
            return Err(format!("inflates to more than {size} bytes"));
        }
        match st {
            flate2::Status::StreamEnd => break,
            _ if (d.total_in(), d.total_out()) == before && out.len() < out.capacity() => {
                return Err("truncated DEFLATE stream".into());
            }
            _ => {}
        }
    }
    if d.total_in() as usize != stored.len() {
        return Err(format!("{} bytes after the DEFLATE stream", stored.len() - d.total_in() as usize));
    }
    if out.len() as u64 != size {
        return Err(format!("inflates to {} bytes, not {size}", out.len()));
    }
    Ok(out)
}

fn unzstd(stored: &[u8], size: u64) -> Result<Vec<u8>, String> {
    let cap = usize::try_from(size).map_err(|_| "value too large".to_string())?;
    // one frame, nothing after it, no skippable frame
    let frame = zstd::zstd_safe::find_frame_compressed_size(stored).map_err(|e| format!("not a zstd frame: {}", zstd::zstd_safe::get_error_name(e)))?;
    if frame != stored.len() {
        return Err(format!("{} bytes after the zstd frame", stored.len().saturating_sub(frame)));
    }
    if stored.len() >= 4 && (u32::from_le_bytes(stored[..4].try_into().unwrap()) & 0xFFFF_FFF0) == 0x184D_2A50 {
        return Err("a skippable zstd frame".into());
    }
    // decompress into a buffer one byte larger, to tell "more than size" from "exactly size"
    let mut d = zstd::bulk::Decompressor::new().map_err(|e| e.to_string())?;
    let mut out = Vec::with_capacity(cap.checked_add(1).ok_or("value too large")?);
    d.decompress_to_buffer(stored, &mut out).map_err(|e| format!("does not decompress: {e}"))?;
    if out.len() != cap {
        return Err(format!("decompresses to {} bytes, not {size}", out.len()));
    }
    Ok(out)
}

pub fn crc32c(b: &[u8]) -> u32 {
    crc32c::crc32c(b)
}

// ---------------------------------------------------------------- varints

pub fn put_varint(out: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        out.push((v as u8) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}

/// A cursor over bytes being decoded; every read fails with a message on truncation.
pub struct Cur<'a> {
    pub b: &'a [u8],
    pub pos: usize,
}

impl<'a> Cur<'a> {
    pub fn new(b: &'a [u8]) -> Cur<'a> {
        Cur { b, pos: 0 }
    }

    pub fn done(&self) -> bool {
        self.pos == self.b.len()
    }

    pub fn varint(&mut self) -> Result<u64, String> {
        let mut v: u64 = 0;
        for i in 0..10 {
            let byte = *self.b.get(self.pos).ok_or("truncated varint")?;
            self.pos += 1;
            if i == 9 && byte > 1 {
                return Err("varint exceeds 2^64 - 1".into());
            }
            v |= ((byte & 0x7f) as u64) << (7 * i);
            if byte < 0x80 {
                return Ok(v);
            }
        }
        Err("varint longer than 10 bytes".into())
    }

    pub fn u8(&mut self) -> Result<u8, String> {
        Ok(self.bytes(1)?[0])
    }

    pub fn u32(&mut self) -> Result<u32, String> {
        Ok(u32::from_le_bytes(self.bytes(4)?.try_into().unwrap()))
    }

    pub fn u64(&mut self) -> Result<u64, String> {
        Ok(u64::from_le_bytes(self.bytes(8)?.try_into().unwrap()))
    }

    pub fn bytes(&mut self, n: u64) -> Result<&'a [u8], String> {
        let n = usize::try_from(n).map_err(|_| "length too large")?;
        let end = self.pos.checked_add(n).filter(|&e| e <= self.b.len()).ok_or("truncated")?;
        let s = &self.b[self.pos..end];
        self.pos = end;
        Ok(s)
    }
}

pub fn le_u32(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}

pub fn le_u64(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}

// ---------------------------------------------------------------- superblock

/// The superblock (spec/bindings/phkv.md §17.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#171-superblock>).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Superblock {
    pub version: u16,
    pub flags: u16,
    pub distance: u64,
    pub key_count: u64,
    pub directory_offset: u64,
    pub directory_length: u64,
    pub region_offset: u64,
    pub region_length: u64,
    pub directory_crc32c: u32,
}

impl Superblock {
    pub fn encode(&self) -> [u8; 64] {
        let mut b = [0u8; 64];
        b[..4].copy_from_slice(MAGIC);
        b[4..6].copy_from_slice(&self.version.to_le_bytes());
        b[6..8].copy_from_slice(&self.flags.to_le_bytes());
        b[8..16].copy_from_slice(&self.distance.to_le_bytes());
        b[16..24].copy_from_slice(&self.key_count.to_le_bytes());
        b[24..32].copy_from_slice(&self.directory_offset.to_le_bytes());
        b[32..40].copy_from_slice(&self.directory_length.to_le_bytes());
        b[40..48].copy_from_slice(&self.region_offset.to_le_bytes());
        b[48..56].copy_from_slice(&self.region_length.to_le_bytes());
        b[56..60].copy_from_slice(&self.directory_crc32c.to_le_bytes());
        let crc = crc32c(&b[..60]);
        b[60..64].copy_from_slice(&crc.to_le_bytes());
        b
    }

    /// The superblock in `b`, if `b` is a match for the probe distance `d`: the magic, the
    /// checksum, and `distance == d`.
    pub fn matching(b: &[u8], d: u64) -> Option<Superblock> {
        if b.len() != 64 || &b[..4] != MAGIC || le_u32(b, 60) != crc32c(&b[..60]) || le_u64(b, 8) != d {
            return None;
        }
        Some(Superblock {
            version: u16::from_le_bytes([b[4], b[5]]),
            flags: u16::from_le_bytes([b[6], b[7]]),
            distance: d,
            key_count: le_u64(b, 16),
            directory_offset: le_u64(b, 24),
            directory_length: le_u64(b, 32),
            region_offset: le_u64(b, 40),
            region_length: le_u64(b, 48),
            directory_crc32c: le_u32(b, 56),
        })
    }

    pub fn hybrid(&self) -> bool {
        self.flags & FLAG_HYBRID != 0
    }
}

// ---------------------------------------------------------------- directory

/// An entry of the section directory (spec/bindings/phkv.md §17.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#173-section-directory>).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DirEntry {
    pub kind: u32,
    pub codec: u8,
    pub flags: u8,
    pub offset: u64,
    pub length: u64,
    pub size: u64,
    pub crc32c: u32,
}

impl DirEntry {
    pub fn encode(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.kind.to_le_bytes());
        out.push(self.codec);
        out.push(self.flags);
        out.extend_from_slice(&[0, 0]);
        out.extend_from_slice(&self.offset.to_le_bytes());
        out.extend_from_slice(&self.length.to_le_bytes());
        out.extend_from_slice(&self.size.to_le_bytes());
        out.extend_from_slice(&self.crc32c.to_le_bytes());
        out.extend_from_slice(&[0; 4]);
    }

    pub fn decode_all(b: &[u8]) -> Result<Vec<DirEntry>, String> {
        if b.len() as u64 % DIR_ENTRY_SIZE != 0 {
            return Err(format!("a directory of {} bytes is not a whole number of entries", b.len()));
        }
        b.chunks_exact(DIR_ENTRY_SIZE as usize)
            .map(|e| {
                if e[6] != 0 || e[7] != 0 || e[36..40] != [0; 4] {
                    return Err("a directory entry's reserved fields are not 0".to_string());
                }
                if e[5] & !FLAG_MUST_UNDERSTAND != 0 {
                    return Err(format!("a directory entry has unknown flags {:#x}", e[5]));
                }
                Ok(DirEntry {
                    kind: le_u32(e, 0),
                    codec: e[4],
                    flags: e[5],
                    offset: le_u64(e, 8),
                    length: le_u64(e, 16),
                    size: le_u64(e, 24),
                    crc32c: le_u32(e, 32),
                })
            })
            .collect()
    }

    /// Whether `[offset, offset + length)` lies within a file of `file_size` bytes.
    pub fn within(&self, file_size: u64) -> bool {
        self.offset.checked_add(self.length).is_some_and(|e| e <= file_size)
    }
}

// ---------------------------------------------------------------- PTRHASH

/// The PTRHASH section, decoded (spec/bindings/phkv.md §18.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#181-ptrhash-section>).
pub fn encode_ptrhash(params: &Params, pilots: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(40 + pilots.len());
    out.push(HASH_XXH64);
    out.extend_from_slice(&[0; 7]);
    out.extend_from_slice(&params.seed.to_le_bytes());
    out.extend_from_slice(&params.parts.to_le_bytes());
    out.extend_from_slice(&params.slots.to_le_bytes());
    out.extend_from_slice(&params.buckets.to_le_bytes());
    out.extend_from_slice(pilots);
    out
}

/// The parameters and pilots of a decoded PTRHASH section, checked against the key count.
pub fn decode_ptrhash(b: Vec<u8>, n: u64) -> Result<(Params, Vec<u8>), String> {
    if b.len() < 40 {
        return Err("PTRHASH section shorter than its header".into());
    }
    if b[0] != HASH_XXH64 {
        return Err(format!("unknown hash_id {}", b[0]));
    }
    if b[1..8] != [0; 7] {
        return Err("PTRHASH reserved bytes are not 0".into());
    }
    let params = Params { seed: le_u64(&b, 8), parts: le_u64(&b, 16), slots: le_u64(&b, 24), buckets: le_u64(&b, 32) };
    if params.parts == 0 || params.slots == 0 || params.buckets == 0 {
        return Err("PTRHASH parts, slots and buckets must not be 0".into());
    }
    let total_slots = params.parts.checked_mul(params.slots).ok_or("parts × slots exceeds 2^64 - 1")?;
    let total_buckets = params.parts.checked_mul(params.buckets).ok_or("parts × buckets exceeds 2^64 - 1")?;
    if total_slots < n {
        return Err(format!("{total_slots} slots for {n} keys"));
    }
    if (b.len() - 40) as u64 != total_buckets {
        return Err(format!("{} pilots for {total_buckets} buckets", b.len() - 40));
    }
    let mut b = b;
    b.drain(..40);
    Ok((params, b))
}

// ---------------------------------------------------------------- records

pub const KIND_BYTES: u8 = 0;
pub const KIND_RANGE: u8 = 1;
pub const KIND_CONCAT: u8 = 2;
pub const REC_INLINE: u8 = 1;
pub const REC_CRC32C: u8 = 2;
pub const REC_HIDDEN: u8 = 4;

/// Where a bytes record's stored bytes are.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Stored {
    Inline(Vec<u8>),
    At(u64),
}

/// What a record says about its value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    Bytes { codec: Codec, stored: Stored, stored_length: u64, size: u64, crc32c: Option<u32> },
    /// kind 1 (a Range) or 2 (a Concat), and the payload
    Reference { kind: u8, payload: Vec<u8> },
}

/// A record (spec/bindings/phkv.md §19.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#191-record-encoding>).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Record {
    pub key: Vec<u8>,
    pub rank: u64,
    pub hidden: bool,
    pub value: Value,
}

impl Record {
    pub fn encode(&self, out: &mut Vec<u8>) {
        put_varint(out, self.key.len() as u64);
        let hidden = if self.hidden { REC_HIDDEN } else { 0 };
        match &self.value {
            Value::Bytes { codec, stored, stored_length, size, crc32c } => {
                out.push(KIND_BYTES);
                let inline = if matches!(stored, Stored::Inline(_)) { REC_INLINE } else { 0 };
                out.push(inline | if crc32c.is_some() { REC_CRC32C } else { 0 } | hidden);
                put_varint(out, self.rank);
                out.push(*codec as u8);
                if let Stored::At(o) = stored {
                    put_varint(out, *o);
                }
                put_varint(out, *stored_length);
                put_varint(out, *size);
                if let Some(c) = crc32c {
                    out.extend_from_slice(&c.to_le_bytes());
                }
                out.extend_from_slice(&self.key);
                if let Stored::Inline(d) = stored {
                    out.extend_from_slice(d);
                }
            }
            Value::Reference { kind, payload } => {
                out.push(*kind);
                out.push(hidden);
                put_varint(out, self.rank);
                put_varint(out, payload.len() as u64);
                out.extend_from_slice(&self.key);
                out.extend_from_slice(payload);
            }
        }
    }
}

/// The result of decoding a record from bytes that may hold only its start.
pub enum Parsed {
    /// the record, and its length
    Whole(Record, usize),
    /// the record is this many bytes long, more than were given
    Needs(u64),
}

/// Decodes the record at the start of `b`. `b` may be cut short: then, if the fields before the key
/// are whole, it says how long the record is. `hybrid` says whether `hidden` is allowed.
pub fn parse_record(b: &[u8], hybrid: bool) -> Result<Parsed, String> {
    let mut c = Cur::new(b);
    let key_len = c.varint()?;
    if key_len == 0 {
        return Err("a record with an empty key".into());
    }
    let kind = c.u8()?;
    let flags = c.u8()?;
    if flags & !(REC_INLINE | REC_CRC32C | REC_HIDDEN) != 0 {
        return Err(format!("record flags {flags:#x} have unknown bits"));
    }
    if flags & REC_HIDDEN != 0 && !hybrid {
        return Err("a hidden record in a standalone store".into());
    }
    let rank = c.varint()?;
    let hidden = flags & REC_HIDDEN != 0;
    let total = |c: &Cur, tail: u64| -> Result<u64, String> {
        (c.pos as u64).checked_add(key_len).and_then(|t| t.checked_add(tail)).ok_or_else(|| "record length exceeds 2^64 - 1".to_string())
    };
    match kind {
        KIND_BYTES => {
            let codec = Codec::from_u8(c.u8()?).ok_or("unknown codec")?;
            let inline = flags & REC_INLINE != 0;
            let at = if inline { None } else { Some(c.varint()?) };
            let stored_length = c.varint()?;
            let size = c.varint()?;
            let crc = if flags & REC_CRC32C != 0 { Some(c.u32()?) } else { None };
            let len = total(&c, if inline { stored_length } else { 0 })?;
            if len > b.len() as u64 {
                return Ok(Parsed::Needs(len));
            }
            let key = utf8_key(c.bytes(key_len)?)?;
            let stored = match at {
                Some(o) => Stored::At(o),
                None => Stored::Inline(c.bytes(stored_length)?.to_vec()),
            };
            Ok(Parsed::Whole(Record { key, rank, hidden, value: Value::Bytes { codec, stored, stored_length, size, crc32c: crc } }, c.pos))
        }
        KIND_RANGE | KIND_CONCAT => {
            if flags & (REC_INLINE | REC_CRC32C) != 0 {
                return Err("a reference record with inline or crc32c set".into());
            }
            let payload_len = c.varint()?;
            let len = total(&c, payload_len)?;
            if len > b.len() as u64 {
                return Ok(Parsed::Needs(len));
            }
            let key = utf8_key(c.bytes(key_len)?)?;
            let payload = c.bytes(payload_len)?.to_vec();
            Ok(Parsed::Whole(Record { key, rank, hidden, value: Value::Reference { kind, payload } }, c.pos))
        }
        k => Err(format!("unknown record kind {k}")),
    }
}

fn utf8_key(b: &[u8]) -> Result<Vec<u8>, String> {
    std::str::from_utf8(b).map_err(|_| "a record key that is not UTF-8".to_string())?;
    Ok(b.to_vec())
}

// ---------------------------------------------------------------- pages

/// A page of the page index (spec/bindings/phkv.md §19.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#194-page-index>).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Page {
    pub first_key: Vec<u8>,
    pub offset: u64,
    pub length: u64,
}

pub fn encode_pages(pages: &[Page]) -> Vec<u8> {
    let mut out = Vec::new();
    for p in pages {
        put_varint(&mut out, p.first_key.len() as u64);
        out.extend_from_slice(&p.first_key);
        put_varint(&mut out, p.offset);
        put_varint(&mut out, p.length);
    }
    out
}

/// The pages of a decoded PAGES section, checked against the RECORDS section `[start, end)`.
pub fn decode_pages(b: &[u8], start: u64, end: u64) -> Result<Vec<Page>, String> {
    let mut c = Cur::new(b);
    let mut pages: Vec<Page> = Vec::new();
    let mut at = start;
    while !c.done() {
        let n = c.varint()?;
        if n == 0 {
            return Err("a page with an empty first_key".into());
        }
        let first_key = c.bytes(n)?.to_vec();
        let offset = c.varint()?;
        let length = c.varint()?;
        if length == 0 {
            return Err("a page of length 0".into());
        }
        if offset != at {
            return Err(format!("a page at {offset}, where {at} was expected"));
        }
        if pages.last().is_some_and(|p| p.first_key >= first_key) {
            return Err("page first_key values do not strictly increase".into());
        }
        at = offset.checked_add(length).ok_or("page end exceeds 2^64 - 1")?;
        if at > end {
            return Err("a page past the end of RECORDS".into());
        }
        pages.push(Page { first_key, offset, length });
    }
    if at != end {
        return Err("the pages do not reach the end of RECORDS".into());
    }
    Ok(pages)
}

/// Whether page `i` (keys `[first_key[i], first_key[i + 1])`) may hold a key starting with
/// `prefix` (spec/bindings/vzip.md §13.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#132-operations>).
pub fn page_may_hold(pages: &[Page], i: usize, prefix: &[u8]) -> bool {
    let lo = pages[i].first_key.as_slice();
    let hi = pages.get(i + 1).map(|p| p.first_key.as_slice());
    hi.is_none_or(|hi| prefix < hi) && (lo <= prefix || lo.starts_with(prefix))
}

// ---------------------------------------------------------------- properties

pub const TYPE_U64: u8 = 1;
pub const TYPE_I64: u8 = 2;
pub const TYPE_BYTES: u8 = 3;
pub const TYPE_STRING: u8 = 4;

/// A property's value (spec/bindings/phkv.md §20.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#201-property-sections>).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Prop {
    U64(u64),
    I64(i64),
    Bytes(Vec<u8>),
    Str(String),
}

impl Prop {
    pub fn type_code(&self) -> u8 {
        match self {
            Prop::U64(_) => TYPE_U64,
            Prop::I64(_) => TYPE_I64,
            Prop::Bytes(_) => TYPE_BYTES,
            Prop::Str(_) => TYPE_STRING,
        }
    }
}

/// The type of each registered property name (spec/bindings/phkv.md §20.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#202-property-names>).
pub fn registered_type(name: &str) -> Option<u8> {
    match name {
        "mtime" => Some(TYPE_I64),
        "mode" => Some(TYPE_U64),
        "media_type" => Some(TYPE_STRING),
        _ => None,
    }
}

/// Whether `name` may name a property or a metadata entry: registered, or containing a `:`.
pub fn check_name(name: &str, registered: impl Fn(&str) -> bool) -> Result<(), String> {
    if name.is_empty() {
        return Err("an empty property name".into());
    }
    if !name.contains(':') && !registered(name) {
        return Err(format!("{name:?} is not a registered name, and has no ':'"));
    }
    Ok(())
}

fn pad8(out: &mut Vec<u8>) {
    while out.len() % 8 != 0 {
        out.push(0);
    }
}

/// The layout of a PROPERTY section's header: the name, and where `present` and `values` start.
pub fn property_header(name_length: u64, n: u64) -> (u64, u64) {
    let present = (8 + name_length).div_ceil(8) * 8;
    let values = present + n.div_ceil(8).div_ceil(8) * 8;
    (present, values)
}

/// A PROPERTY section: `values[r]` is the value of the record of rank `r`, if it has one.
pub fn encode_property(name: &str, type_code: u8, values: &[Option<Prop>]) -> Vec<u8> {
    let n = values.len();
    let mut out = Vec::new();
    out.extend_from_slice(&(name.len() as u32).to_le_bytes());
    out.push(type_code);
    out.extend_from_slice(&[0; 3]);
    out.extend_from_slice(name.as_bytes());
    pad8(&mut out);
    let mut present = vec![0u8; n.div_ceil(8)];
    for (r, v) in values.iter().enumerate() {
        if v.is_some() {
            present[r / 8] |= 1 << (r % 8);
        }
    }
    out.extend_from_slice(&present);
    pad8(&mut out);
    match type_code {
        TYPE_U64 | TYPE_I64 => {
            for v in values {
                let x = match v {
                    Some(Prop::U64(x)) => *x,
                    Some(Prop::I64(x)) => *x as u64,
                    _ => 0,
                };
                out.extend_from_slice(&x.to_le_bytes());
            }
        }
        _ => {
            let mut heap = Vec::new();
            out.extend_from_slice(&0u64.to_le_bytes());
            for v in values {
                match v {
                    Some(Prop::Bytes(b)) => heap.extend_from_slice(b),
                    Some(Prop::Str(s)) => heap.extend_from_slice(s.as_bytes()),
                    _ => {}
                }
                out.extend_from_slice(&(heap.len() as u64).to_le_bytes());
            }
            out.extend_from_slice(&heap);
        }
    }
    out
}

// ---------------------------------------------------------------- metadata

/// The METADATA section (spec/bindings/phkv.md §20.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#203-metadata-section>), from
/// entries in name order.
pub fn encode_metadata(entries: &[(String, Vec<u8>)]) -> Vec<u8> {
    let mut out = Vec::new();
    for (k, v) in entries {
        put_varint(&mut out, k.len() as u64);
        out.extend_from_slice(k.as_bytes());
        put_varint(&mut out, v.len() as u64);
        out.extend_from_slice(v);
    }
    out
}

pub fn decode_metadata(b: &[u8]) -> Result<Vec<(String, Vec<u8>)>, String> {
    let mut c = Cur::new(b);
    let mut out: Vec<(String, Vec<u8>)> = Vec::new();
    while !c.done() {
        let n = c.varint()?;
        let name = String::from_utf8(c.bytes(n)?.to_vec()).map_err(|_| "a metadata name that is not UTF-8")?;
        check_name(&name, |n| n == "writer")?;
        let m = c.varint()?;
        let value = c.bytes(m)?.to_vec();
        if out.last().is_some_and(|(k, _)| k.as_bytes() >= name.as_bytes()) {
            return Err("metadata names do not strictly increase".into());
        }
        out.push((name, value));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn varints() {
        for v in [0, 1, 127, 128, 300, u32::MAX as u64, u64::MAX] {
            let mut b = Vec::new();
            put_varint(&mut b, v);
            assert_eq!(Cur::new(&b).varint().unwrap(), v);
        }
        assert!(Cur::new(&[0xff; 10]).varint().is_err());
        assert!(Cur::new(&[0x80]).varint().is_err());
    }

    #[test]
    fn records_round_trip() {
        let recs = [
            Record { key: b"a".to_vec(), rank: 0, hidden: false, value: Value::Bytes { codec: Codec::None, stored: Stored::Inline(b"xyz".to_vec()), stored_length: 3, size: 3, crc32c: Some(7) } },
            Record { key: b"b/c".to_vec(), rank: 1, hidden: true, value: Value::Bytes { codec: Codec::Zstd, stored: Stored::At(12345), stored_length: 10, size: 99, crc32c: None } },
            Record { key: b"d".to_vec(), rank: 2, hidden: false, value: Value::Reference { kind: KIND_RANGE, payload: vec![8, 1] } },
        ];
        for r in recs {
            let mut b = Vec::new();
            r.encode(&mut b);
            match parse_record(&b, true).unwrap() {
                Parsed::Whole(back, len) => {
                    assert_eq!(back, r);
                    assert_eq!(len, b.len());
                }
                Parsed::Needs(_) => panic!("whole"),
            }
            // the fields before the key are always under 64 bytes: a short read gives the length
            if b.len() > 40 {
                assert!(matches!(parse_record(&b[..40], true).unwrap(), Parsed::Needs(n) if n == b.len() as u64));
            }
        }
    }

    #[test]
    fn codecs_round_trip() {
        let data: Vec<u8> = (0..10_000u32).flat_map(|i| (i % 251).to_le_bytes()).collect();
        for codec in [Codec::None, Codec::Deflate, Codec::Zstd] {
            let s = encode(codec, &data);
            assert_eq!(decode(codec, &s, data.len() as u64).unwrap(), data);
            assert!(decode(codec, &s, data.len() as u64 - 1).is_err());
            assert!(decode(codec, &s, data.len() as u64 + 1).is_err());
            let mut trailing = s.clone();
            trailing.push(0);
            assert!(decode(codec, &trailing, data.len() as u64).is_err());
        }
    }

    #[test]
    fn superblock_round_trip() {
        let sb = Superblock { version: 0, flags: 1, distance: 4096, key_count: 3, directory_offset: 10, directory_length: 80, region_offset: 0, region_length: 99, directory_crc32c: 5 };
        let b = sb.encode();
        assert_eq!(Superblock::matching(&b, 4096), Some(sb));
        assert_eq!(Superblock::matching(&b, 0), None);
    }
}
