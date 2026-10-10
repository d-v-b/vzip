// §3.2 tag scan over OME-XML text.

pub struct Tag {
    pub start: usize,
    pub end: usize, // exclusive
    pub is_end: bool,
    pub self_closing: bool,
    pub name: String, // local name
    pub attrs: Vec<(String, String)>, // raw (undecoded) values
}

impl Tag {
    /// First attribute of that name, references decoded.
    pub fn attr(&self, name: &str) -> Option<String> {
        self.attrs.iter().find(|(k, _)| k == name).map(|(_, v)| decode_refs(v))
    }
}

pub enum Tok {
    Tag(Tag),
    Skip(usize, usize), // skipped section [start, end)
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
    x[from..].windows(pat.len()).position(|w| w == pat).map(|p| p + from)
}

pub fn scan(x: &str) -> Vec<Tok> {
    let b = x.as_bytes();
    let mut toks = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'<' {
            i += 1;
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
            let e = match find(b, i + ml, endm) {
                Some(p) => p + endm.len(),
                None => b.len(),
            };
            toks.push(Tok::Skip(i, e));
            i = e;
            continue;
        }
        if let Some(t) = match_tag(x, i) {
            i = t.end;
            toks.push(Tok::Tag(t));
        } else {
            i += 1;
        }
    }
    toks
}

fn match_tag(x: &str, i: usize) -> Option<Tag> {
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
    let mut attrs = Vec::new();
    loop {
        let mut q = p;
        while at(q).is_some_and(is_ws) {
            q += 1;
        }
        if q > p {
            if let Some((k, v, q2)) = match_attr(x, q) {
                attrs.push((k, v));
                p = q2;
                continue;
            }
        }
        p = q;
        break;
    }
    let mut sc = false;
    if at(p) == Some(b'/') {
        sc = true;
        p += 1;
    }
    if at(p) != Some(b'>') {
        return None;
    }
    Some(Tag { start: i, end: p + 1, is_end, self_closing: sc, name: local.to_string(), attrs })
}

fn match_attr(x: &str, q: usize) -> Option<(String, String, usize)> {
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
    Some((name, x[vs..ve].to_string(), ve + 1))
}

/// Decode the five predefined entities and numeric character references.
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
        for (ent, c) in [("&lt;", '<'), ("&gt;", '>'), ("&amp;", '&'), ("&quot;", '"'), ("&apos;", '\'')] {
            if rest.starts_with(ent) {
                rep = Some((c, ent.len()));
            }
        }
        if rep.is_none() {
            let (digits_start, radix) = if rest.starts_with("&#x") {
                (3, 16)
            } else if rest.starts_with("&#") {
                (2, 10)
            } else {
                (0, 0)
            };
            if radix != 0 {
                let rb = rest.as_bytes();
                let mut j = digits_start;
                while j < rb.len() && (if radix == 16 { rb[j].is_ascii_hexdigit() } else { rb[j].is_ascii_digit() }) {
                    j += 1;
                }
                if j > digits_start && j < rb.len() && rb[j] == b';' {
                    // Value, saturating to avoid overflow.
                    let mut v: u64 = 0;
                    for &d in &rb[digits_start..j] {
                        let dv = (d as char).to_digit(radix).unwrap() as u64;
                        v = v.saturating_mul(radix as u64).saturating_add(dv);
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
