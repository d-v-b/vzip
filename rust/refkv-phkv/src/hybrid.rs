//! The hybrid writer (spec/bindings/phkv.md §21
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#21-hybrid-profile>): a vzip
//! archive, written by rust/refkv-vzip's `Writer`, whose last entry before the central directory
//! is `__vz__/phkv`, a phkv container over the archive's own entries. Bytes values and the source
//! table stay in their ZIP entries; the container's records point at their bodies.

use crate::format::{self, section, Codec, DirEntry, Prop, Stored, Value};
use crate::writer::{build_tail, invalid, Entry, Options, Props, TailInput};
use crate::Error;
use refkv_vzip::pb::{Range, Source};
use refkv_vzip::{Layout, CONCAT_EXTRA_ID, RANGE_EXTRA_ID, RESERVED_PREFIX, SOURCES_KEY};
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::path::Path;

/// The name of the hidden entry, after the reserved prefix.
const ENTRY_NAME: &str = "phkv";

/// Writes a hybrid store: a vzip archive with a phkv container in `__vz__/phkv`.
pub struct HybridWriter<W: Write> {
    zip: refkv_vzip::Writer<W>,
    opts: Options,
    /// the CRC-32C of each bytes value the writer was given
    crcs: HashMap<String, u32>,
    props: Props,
}

impl<W: Write> HybridWriter<W> {
    /// `zip` says how the archive is written; it must mirror reference payloads in their bodies,
    /// as the hybrid profile requires. Of `opts`, the codec and inlining of values do not apply:
    /// values are the ZIP entries' bodies.
    pub fn new(w: W, zip: refkv_vzip::Options, opts: Options) -> Result<HybridWriter<W>, Error> {
        if !zip.mirror_refs {
            return invalid("a hybrid store's reference entries carry their payloads in their bodies (mirror_refs)");
        }
        if opts.distance != 0 {
            return invalid("a hybrid store's distance is set by its central directory");
        }
        Ok(HybridWriter { zip: refkv_vzip::Writer::new(w, zip), opts, crcs: HashMap::new(), props: Props::default() })
    }

    pub fn source(&mut self, src: Source) -> Result<u32, Error> {
        Ok(self.zip.source(src)?)
    }

    pub fn url(&mut self, url: &str) -> Result<u32, Error> {
        Ok(self.zip.url(url)?)
    }

    pub fn internal(&mut self, key: &str) -> Result<u32, Error> {
        Ok(self.zip.internal(key)?)
    }

    pub fn blob(&mut self, data: &[u8]) -> Result<u32, Error> {
        Ok(self.zip.blob(data)?)
    }

    /// Adds a bytes entry, DEFLATE-compressed when `compress`; `late` as rust/refkv-vzip's
    /// `Writer::add_bytes` says.
    pub fn add_bytes(&mut self, key: &str, data: &[u8], compress: bool, late: bool) -> Result<(), Error> {
        self.zip.add_bytes(key, data, compress, late)?;
        if self.opts.crc32c {
            self.crcs.insert(key.to_string(), format::crc32c(data));
        }
        Ok(())
    }

    /// Adds the bytes of the file at `path` as a STORED entry.
    pub fn add_file(&mut self, key: &str, path: &Path) -> Result<(), Error> {
        self.zip.add_file(key, path)?;
        if self.opts.crc32c {
            let mut f = std::fs::File::open(path)?;
            let (mut buf, mut crc) = (vec![0u8; 1 << 24], 0u32);
            loop {
                let n = f.read(&mut buf)?;
                if n == 0 {
                    break;
                }
                crc = crc32c::crc32c_append(crc, &buf[..n]);
            }
            self.crcs.insert(key.to_string(), crc);
        }
        Ok(())
    }

