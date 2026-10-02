//! The tag scanner of VIRTUALIZE.md §3.2 (not a validating XML parser).

#[derive(Debug)]
pub enum Ev {
    Start { name: String, attrs: Vec<(String, String)>, self_closing: bool },
    End { name: String },
    Text(String),
}

fn is_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

/// The name without its namespace prefix.
fn local(name: &str) -> String {
    match name.find(':') {
        Some(i) => name[i + 1..].to_string(),
        None => name.to_string(),
    }
}

/// Decodes the five predefined entities and numeric character references.
pub fn decode_entities(v: &str) -> String {
    let mut out = String::with_capacity(v.len());
    let mut rest = v;
    while let Some(i) = rest.find('&') {
        out.push_str(&rest[..i]);
        rest = &rest[i..];
        let semi = rest.find(';');
        let decoded = semi.and_then(|j| {
            let ent = &rest[1..j];
            let ch = match ent {
                "lt" => Some('<'),
                "gt" => Some('>'),
                "amp" => Some('&'),
                "quot" => Some('"'),
                "apos" => Some('\''),
                _ => {
                    let num = if let Some(h) = ent.strip_prefix("#x") {
                        if !h.is_empty() && h.bytes().all(|b| b.is_ascii_hexdigit()) {
                            u32::from_str_radix(h, 16).ok()
                        } else {
                            None
                        }
                    } else if let Some(d) = ent.strip_prefix('#') {
                        if !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit()) {
                            d.parse::<u32>().ok()
                        } else {
                            None
                        }
                    } else {
                        None
                    };
                    num.and_then(char::from_u32)
                }
            };
            ch.map(|c| (c, j))
        });
        match decoded {
            Some((c, j)) => {
                out.push(c);
                rest = &rest[j + 1..];
            }
            None => {
                out.push('&');
                rest = &rest[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn find_from(b: &[u8], from: usize, pat: &[u8]) -> Option<usize> {
    if from > b.len() {
        return None;
    }
    b[from..].windows(pat.len()).position(|w| w == pat).map(|p| p + from)
}

/// Scans `x` into a sequence of start tags, end tags and text.
pub fn scan(x: &str) -> Vec<Ev> {
    let b = x.as_bytes();
    let n = b.len();
    let mut evs = Vec::new();
    let mut i = 0;
    while i < n {
        if b[i] != b'<' {
            let j = find_from(b, i, b"<").unwrap_or(n);
            evs.push(Ev::Text(x[i..j].to_string()));
            i = j;
            continue;
        }
        let rest = &b[i..];
        let skip_to = |pat: &[u8], start: usize| find_from(b, start, pat).map(|p| p + pat.len()).unwrap_or(n);
        if rest.starts_with(b"<!--") {
            i = skip_to(b"-->", i + 4);
        } else if rest.starts_with(b"<![CDATA[") {
            i = skip_to(b"]]>", i + 9);
        } else if rest.starts_with(b"<?") {
            i = skip_to(b"?>", i + 2);
        } else if rest.starts_with(b"<!") {
            i = skip_to(b">", i + 2);
        } else if rest.starts_with(b"</") {
            let mut j = i + 2;
            while j < n && !is_ws(b[j]) && b[j] != b'>' {
                j += 1;
            }
            let name = local(&x[i + 2..j]);
            i = skip_to(b">", j);
            evs.push(Ev::End { name });
        } else {
            // Start tag.
            let mut j = i + 1;
            while j < n && !is_ws(b[j]) && b[j] != b'/' && b[j] != b'>' {
                j += 1;
            }
            let name = local(&x[i + 1..j]);
            let mut attrs = Vec::new();
            let mut self_closing = false;
            loop {
                while j < n && is_ws(b[j]) {
                    j += 1;
                }
                if j >= n {
                    break;
                }
                if b[j] == b'>' {
                    j += 1;
                    break;
                }
                if b[j] == b'/' {
                    if j + 1 < n && b[j + 1] == b'>' {
                        self_closing = true;
                        j += 2;
                        break;
                    }
                    j += 1;
                    continue;
                }
                let a0 = j;
                while j < n && !is_ws(b[j]) && b[j] != b'=' && b[j] != b'>' && b[j] != b'/' {
                    j += 1;
                }
                let aname = x[a0..j].to_string();
                while j < n && is_ws(b[j]) {
                    j += 1;
                }
                if j < n && b[j] == b'=' {
                    j += 1;
                    while j < n && is_ws(b[j]) {
                        j += 1;
                    }
                    if j < n && (b[j] == b'"' || b[j] == b'\'') {
                        let q = b[j];
                        let v0 = j + 1;
                        let v1 = find_from(b, v0, &[q]).unwrap_or(n);
                        attrs.push((aname, decode_entities(&x[v0..v1])));
                        j = (v1 + 1).min(n);
                    } else {
                        let v0 = j;
                        while j < n && !is_ws(b[j]) && b[j] != b'>' {
                            j += 1;
                        }
                        attrs.push((aname, decode_entities(&x[v0..j])));
                    }
                } else if !aname.is_empty() {
                    attrs.push((aname, String::new()));
                }
            }
            evs.push(Ev::Start { name, attrs, self_closing });
            i = j;
        }
    }
    evs
}

/// True when `x` contains a start tag whose local name is `OME`, followed by
/// whitespace, `/` or `>` (§3.2). Tags inside comments etc. do not count.
pub fn has_ome_start_tag(x: &str) -> bool {
    // Scan like `scan`, but also require the character after the name.
    let b = x.as_bytes();
    let n = b.len();
    let mut i = 0;
    while let Some(p) = find_from(b, i, b"<") {
        let rest = &b[p..];
        let skip_to = |pat: &[u8], start: usize| find_from(b, start, pat).map(|q| q + pat.len()).unwrap_or(n);
        if rest.starts_with(b"<!--") {
            i = skip_to(b"-->", p + 4);
        } else if rest.starts_with(b"<![CDATA[") {
            i = skip_to(b"]]>", p + 9);
        } else if rest.starts_with(b"<?") {
            i = skip_to(b"?>", p + 2);
        } else if rest.starts_with(b"<!") {
            i = skip_to(b">", p + 2);
        } else if rest.starts_with(b"</") {
            i = p + 2;
        } else {
            let mut j = p + 1;
            while j < n && !is_ws(b[j]) && b[j] != b'/' && b[j] != b'>' {
                j += 1;
            }
            if j < n && local(&x[p + 1..j]) == "OME" {
                return true;
            }
            // Skip the rest of the tag, honoring quoted attribute values.
            let mut q: Option<u8> = None;
            while j < n {
                match q {
                    Some(c) if b[j] == c => q = None,
                    Some(_) => {}
                    None if b[j] == b'"' || b[j] == b'\'' => q = Some(b[j]),
                    None if b[j] == b'>' => break,
                    None => {}
                }
                j += 1;
            }
            i = j;
        }
    }
    false
}
