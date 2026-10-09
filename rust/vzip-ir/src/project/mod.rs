//! The image projections (ARCHITECTURE.md §3.4): a valid IR and its facts into
//! today's Zarr hierarchy (conventions/{tiff,czi,nd2} §4, and ND2's root source
//! metadata of §5.1). A projection checks nothing (the parser checked the schema)
//! and reads nothing (the IR holds what it needs).

mod czi;
mod nd2;
mod tiff;

use crate::ir::Ir;
use crate::out::Out;
use crate::run::Format;
use serde_json::{Map, Value as J};
use std::fmt::Write;

/// The image projection of `format`: today's hierarchy, without `vzip_source`.
pub fn project(format: Format, ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    match format {
        Format::Tiff => tiff::project(ir, facts, url),
        Format::Czi => czi::project(ir, facts, url),
        Format::Nd2 => nd2::project(ir, facts, url),
    }
}

// ---- reading the facts (JSON the parsers wrote)

/// A count or index of the facts (an integer, or a number written as a float).
fn n(v: &J) -> u64 {
    v.as_u64().or_else(|| v.as_f64().map(|f| f as u64)).unwrap_or(0)
}

/// An array of the facts (empty when absent).
fn arr(v: &J) -> &[J] {
    v.as_array().map(|a| a.as_slice()).unwrap_or(&[])
}

/// An object of the facts (empty when absent).
fn obj(v: &J) -> Map<String, J> {
    v.as_object().cloned().unwrap_or_default()
}

/// Axis names.
fn strs(v: &J) -> Vec<&str> {
    arr(v).iter().filter_map(|a| a.as_str()).collect()
}

/// A JSON value's truth as Python's.
fn truthy(v: &J) -> bool {
    match v {
        J::Null => false,
        J::Bool(b) => *b,
        J::Number(x) => x.as_f64().is_some_and(|f| f != 0.0),
        J::String(s) => !s.is_empty(),
        J::Array(a) => !a.is_empty(),
        J::Object(o) => !o.is_empty(),
    }
}

/// `translations` as `image_ome` takes them: None when absent or empty.
fn translations(v: &J) -> Option<&[J]> {
    v.as_array().filter(|a| !a.is_empty()).map(|a| a.as_slice())
}

/// A chunk key: `prefix` and the coordinates, joined by "/".
fn key(prefix: &str, coords: impl IntoIterator<Item = u64>) -> String {
    let mut k = String::from(prefix);
    for (i, c) in coords.into_iter().enumerate() {
        if i > 0 {
            k.push('/');
        }
        let _ = write!(k, "{c}");
    }
    k
}
