//! vzip archives, refkv's ZIP binding (spec/bindings/vzip.md): the protobuf messages of
//! spec/proto/refkv.proto ([`pb`]), URI references ([`uri`]), and the archive writer
//! ([`Writer`]), which writes the archive python/src/refkv/vzip/archive.py's `VZipWriter`
//! writes, byte for byte.

pub mod pb;
pub mod uri;
mod writer;

pub use pb::{CdIndex, Page, Pinned, Range, Source, SourceKind};
pub use writer::{deflate, EntryLayout, Layout, Options, Writer, MAX_PAYLOAD, SPEC_REVISION};

/// The prefix of every hidden entry, and the format entries the writer writes itself
/// (spec/bindings/vzip.md §11.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#114-hidden-entries>).
pub const RESERVED_PREFIX: &str = "__vz__/";
pub const SOURCES_KEY: &str = "__vz__/sources";
pub const INDEX_KEY: &str = "__vz__/index";

/// The extra field header IDs of a reference entry's payload: a Range, or a Concat
/// (spec/bindings/vzip.md §11.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#113-reference-entries>).
pub const RANGE_EXTRA_ID: u16 = 0x7a76;
pub const CONCAT_EXTRA_ID: u16 = 0x7a77;

/// A writer's failure: what it was asked to write breaks a writer requirement (spec/refkv.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>, spec/bindings/vzip.md §14.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#141-requirements>), or the
/// output could not be written.
#[derive(Debug)]
pub enum Error {
    Invalid(String),
    Io(std::io::Error),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Invalid(m) => f.write_str(m),
            Error::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Error {
        Error::Io(e)
    }
}
