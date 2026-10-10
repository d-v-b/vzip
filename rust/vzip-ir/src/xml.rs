//! XML variant chunks (spec/virtualize/nd2.md §5.1): the tag scan of the TIFF
//! convention's OME-XML reading (spec/virtualize/tiff.md §3), and the `variant` document
//! as a tree with the positions of its elements and value attributes.

pub struct Tag {
    pub start: usize,
    pub end: usize,
    pub is_end: bool,
    pub self_closing: bool,
    pub name: String,
    /// (name, raw value, value span)
    pub attrs: Vec<(String, String, (usize, usize))>,
}

impl Tag {
    /// The first attribute of that name, references decoded, and its raw span.
    pub fn attr(&self, name: &str) -> Option<(String, (usize, usize))> {
        self.attrs
            .iter()
            .find(|(k, _, _)| k == name)
            .map(|(_, v, s)| (decode_refs(v), *s))
    }
}

fn is_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

fn is_name(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_' || b == b'.' || b == b'-'
}

fn find(x: &[u8], from: usize, pat: &[u8]) -> Option<usize> {
    if from > x.len() {
        return None;
    }
    x[from..]
        .windows(pat.len())
        .position(|w| w == pat)
        .map(|p| p + from)
}

/// One step of the scan: a skipped section (comment, CDATA, processing
/// instruction or declaration) or a tag.
pub enum Ev<'a> {
    Skip(usize, usize),
    Tag(TagRef<'a>),
}

/// A tag, borrowed from the text: its attributes are read when asked for.
#[derive(Clone, Copy)]
pub struct TagRef<'a> {
    pub start: usize,
    pub end: usize,
    pub is_end: bool,
    pub self_closing: bool,
    pub name: &'a str,
    attrs: (usize, usize),
}

impl<'a> TagRef<'a> {
    /// Its attributes in order: (name, raw value, value span).
    pub fn attrs(&self, x: &str) -> Vec<(String, String, (usize, usize))> {
        let b = x.as_bytes();
        let mut out = Vec::new();
        let mut p = self.attrs.0;
        while p < self.attrs.1 {
            let mut q = p;
            while q < b.len() && is_ws(b[q]) {
                q += 1;
            }
            match match_attr(x, q) {
                Some((k, v, span, q2)) if q > p => {
                    out.push((k, v, span));
                    p = q2;
                }
                _ => break,
            }
        }
        out
    }

    /// The first attribute of that name, references decoded, and its raw span.
    pub fn attr(&self, x: &str, name: &str) -> Option<(String, (usize, usize))> {
        self.attrs(x)
            .into_iter()
            .find(|(k, _, _)| k == name)
            .map(|(_, v, s)| (decode_refs(&v), s))
    }

    pub fn owned(&self, x: &str) -> Tag {
        Tag {
            start: self.start,
            end: self.end,
            is_end: self.is_end,
            self_closing: self.self_closing,
            name: self.name.to_string(),
            attrs: self.attrs(x),
        }
    }
}

/// The scan of spec/virtualize/tiff.md §3 as an iterator: it holds nothing but its position.
pub struct Scan<'a> {
    x: &'a str,
    i: usize,
}

pub fn scan(x: &str) -> Scan<'_> {
    Scan { x, i: 0 }
}

impl<'a> Iterator for Scan<'a> {
    type Item = Ev<'a>;
    fn next(&mut self) -> Option<Ev<'a>> {
        let b = self.x.as_bytes();
        while self.i < b.len() {
            let i = self.i;
            if b[i] != b'<' {
                self.i = match b[i..].iter().position(|&c| c == b'<') {
                    Some(k) => i + k,
                    None => b.len(),
                };
                continue;
            }
            let rest = &b[i..];
            let skip: Option<(usize, &[u8])> = if rest.starts_with(b"<!--") {
                Some((4, b"-->"))
            } else if rest.starts_with(b"<![CDATA[") {
                Some((9, b"]]>"))
            } else if rest.starts_with(b"<?") {
                Some((2, b"?>"))
            } else if rest.starts_with(b"<!") {
                Some((2, b">"))
            } else {
                None
            };
            if let Some((ml, endm)) = skip {
                self.i = match find(b, i + ml, endm) {
                    Some(p) => p + endm.len(),
                    None => b.len(),
                };
                return Some(Ev::Skip(i, self.i));
            }
            if let Some(t) = match_tag(self.x, i) {
                self.i = t.end;
                return Some(Ev::Tag(t));
            }
            self.i = i + 1;
        }
        None
    }
}

