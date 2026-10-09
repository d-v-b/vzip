//! vzip's intermediate representation (ARCHITECTURE.md §3), in Rust: a compact
//! columnar IR with runs, its invariants, sans-IO parsers, and the checker of a
//! source model schema. The core has no I/O and builds for wasm32; the Python
//! bindings are behind the `python` feature.

pub mod canon;
pub mod transform;
pub mod check;
pub mod coding;
pub mod cxml;
pub mod czi;
pub mod expr;
pub mod fed;
pub mod ir;
pub mod lv;
pub mod mirror;
pub mod nd2;
pub mod out;
pub mod plan;
pub mod project;
pub mod refs;
pub mod rules;
pub mod run;
pub mod schema;
pub mod tiff;
pub mod types;
pub mod xml;

#[cfg(feature = "python")]
mod py;

#[cfg(target_arch = "wasm32")]
pub mod wasm;

/// A sans-IO parser: `step` gives the next batch of ranges to read (or done),
/// `feed` gives one range's bytes, `finish` the IR and the facts its projection reads.
pub trait Parser {
    fn new(size: u64) -> Self;
    fn step(&mut self) -> Result<nd2::Step, String>;
    fn feed(&mut self, offset: u64, data: Vec<u8>);
    fn finish(self) -> Result<(ir::Ir, serde_json::Value), String>;
}

macro_rules! parser {
    ($t:ty) => {
        impl Parser for $t {
            fn new(size: u64) -> Self {
                <$t>::new(size)
            }
            fn step(&mut self) -> Result<nd2::Step, String> {
                <$t>::step(self)
            }
            fn feed(&mut self, offset: u64, data: Vec<u8>) {
                <$t>::feed(self, offset, data)
            }
            fn finish(self) -> Result<(ir::Ir, serde_json::Value), String> {
                <$t>::finish(self)
            }
        }
    };
}

parser!(nd2::Nd2);
parser!(tiff::Tiff);
parser!(czi::Czi);

/// Runs a parser over bytes in memory (tests, tools).
pub fn parse_bytes<P: Parser>(data: &[u8]) -> Result<(ir::Ir, serde_json::Value), String> {
    let mut p = P::new(data.len() as u64);
    loop {
        match p.step()? {
            nd2::Step::Read(r) => {
                for (o, n) in r {
                    p.feed(o, data[o as usize..(o + n) as usize].to_vec());
                }
            }
            nd2::Step::Done => break,
        }
    }
    p.finish()
}
