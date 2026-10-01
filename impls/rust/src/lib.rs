//! vzip: a ZIP container for byte-range references (format version 0).

pub mod desc;
pub mod error;
pub mod http;
pub mod httpdate;
pub mod proto;
pub mod reader;
pub mod uri;
pub mod writer;
pub mod zip;

pub use error::{ErrorClass, VzError};
pub use reader::{Archive, Kind, Request};
pub use writer::{write_archive, WriteSpec};