/// The tags of `x`, in order (comments, CDATA, processing instructions and
/// declarations skipped).
pub fn tags(x: &str) -> Vec<Tag> {
    scan(x)
        .filter_map(|e| match e {
            Ev::Tag(t) => Some(t.owned(x)),
            Ev::Skip(..) => None,
        })
        .collect()
}

fn match_tag(x: &str, i: usize) -> Option<TagRef<'_>> {
    let b = x.as_bytes();
    let at = |p: usize| -> Option<u8> { b.get(p).copied() };
    let mut p = i + 1;
    let mut is_end = false;
    if at(p) == Some(b'/') {
        is_end = true;
        p += 1;
    }
    let n1 = p;
    while at(p).is_some_and(is_name) {
        p += 1;
    }
    if p == n1 {
        return None;
    }
    let mut local = &x[n1..p];
    if at(p) == Some(b':') {
        p += 1;
        let n2 = p;
        while at(p).is_some_and(is_name) {
            p += 1;
        }
        if p == n2 {
            return None;
        }
        local = &x[n2..p];
    }
    let a0 = p;
    loop {
        let mut q = p;
        while at(q).is_some_and(is_ws) {
            q += 1;
        }
        if q > p {
            if let Some((_, _, _, q2)) = match_attr(x, q) {
                p = q2;
                continue;
            }
        }
        break;
    }
    let a1 = p;
    while at(p).is_some_and(is_ws) {
        p += 1;
    }
    let mut sc = false;
    if at(p) == Some(b'/') {
        sc = true;
        p += 1;
    }
    if at(p) != Some(b'>') {
        return None;
    }
    Some(TagRef {
        start: i,
        end: p + 1,
        is_end,
        self_closing: sc,
        name: local,
        attrs: (a0, a1),
    })
}

fn match_attr(x: &str, q: usize) -> Option<(String, String, (usize, usize), usize)> {
    let b = x.as_bytes();
    let at = |p: usize| -> Option<u8> { b.get(p).copied() };
    let mut p = q;
    while let Some(c) = at(p) {
        if is_ws(c) || matches!(c, b'=' | b'/' | b'>' | b'"' | b'\'' | b'<') {
            break;
        }
        p += 1;
    }
    if p == q {
        return None;
    }
    let name = x[q..p].to_string();
    while at(p).is_some_and(is_ws) {
        p += 1;
    }
    if at(p) != Some(b'=') {
        return None;
    }
    p += 1;
    while at(p).is_some_and(is_ws) {
        p += 1;
    }
    let quote = at(p)?;
    if quote != b'"' && quote != b'\'' {
        return None;
    }
    let vs = p + 1;
    let ve = vs + b[vs..].iter().position(|&c| c == quote)?;
    Some((name, x[vs..ve].to_string(), (vs, ve), ve + 1))
}

