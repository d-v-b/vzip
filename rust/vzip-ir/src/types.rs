//! The declared types of `value` elements and the one generic decoder (the
//! round-1 grammar of `vzip/ir/types.py`, plus `utf16[n]` and the text types of
//! parsed documents):
//!
//! ```text
//! type  = base ["[" n {"," n} "]"]
//! base  = ["<" | ">"] num | "ascii" | "cstr" | "bytes" | "utf16" | "guid" | "{" field {"," field} "}"
//! num   = "u1" | "i1" | "u2" | "i2" | "u4" | "i4" | "u8" | "i8" | "f4" | "f8"
//! field = name ":" type
//! ```
//!
//! `utf16[n]` decodes to its bytes (a reader decides how to read code units);
//! `cstr[n]` to its text up to the first NUL, or, when bytes after that NUL are
//! not all NUL, to `{text, after}` (the bytes from the NUL on), so that no byte
//! of a name is lost to the decoding;
//! `xml` and `xml:<runtype>` are the text of an XML attribute value, as UTF-8.

#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    Int(i128),
    Float(f64),
    Str(String),
    Bytes(Vec<u8>),
    List(Vec<Val>),
    Rec(Vec<(String, Val)>),
}

#[derive(Clone, Debug)]
pub enum Ty {
    Num {
        code: String,
        big: bool,
        size: usize,
        dims: Vec<usize>,
    },
    Text {
        kind: String,
        size: usize,
    },
    Rec {
        fields: Vec<(String, Ty)>,
        size: usize,
        dims: Vec<usize>,
    },
    Xml,
}

impl Ty {
    pub fn size(&self) -> Option<usize> {
        match self {
            Ty::Num { size, dims, .. } => Some(size * dims.iter().product::<usize>()),
            Ty::Text { size, .. } => Some(*size),
            Ty::Rec { size, dims, .. } => Some(size * dims.iter().product::<usize>()),
            Ty::Xml => None,
        }
    }
}

fn tokens(s: &str) -> Vec<String> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if c.is_ascii_whitespace() {
            i += 1;
        } else if b"{}[],:".contains(&c) {
            out.push((c as char).to_string());
            i += 1;
        } else if c.is_ascii_digit() {
            let j = i;
            while i < b.len() && b[i].is_ascii_digit() {
                i += 1;
            }
            out.push(s[j..i].to_string());
        } else {
            let j = i;
            if c == b'<' || c == b'>' {
                i += 1;
            }
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
                i += 1;
            }
            if i == j {
                i += 1;
            }
            out.push(s[j..i].to_string());
        }
    }
    out
}

pub fn parse(s: &str) -> Result<Ty, String> {
    if s == "xml" || s.starts_with("xml:") {
        return Ok(Ty::Xml);
    }
    let t = tokens(s);
    let mut pos = 0;
    let ty = one(&t, &mut pos, s)?;
    if pos != t.len() {
        return Err(format!("bad type {s:?}"));
    }
    Ok(ty)
}

fn one(t: &[String], pos: &mut usize, s: &str) -> Result<Ty, String> {
    let bad = || format!("bad type {s:?}");
    let tok = t.get(*pos).ok_or_else(bad)?.clone();
    *pos += 1;
    let mut base = if tok == "{" {
        let mut fields = Vec::new();
        loop {
            let name = t.get(*pos).ok_or_else(bad)?.clone();
            *pos += 1;
            if t.get(*pos).map(|x| x.as_str()) != Some(":") {
                return Err(bad());
            }
            *pos += 1;
            let f = one(t, pos, s)?;
            fields.push((name, f));
            let sep = t.get(*pos).ok_or_else(bad)?.clone();
            *pos += 1;
            if sep == "}" {
                break;
            }
            if sep != "," {
                return Err(bad());
            }
        }
        let size = fields.iter().map(|(_, f)| f.size().unwrap_or(0)).sum();
        Ty::Rec {
            fields,
            size,
            dims: vec![],
        }
    } else {
        let big = tok.starts_with('>');
        let name = tok.trim_start_matches(['<', '>']);
        match name {
            "u1" | "i1" | "u2" | "i2" | "u4" | "i4" | "u8" | "i8" | "f4" | "f8" => {
                let size = name[1..].parse::<usize>().unwrap();
                Ty::Num {
                    code: name.to_string(),
                    big,
                    size,
                    dims: vec![],
                }
            }
            "ascii" | "cstr" | "bytes" | "utf16" => Ty::Text {
                kind: name.to_string(),
                size: 1,
            },
            "guid" => Ty::Text {
                kind: "guid".into(),
                size: 16,
            },
            _ => return Err(bad()),
        }
    };
    let mut dims = Vec::new();
    if t.get(*pos).map(|x| x.as_str()) == Some("[") {
        *pos += 1;
        loop {
            let n = t
                .get(*pos)
                .ok_or_else(bad)?
                .parse::<usize>()
                .map_err(|_| bad())?;
            *pos += 1;
            dims.push(n);
            let sep = t.get(*pos).ok_or_else(bad)?.clone();
            *pos += 1;
            if sep == "]" {
                break;
            }
        }
    }
    if !dims.is_empty() {
        base = match base {
            Ty::Text { kind, .. } if kind != "guid" => Ty::Text {
                kind,
                size: dims[0],
            },
            Ty::Num {
                code, big, size, ..
            } => Ty::Num {
                code,
                big,
                size,
                dims,
            },
            Ty::Rec { fields, size, .. } => Ty::Rec { fields, size, dims },
            other => other,
        };
    }
    Ok(base)
}

