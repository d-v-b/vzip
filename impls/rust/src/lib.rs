//! vzip: a ZIP container for byte-range references (format version 0).

pub mod http;
pub mod proto;
pub mod reader;
pub mod uri;
pub mod writer;

pub use reader::{Archive, Kind, Request};

pub const HIDDEN_PREFIX: &str = "__vz__/";
pub const SOURCES_KEY: &str = "__vz__/sources";
pub const INDEX_KEY: &str = "__vz__/index";

/// Error classes of spec §8.4.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Archive(String),
    Entry(String),
    Body(String),
    Payload(String),
    Resolution(String),
    Request(String),
}

impl Error {
    pub fn class(&self) -> &'static str {
        match self {
            Error::Archive(_) => "archive",
            Error::Entry(_) => "entry",
            Error::Body(_) => "body",
            Error::Payload(_) => "payload",
            Error::Resolution(_) => "resolution",
            Error::Request(_) => "request",
        }
    }
    pub fn message(&self) -> &str {
        match self {
            Error::Archive(m)
            | Error::Entry(m)
            | Error::Body(m)
            | Error::Payload(m)
            | Error::Resolution(m)
            | Error::Request(m) => m,
        }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} error: {}", self.class(), self.message())
    }
}

impl std::error::Error for Error {}

/// `DQUOTE *etagc DQUOTE` with etagc = %x21 / %x23-7E (§6.1).
pub fn is_strong_etag(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() >= 2
        && b[0] == b'"'
        && b[b.len() - 1] == b'"'
        && b[1..b.len() - 1].iter().all(|&c| c == 0x21 || (0x23..=0x7e).contains(&c))
}
