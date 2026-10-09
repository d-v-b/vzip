//! The values the CZI layout reads from the metadata XML (conventions/czi §2.7),
//! in one pass over the tag scan, with no tree: open elements are a stack, an end
//! tag closes back to the innermost open element of its name (a count per name
//! says at once whether there is one), and only elements on the paths the layout
//! reads are looked at. Linear in the XML; memory bounded by its nesting.

use crate::xml::{self, Ev};
use serde_json::{Value as J, json};
use std::collections::HashMap;

const DEPTH: usize = 10;
const INFO: [&str; 3] = ["Metadata", "Information", "Image"];
const SCALING: [&str; 3] = ["Metadata", "Scaling", "Items"];

#[derive(Default, Debug, Clone)]
pub struct Channel {
    pub name: Option<String>,
    pub color: Option<String>,
    pub bits: Option<u64>,
    pub low: Option<f64>,
    pub high: Option<f64>,
}

#[derive(Default, Debug, Clone)]
pub struct Values {
    pub px: Option<f64>,
    pub py: Option<f64>,
    pub pz: Option<f64>,
    pub inc: Option<f64>,
    pub bits: Option<u64>,
    pub info: Vec<Channel>,
    pub display: Vec<Channel>,
    /// S index -> the Name of its first Scene, in order of first appearance
    pub scenes: Vec<(u64, Option<String>)>,
}

impl Values {
    pub fn to_json(&self) -> J {
        let ch = |c: &Channel| json!({"name": c.name, "color": c.color, "bits": c.bits, "low": c.low, "high": c.high});
        json!({
            "px": self.px, "py": self.py, "pz": self.pz, "inc": self.inc, "bits": self.bits,
            "info": self.info.iter().map(ch).collect::<Vec<_>>(),
            "display": self.display.iter().map(ch).collect::<Vec<_>>(),
            "scenes": self.scenes.iter().map(|(i, n)| json!([i, n])).collect::<Vec<_>>(),
        })
    }
    pub fn scene(&self, i: i64) -> Option<&str> {
        self.scenes.iter().find(|s| s.0 as i64 == i).and_then(|s| s.1.as_deref())
    }
}

/// `[0-9]+`, at most 16 digits after leading zeros, at most 2^53 - 1.
pub fn integer(t: &str) -> Option<u64> {
    if t.is_empty() || !t.bytes().all(|c| c.is_ascii_digit()) || t.trim_start_matches('0').len() > 16 {
        return None;
    }
    let v: u64 = t.parse().ok()?;
    (v < (1 << 53)).then_some(v)
}

/// `#(?:[0-9A-Fa-f]{2})?([0-9A-Fa-f]{6})`: the last six digits, upper case.
pub fn color(t: &str) -> Option<String> {
    let h = t.strip_prefix('#')?;
    if !(h.len() == 6 || h.len() == 8) || !h.bytes().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    Some(h[h.len() - 6..].to_ascii_uppercase())
}

fn decimal(t: &str) -> Option<f64> {
    xml::decimal_value(t, false)
}

fn positive(v: Option<f64>) -> Option<f64> {
    v.filter(|x| *x > 0.0)
}

#[derive(Clone, Copy)]
enum Set {
    Inc,
    Bits,
    Axis(u8),
    Field(bool, usize, u8), // display?, channel index, field: 0 color, 1 bits, 2 low, 3 high
}

struct Open {
    name: String,
    path: Option<Vec<(String, u64)>>,
    kids: HashMap<String, u64>,
}

pub fn values(data: &[u8], max_xml: usize) -> Values {
    let mut out = Values::default();
    if data.len() > max_xml {
        return out;
    }
    let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data);
    let Ok(x) = std::str::from_utf8(data) else { return out };
    let mut stack: Vec<Open> = Vec::new();
    let mut count: HashMap<String, u64> = HashMap::new();
    let mut root_seen = false;
    let mut text_of: Option<(Set, String, usize)> = None; // (setter, pieces, pos)
    let mut axes: Vec<(u8, Vec<(String, u64)>)> = Vec::new();
    for ev in xml::scan(x) {
        let t = match ev {
            Ev::Skip(a, b) => {
                if let Some((_, pieces, pos)) = text_of.as_mut() {
                    pieces.push_str(&x[*pos..a]);
                    *pos = b;
                }
                continue;
            }
            Ev::Tag(t) => t,
        };
        if let Some((set, mut pieces, pos)) = text_of.take() {
            pieces.push_str(&x[pos..t.start]);
            apply(&mut out, set, xml::decode_refs(&pieces).trim_matches([' ', '\t', '\r', '\n']));
        }
        if t.is_end {
            if count.get(t.name).copied().unwrap_or(0) > 0 {
                while let Some(top) = stack.pop() {
                    *count.get_mut(&top.name).unwrap() -= 1;
                    if top.name == t.name {
                        break;
                    }
                }
            }
            continue;
        }
        let path = if let Some(parent) = stack.last_mut() {
            match &parent.path {
                Some(pp) if pp.len() < DEPTH => {
                    let r = parent.kids.entry(t.name.to_string()).or_insert(0);
                    let rank = *r;
                    *r += 1;
                    let mut p = pp.clone();
                    p.push((t.name.to_string(), rank));
                    Some(p)
                }
                _ => None,
            }
        } else if root_seen {
            None
        } else {
            root_seen = true;
            if t.name != "ImageDocument" {
                return out;
            }
            Some(Vec::new())
        };
        if let Some(p) = &path {
            if let Some(set) = role(p, &mut out, &mut axes, &t, x) {
                if t.self_closing {
                    apply(&mut out, set, "");
                } else {
                    text_of = Some((set, String::new(), t.end));
                }
            }
        }
        if !t.self_closing {
            *count.entry(t.name.to_string()).or_insert(0) += 1;
            stack.push(Open { name: t.name.to_string(), path, kids: HashMap::new() });
        }
    }
    if let Some((set, mut pieces, pos)) = text_of.take() {
        pieces.push_str(&x[pos..]);
        apply(&mut out, set, xml::decode_refs(&pieces).trim_matches([' ', '\t', '\r', '\n']));
    }
    out
}