/// The five predefined entities and numeric character references (others kept).
pub fn decode_refs(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut last = 0;
    while i < b.len() {
        if b[i] != b'&' {
            i += 1;
            continue;
        }
        let rest = &s[i..];
        let mut rep: Option<(char, usize)> = None;
        for (ent, c) in [
            ("&lt;", '<'),
            ("&gt;", '>'),
            ("&amp;", '&'),
            ("&quot;", '"'),
            ("&apos;", '\''),
        ] {
            if rest.starts_with(ent) {
                rep = Some((c, ent.len()));
            }
        }
        if rep.is_none() {
            let (ds, radix) = if rest.starts_with("&#x") {
                (3, 16)
            } else if rest.starts_with("&#") {
                (2, 10)
            } else {
                (0, 0)
            };
            if radix != 0 {
                let rb = rest.as_bytes();
                let mut j = ds;
                while j < rb.len()
                    && (if radix == 16 {
                        rb[j].is_ascii_hexdigit()
                    } else {
                        rb[j].is_ascii_digit()
                    })
                {
                    j += 1;
                }
                if j > ds && j < rb.len() && rb[j] == b';' {
                    let mut v: u64 = 0;
                    for &d in &rb[ds..j] {
                        v = v
                            .saturating_mul(radix as u64)
                            .saturating_add((d as char).to_digit(radix).unwrap() as u64);
                    }
                    if v != 0 && v <= 0x10FFFF {
                        if let Some(c) = char::from_u32(v as u32) {
                            rep = Some((c, j + 1));
                        }
                    }
                }
            }
        }
        if let Some((c, l)) = rep {
            out.push_str(&s[last..i]);
            out.push(c);
            i += l;
            last = i;
        } else {
            i += 1;
        }
    }
    out.push_str(&s[last..]);
    out
}

pub const MAX_XML_DEPTH: usize = 100;

/// An element of a variant document: its name, its span, and either its value
/// attribute (runtype, value, raw span) or its child elements.
#[derive(Debug, Clone)]
pub struct El {
    pub name: String,
    pub span: (usize, usize),
    pub value: Option<(Option<String>, String, (usize, usize))>,
    pub children: Vec<El>,
}

/// The `variant` document element, or None when the chunk is not a variant
/// document (not UTF-8, mismatched or missing end tags, content after the
/// document element, another document element, a `variant` with a value, or
/// nesting past 100).
pub fn variant(data: &[u8]) -> Option<El> {
    let x = std::str::from_utf8(data).ok()?;
    let mut stack: Vec<El> = Vec::new();
    let mut root: Option<El> = None;
    for t in tags(x) {
        if root.is_some() {
            return None;
        }
        let el = if t.is_end {
            if stack.last().map(|e| e.name.as_str()) != Some(t.name.as_str()) {
                return None;
            }
            let mut e = stack.pop().unwrap();
            e.span.1 = t.end;
            e
        } else {
            if stack.len() > MAX_XML_DEPTH {
                return None;
            }
            let value = t
                .attr("value")
                .map(|(v, span)| (t.attr("runtype").map(|r| r.0), v, span));
            let e = El {
                name: t.name.clone(),
                span: (t.start, t.end),
                value,
                children: Vec::new(),
            };
            if !t.self_closing {
                stack.push(e);
                continue;
            }
            e
        };
        if let Some(top) = stack.last_mut() {
            if top.value.is_none() {
                top.children.push(el);
            } else {
                // the children of an element with a value attribute are not kept
            }
        } else if el.name == "variant" && el.value.is_none() {
            root = Some(el);
        } else {
            return None;
        }
    }
    if stack.is_empty() { root } else { None }
}

// ---- values (spec/virtualize/nd2.md §5.1)

#[derive(Debug, Clone, PartialEq)]
pub enum Scalar {
    Int(i128), // within ±(2^53 - 1); larger integers are Tag
    Float(f64),
    Bool(bool),
    Str(String),
    Tag,
}

pub(crate) fn decimal(v: &str) -> bool {
    // [+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?
    let b = v.as_bytes();
    let mut i = 0;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    let d0 = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    let int_digits = i - d0;
    if i < b.len() && b[i] == b'.' {
        i += 1;
        let f0 = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if int_digits == 0 && i == f0 {
            return false;
        }
    } else if int_digits == 0 {
        return false;
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        let e0 = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == e0 {
            return false;
        }
    }
    i == b.len()
}

const MAX_SAFE: i128 = (1 << 53) - 1;