    /// Adds a hidden entry, `__vz__/<name>`; `__vz__/phkv` is the container's own.
    pub fn add_hidden(&mut self, name: &str, data: &[u8], compress: bool, late: bool) -> Result<String, Error> {
        if name == ENTRY_NAME {
            return invalid(format!("{:?} is the hybrid store's container", format::HYBRID_KEY));
        }
        let key = self.zip.add_hidden(name, data, compress, late)?;
        if self.opts.crc32c {
            self.crcs.insert(key.clone(), format::crc32c(data));
        }
        Ok(key)
    }

    pub fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), Error> {
        Ok(self.zip.add_ranges(key, ranges)?)
    }

    pub fn set_property(&mut self, key: &str, name: &str, value: Prop) -> Result<(), Error> {
        self.props.set(key, name, value)
    }

    pub fn set_metadata(&mut self, name: &str, value: &[u8]) -> Result<(), Error> {
        self.props.set_metadata(name, value)
    }

    /// Writes the archive, with the container last before the central directory.
    pub fn close(self) -> Result<W, Error> {
        let HybridWriter { zip, opts, crcs, props, .. } = self;
        let mut failure: Option<Error> = None;
        let mut build = |layout: &Layout| -> Result<Vec<u8>, refkv_vzip::Error> {
            container(layout, &opts, &crcs, &props).map_err(|e| {
                let m = e.to_string();
                failure = Some(e);
                refkv_vzip::Error::Invalid(m)
            })
        };
        let r = zip.close_with_trailer(ENTRY_NAME, &mut build);
        match (r, failure) {
            (Ok(w), _) => Ok(w),
            (Err(_), Some(e)) => Err(e),
            (Err(e), None) => Err(e.into()),
        }
    }
}

/// The body of `__vz__/phkv` for the archive's layout: the container, then zero bytes up to the
/// probe distance its superblock is at.
fn container(layout: &Layout, opts: &Options, crcs: &HashMap<String, u32>, props: &Props) -> Result<Vec<u8>, Error> {
    let mut entries: Vec<Entry> = Vec::with_capacity(layout.entries.len());
    let mut sources_section = None;
    for e in layout.entries {
        let value = match &e.payload {
            Some((id, payload)) if *id == RANGE_EXTRA_ID || *id == CONCAT_EXTRA_ID => {
                let kind = if *id == RANGE_EXTRA_ID { format::KIND_RANGE } else { format::KIND_CONCAT };
                Value::Reference { kind, payload: payload.clone() }
            }
            _ => {
                let codec = match e.method {
                    0 => Codec::None,
                    8 => Codec::Deflate,
                    m => return invalid(format!("{:?} uses method {m}", e.name)),
                };
                Value::Bytes { codec, stored: Stored::At(e.body_offset), stored_length: e.csize, size: e.size, crc32c: crcs.get(&e.name).copied() }
            }
        };
        if e.name == SOURCES_KEY {
            sources_section = Some(DirEntry {
                kind: section::SOURCES,
                codec: Codec::Deflate as u8,
                flags: 0,
                offset: e.body_offset,
                length: e.csize,
                size: e.size,
                crc32c: format::crc32c(layout.sources_stored),
            });
        }
        entries.push(Entry { key: e.name.clone(), hidden: e.name.starts_with(RESERVED_PREFIX), value });
    }
    let names: HashSet<&str> = layout.entries.iter().map(|e| e.name.as_str()).collect();
    props.check_keys(|k| names.contains(k))?;
    let d = format::probe_distance_at_least(layout.tail_len).ok_or_else(|| Error::Invalid("the central directory exceeds the largest probe distance".into()))?;
    let padding = d - layout.tail_len;
    // the source table is already the body of __vz__/sources: the container only points at it
    let sources = crate::writer::Sources::default();
    let tail = build_tail(
        TailInput { entries: &mut entries, sources: &sources, sources_section, props, opts, hybrid: true },
        layout.body_offset,
        d,
        layout.body_offset,
        padding,
    )?;
    debug_assert_eq!(tail.superblock_end, layout.body_offset + tail.bytes.len() as u64);
    let mut body = tail.bytes;
    body.resize(body.len() + padding as usize, 0);
    Ok(body)
}
