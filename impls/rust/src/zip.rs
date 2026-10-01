//! ZIP structure helpers shared by reader and writer.

pub const SIG_LOCAL: u32 = 0x04034b50;
pub const SIG_CDR: u32 = 0x02014b50;
pub const SIG_EOCD: u32 = 0x06054b50;
pub const SIG_ZIP64_EOCD: u32 = 0x06064b50;
pub const SIG_ZIP64_LOC: u32 = 0x07064b50;

pub const ID_RANGE: u16 = 0x7A76;
pub const ID_CONCAT: u16 = 0x7A77;
pub const ID_ZIP64: u16 = 0x0001;

pub const RESERVED_PREFIX: &str = "__vz__/";
pub const SOURCES_KEY: &str = "__vz__/sources";
pub const INDEX_KEY: &str = "__vz__/index";

pub fn le16(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}
pub fn le32(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
pub fn le64(b: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

/// A central directory record, as stored.
#[derive(Debug, Clone)]
pub struct CdRecord {
    pub flags: u16,
    pub method: u16,
    pub crc: u32,
    pub csize: u32,
    pub usize: u32,
    pub lho: u32,
    pub name: Vec<u8>,
    pub extra: Vec<u8>,
}

impl CdRecord {
    /// The key named by this record, if its name is non-empty valid UTF-8.
    pub fn key(&self) -> Option<&str> {
        if self.name.is_empty() {
            return None;
        }
        std::str::from_utf8(&self.name).ok()
    }
}

/// Parse a byte slice as a sequence of whole central directory records that
/// exactly fills it.
pub fn parse_records(buf: &[u8]) -> Result<Vec<CdRecord>, String> {
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos < buf.len() {
        if buf.len() - pos < 46 {
            return Err(format!("truncated central directory record at offset {pos}"));
        }
        let r = &buf[pos..];
        if le32(r, 0) != SIG_CDR {
            return Err(format!("bad central directory record signature at offset {pos}"));
        }
        let nlen = le16(r, 28) as usize;
        let xlen = le16(r, 30) as usize;
        let clen = le16(r, 32) as usize;
        let total = 46 + nlen + xlen + clen;
        if r.len() < total {
            return Err(format!("central directory record at offset {pos} overruns its container"));
        }
        out.push(CdRecord {
            flags: le16(r, 8),
            method: le16(r, 10),
            crc: le32(r, 16),
            csize: le32(r, 20),
            usize: le32(r, 24),
            lho: le32(r, 42),
            name: r[46..46 + nlen].to_vec(),
            extra: r[46 + nlen..46 + nlen + xlen].to_vec(),
        });
        pos += total;
    }
    Ok(out)
}

/// Parse an extra field into (id, data) blocks that exactly fill it.
pub fn parse_extra(extra: &[u8]) -> Option<Vec<(u16, &[u8])>> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < extra.len() {
        if extra.len() - pos < 4 {
            return None;
        }
        let id = le16(extra, pos);
        let len = le16(extra, pos + 2) as usize;
        if extra.len() - pos - 4 < len {
            return None;
        }
        out.push((id, &extra[pos + 4..pos + 4 + len]));
        pos += 4 + len;
    }
    Some(out)
}

pub fn is_hidden(key: &str) -> bool {
    key.starts_with(RESERVED_PREFIX)
}
