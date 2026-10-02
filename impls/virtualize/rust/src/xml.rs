//! The OME-XML tag scan of §3.2.

pub struct Tag {
    pub start: usize,
    pub end: usize, // index after '>'
    pub is_end: bool,
    pub self_closing: bool,
    pub name: String, // local name
    attrs: Vec<(String, String)>, // raw (undecoded) values
}

impl Tag {
    /// First attribute with this exact name, references decoded.
    pub fn attr(&self, name: &str) -> Option<String> {
        self.attrs.iter().find(|(k, _)| k == name).map(|(_, v)| decode_refs(v))
    }
    pub fn is_start(&self, name: &str) -> bool {
        !self.is_end && self.name == name
    }
    pub fn is_end_of(&self, name: &str) -> bool {
        self.is_end && self.name == name
    }
}

pub struct Scan {
    pub tags: Vec<Tag>,
    pub skipped: Vec<(usize, usize)>,
}

fn is_ws(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\r' | b'\n')
}

fn is_name_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-')
}

fn find_from(x: &[u8], from: usize, pat: &[u8]) -> Option<usize> {
    if from > x.len() {
        return None;
    }
    x[from..].windows(pat.len()).position(|w| w == pat).map(|p| p + from)
}

pub fn scan(x: &[u8]) -> Scan {
    let mut tags = Vec::new();
    let mut skipped = Vec::new();
    let n = x.len();
    let mut i = 0;
    while i < n {
        if x[i] != b'<' {
            i += 1;
            continue;
        }
        let rest = &x[i..];
        let section: Option<(&[u8], usize)> = if rest.starts_with(b"<!--") {
            Some((b"-->", 4))
        } else if rest.starts_with(b"<![CDATA[") {
            Some((b"]]>", 9))
        } else if rest.starts_with(b"<?") {
            Some((b"?>", 2))
        } else if rest.starts_with(b"<!") {
            Some((b">", 2))
        } else {
            None
        };
        if let Some((close, open_len)) = section {
            let end = match find_from(x, i + open_len, close) {
                Some(p) => p + close.len(),
                None => n,
            };
            skipped.push((i, end));
            i = end;
            continue;
        }
        match parse_tag(x, i) {
            Some(t) => {
                i = t.end;
                tags.push(t);
            }
            None => i += 1,
        }
    }
    Scan { tags, skipped }
}

fn parse_name(x: &[u8], mut p: usize) -> Option<usize> {
    let s = p;
    while p < x.len() && is_name_char(x[p]) {
        p += 1;
    }
    if p > s { Some(p) } else { None }
}

fn parse_attr(x: &[u8], mut p: usize) -> Option<(usize, String, String)> {
    let s = p;
    while p < x.len() && !is_ws(x[p]) && !matches!(x[p], b'=' | b'/' | b'>' | b'"' | b'\'' | b'<') {
        p += 1;
    }
    if p == s {
        return None;
    }
    let name = String::from_utf8_lossy(&x[s..p]).to_string();
    while p < x.len() && is_ws(x[p]) {
        p += 1;
    }
    if p >= x.len() || x[p] != b'=' {
        return None;
    }
    p += 1;
    while p < x.len() && is_ws(x[p]) {
        p += 1;
    }
    if p >= x.len() || !(x[p] == b'"' || x[p] == b'\'') {
        return None;
    }
    let q = x[p];
    p += 1;
    let vs = p;
    while p < x.len() && x[p] != q {
        p += 1;
    }
    if p >= x.len() {
        return None;
    }
    let val = String::from_utf8_lossy(&x[vs..p]).to_string();
    Some((p + 1, name, val))
}

fn parse_tag(x: &[u8], start: usize) -> Option<Tag> {
    let n = x.len();
    let mut p = start + 1;
    let mut is_end = false;
    if p < n && x[p] == b'/' {
        is_end = true;
        p += 1;
    }
    let s1 = p;
    p = parse_name(x, p)?;
    let mut local = (s1, p);
    if p < n && x[p] == b':' {
        let s2 = p + 1;
        p = parse_name(x, s2)?;
        local = (s2, p);
    }
    let mut attrs = Vec::new();
    loop {
        let save = p;
        let mut q = p;
        while q < n && is_ws(x[q]) {
            q += 1;
        }
        if q == save {
            break;
        }
        match parse_attr(x, q) {
            Some((np, k, v)) => {
                attrs.push((k, v));
                p = np;
            }
            None => {
                p = save;
                break;
            }
        }
    }
    while p < n && is_ws(x[p]) {
        p += 1;
    }
    let mut self_closing = false;
    if p < n && x[p] == b'/' {
        self_closing = true;
        p += 1;
    }
    if p < n && x[p] == b'>' {
        Some(Tag {
            start,
            end: p + 1,
            is_end,
            self_closing,
            name: String::from_utf8_lossy(&x[local.0..local.1]).to_string(),
            attrs,
        })
    } else {
        None
    }
}

/// Decodes the five predefined entities and numeric character references.
pub fn decode_refs(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut lit = 0; // start of pending literal run
    while i < b.len() {
        if b[i] != b'&' {
            i += 1;
            continue;
        }
        if let Some((rep, len)) = match_ref(&b[i..]) {
            out.push_str(&s[lit..i]);
            out.push(rep);
            i += len;
            lit = i;
        } else {
            i += 1;
        }
    }
    out.push_str(&s[lit..]);
    out
}

fn match_ref(b: &[u8]) -> Option<(char, usize)> {
    for (name, c) in [("&lt;", '<'), ("&gt;", '>'), ("&amp;", '&'), ("&quot;", '"'), ("&apos;", '\'')] {
        if b.starts_with(name.as_bytes()) {
            return Some((c, name.len()));
        }
    }
    if !b.starts_with(b"&#") {
        return None;
    }
    let (hex, mut p) = if b.len() > 2 && b[2] == b'x' { (true, 3) } else { (false, 2) };
    let ds = p;
    let mut v: u64 = 0;
    while p < b.len() && (if hex { b[p].is_ascii_hexdigit() } else { b[p].is_ascii_digit() }) {
        let d = (b[p] as char).to_digit(16).unwrap() as u64;
        v = v.saturating_mul(if hex { 16 } else { 10 }).saturating_add(d);
        p += 1;
    }
    if p == ds || p >= b.len() || b[p] != b';' {
        return None;
    }
    if v == 0 || v > 0x10FFFF {
        return None;
    }
    let c = char::from_u32(v as u32)?; // None for surrogates
    Some((c, p + 1))
}

pub fn trim_ws(s: &str) -> &str {
    s.trim_matches(|c| matches!(c, ' ' | '\t' | '\r' | '\n'))
}