/// An XML variant value as the convention reads it (its JSON, up to tags).
pub fn scalar(runtype: Option<&str>, value: &str) -> Scalar {
    let ints = [
        "lx_int8",
        "lx_int16",
        "lx_int32",
        "lx_int64",
        "lx_uint8",
        "lx_uint16",
        "lx_uint32",
        "lx_uint64",
    ];
    if let Some(rt) = runtype {
        let b = value.as_bytes();
        let digits = b
            .strip_prefix(b"+")
            .or_else(|| b.strip_prefix(b"-"))
            .unwrap_or(b);
        if ints.contains(&rt) && !digits.is_empty() && digits.iter().all(|c| c.is_ascii_digit()) {
            let stripped: &[u8] = {
                let k = digits
                    .iter()
                    .position(|&c| c != b'0')
                    .unwrap_or(digits.len());
                &digits[k..]
            };
            if stripped.len() > 20 {
                return Scalar::Tag;
            }
            let mut n: i128 = 0;
            for &c in stripped {
                n = n * 10 + (c - b'0') as i128;
            }
            if b[0] == b'-' {
                n = -n;
            }
            return if n.abs() <= MAX_SAFE {
                Scalar::Int(n)
            } else {
                Scalar::Tag
            };
        }
        if (rt == "double" || rt == "float") && decimal(value) {
            let f: f64 = value.parse().unwrap_or(f64::NAN);
            return if f.is_finite() && !(f == 0.0 && f.is_sign_negative()) {
                Scalar::Float(f)
            } else {
                Scalar::Tag
            };
        }
        if rt == "bool" && (value == "true" || value == "false") {
            return Scalar::Bool(value == "true");
        }
    }
    Scalar::Str(value.to_string())
}

/// An element read as an object (spec/virtualize/nd2.md §5.2): its children by name,
/// first position, last value; None for an element with a value.
pub fn as_object(e: &El) -> Option<Vec<(String, &El)>> {
    if e.value.is_some() {
        return None;
    }
    let mut out: Vec<(String, &El)> = Vec::new();
    for c in &e.children {
        match out.iter_mut().find(|(n, _)| *n == c.name) {
            Some(slot) => slot.1 = c,
            None => out.push((c.name.clone(), c)),
        }
    }
    Some(out)
}

/// The streams `CustomDataV2_0` declares (spec/virtualize/nd2.md §5.2): (ID, data type, member index).
pub fn declared_streams(doc: &El) -> Vec<(String, &'static str, usize)> {
    let mut out: Vec<(String, &'static str, usize)> = Vec::new();
    let Some(top) = as_object(doc) else {
        return out;
    };
    let Some(tags) = top
        .iter()
        .find(|(n, _)| n == "CustomTagDescription_v1.0")
        .and_then(|(_, e)| as_object(e))
    else {
        return out;
    };
    for (i, (_, tag)) in tags.iter().enumerate() {
        let Some(t) = as_object(tag) else { continue };
        let get = |k: &str| t.iter().find(|(n, _)| n == k).map(|(_, e)| *e);
        let read = |e: &El| -> Option<Scalar> {
            e.value.as_ref().map(|(rt, v, _)| scalar(rt.as_deref(), v))
        };
        let sid = get("ID").and_then(read);
        let typ = get("Type").and_then(read);
        let dt = match typ {
            Some(Scalar::Int(2)) => Some("int32"),
            Some(Scalar::Int(3)) => Some("float64"),
            Some(Scalar::Float(f)) if f == 2.0 => Some("int32"),
            Some(Scalar::Float(f)) if f == 3.0 => Some("float64"),
            _ => None,
        };
        if let (Some(Scalar::Str(sid)), Some(dt)) = (sid, dt) {
            if !out.iter().any(|(s, _, _)| *s == sid) {
                out.push((sid, dt, i));
            }
        }
    }
    out
}

/// A decimal value (spec/virtualize/tiff.md §3), finite (and above 0 when `positive`), or None.
pub fn decimal_value(v: &str, positive: bool) -> Option<f64> {
    if !decimal(v) {
        return None;
    }
    let x: f64 = v.parse().ok()?;
    (x.is_finite() && (x > 0.0 || !positive)).then_some(x)
}