fn apply(out: &mut Values, set: Set, t: &str) {
    match set {
        Set::Inc => out.inc = positive(decimal(t)),
        Set::Bits => out.bits = integer(t),
        Set::Axis(a) => {
            let v = positive(decimal(t));
            match a {
                b'X' => out.px = v,
                b'Y' => out.py = v,
                _ => out.pz = v,
            }
        }
        Set::Field(display, k, f) => {
            let ch = if display { &mut out.display[k] } else { &mut out.info[k] };
            match f {
                0 => ch.color = color(t),
                1 => ch.bits = integer(t),
                2 => ch.low = decimal(t),
                _ => ch.high = decimal(t),
            }
        }
    }
}

fn first_attr(t: &xml::TagRef, x: &str, name: &str) -> Option<String> {
    t.attr(x, name).map(|v| v.0)
}

/// What an element on a path is for (it may record its attributes now, and
/// return the setter of its text), or None when the layout does not read it.
/// Every element of the path is its parent's first of its name, but a `Distance`
/// or a `Channel` (any of them) and the element itself.
fn role(path: &[(String, u64)], out: &mut Values, axes: &mut Vec<(u8, Vec<(String, u64)>)>, t: &xml::TagRef, x: &str) -> Option<Set> {
    if path.is_empty() {
        return None;
    }
    let n = path.len();
    if path[..n - 1].iter().any(|(nm, r)| *r != 0 && nm != "Distance" && nm != "Channel") {
        return None;
    }
    let names: Vec<&str> = path.iter().map(|p| p.0.as_str()).collect();
    let rank = path[n - 1].1;
    let is = |prefix: &[&str], rest: &[&str]| names.len() == prefix.len() + rest.len() && names[..prefix.len()] == *prefix && names[prefix.len()..] == *rest;
    if rank == 0 && is(&INFO, &["Dimensions", "T", "Positions", "Interval", "Increment"]) {
        return Some(Set::Inc);
    }
    if rank == 0 && is(&INFO, &["ComponentBitCount"]) {
        return Some(Set::Bits);
    }
    if is(&SCALING, &["Distance"]) {
        if let Some(id) = first_attr(t, x, "Id") {
            if let Some(a) = ["X", "Y", "Z"].iter().position(|v| *v == id) {
                let a = b"XYZ"[a];
                if !axes.iter().any(|(k, _)| *k == a) {
                    axes.push((a, path.to_vec()));
                }
            }
        }
        return None;
    }
    if rank == 0 && is(&SCALING, &["Distance", "Value"]) {
        return axes.iter().find(|(_, p)| p[..] == path[..n - 1]).map(|(a, _)| Set::Axis(*a));
    }
    let channels = |prefix: &[&str]| -> Option<bool> {
        let info: Vec<&str> = INFO.iter().copied().chain(["Dimensions", "Channels"]).collect();
        if prefix == info.as_slice() {
            Some(false)
        } else if prefix == ["Metadata", "DisplaySetting", "Channels"] {
            Some(true)
        } else {
            None
        }
    };
    if names[n - 1] == "Channel" {
        if let Some(display) = channels(&names[..n - 1]) {
            let c = Channel { name: first_attr(t, x, "Name"), ..Default::default() };
            if display { out.display.push(c) } else { out.info.push(c) }
            return None;
        }
    }
    if n >= 2 && names[n - 2] == "Channel" && rank == 0 {
        if let Some(display) = channels(&names[..n - 2]) {
            let len = if display { out.display.len() } else { out.info.len() };
            if path[n - 2].1 + 1 != len as u64 {
                return None;
            }
            let f = match (display, names[n - 1]) {
                (_, "Color") => 0,
                (false, "ComponentBitCount") => 1,
                (true, "Low") => 2,
                (true, "High") => 3,
                _ => return None,
            };
            return Some(Set::Field(display, len - 1, f));
        }
    }
    if is(&INFO, &["Dimensions", "S", "Scenes", "Scene"]) {
        if let Some(i) = first_attr(t, x, "Index").as_deref().and_then(integer) {
            if !out.scenes.iter().any(|s| s.0 == i) {
                out.scenes.push((i, first_attr(t, x, "Name")));
            }
        }
        return None;
    }
    None
}