fn num(code: &str, big: bool, b: &[u8]) -> Val {
    macro_rules! rd {
        ($t:ty) => {{
            let a: [u8; std::mem::size_of::<$t>()] = b.try_into().unwrap();
            if big {
                <$t>::from_be_bytes(a)
            } else {
                <$t>::from_le_bytes(a)
            }
        }};
    }
    match code {
        "u1" => Val::Int(b[0] as i128),
        "i1" => Val::Int(b[0] as i8 as i128),
        "u2" => Val::Int(rd!(u16) as i128),
        "i2" => Val::Int(rd!(i16) as i128),
        "u4" => Val::Int(rd!(u32) as i128),
        "i4" => Val::Int(rd!(i32) as i128),
        "u8" => Val::Int(rd!(u64) as i128),
        "i8" => Val::Int(rd!(i64) as i128),
        "f4" => Val::Float(rd!(f32) as f64),
        _ => Val::Float(rd!(f64)),
    }
}

fn shape(mut flat: Vec<Val>, dims: &[usize]) -> Val {
    for &d in dims[1..].iter().rev() {
        let mut out = Vec::new();
        let mut it = flat.into_iter();
        loop {
            let chunk: Vec<Val> = it.by_ref().take(d).collect();
            if chunk.is_empty() {
                break;
            }
            out.push(Val::List(chunk));
        }
        flat = out;
    }
    Val::List(flat)
}

pub fn decode(ty: &Ty, raw: &[u8]) -> Result<Val, String> {
    if let Some(n) = ty.size() {
        if n != raw.len() {
            return Err(format!("{} bytes for a type of {n}", raw.len()));
        }
    }
    Ok(match ty {
        Ty::Num {
            code,
            big,
            size,
            dims,
        } => {
            let flat: Vec<Val> = raw.chunks(*size).map(|c| num(code, *big, c)).collect();
            if dims.is_empty() {
                flat.into_iter().next().unwrap()
            } else {
                shape(flat, dims)
            }
        }
        Ty::Text { kind, .. } => match kind.as_str() {
            "bytes" | "utf16" => Val::Bytes(raw.to_vec()),
            "guid" => {
                let a = u32::from_le_bytes(raw[0..4].try_into().unwrap());
                let b = u16::from_le_bytes(raw[4..6].try_into().unwrap());
                let c = u16::from_le_bytes(raw[6..8].try_into().unwrap());
                let hex = |x: &[u8]| x.iter().map(|v| format!("{v:02x}")).collect::<String>();
                Val::Str(format!(
                    "{a:08x}-{b:04x}-{c:04x}-{}-{}",
                    hex(&raw[8..10]),
                    hex(&raw[10..16])
                ))
            }
            _ => {
                let n = if kind == "cstr" {
                    raw.iter().position(|&c| c == 0).unwrap_or(raw.len())
                } else {
                    raw.len()
                };
                let text = match std::str::from_utf8(&raw[..n]) {
                    Ok(s) => Val::Str(s.to_string()),
                    Err(_) => Val::Bytes(raw[..n].to_vec()),
                };
                // a C string is its bytes up to the first NUL, and nothing is lost:
                // bytes after it that are not all NUL are kept beside it
                if raw[n..].iter().any(|&c| c != 0) {
                    Val::Rec(vec![("text".into(), text), ("after".into(), Val::Bytes(raw[n..].to_vec()))])
                } else {
                    text
                }
            }
        },
        Ty::Rec { fields, size, dims } => {
            let count: usize = dims.iter().product();
            let mut items = Vec::new();
            for k in 0..count {
                let mut at = k * size;
                let mut rec = Vec::new();
                for (name, f) in fields {
                    let n = f.size().unwrap_or(0);
                    rec.push((name.clone(), decode(f, &raw[at..at + n])?));
                    at += n;
                }
                items.push(Val::Rec(rec));
            }
            if dims.is_empty() {
                items.into_iter().next().unwrap()
            } else {
                shape(items, dims)
            }
        }
        Ty::Xml => Val::Str(String::from_utf8_lossy(raw).into_owned()),
    })
}
