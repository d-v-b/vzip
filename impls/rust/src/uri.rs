//! RFC 3986 URI-reference validation, parsing and strict resolution (§5.2.2),
//! plus the vzip `file:` mapping and base-URI construction (spec §6).

use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UriRef {
    pub scheme: Option<String>,
    pub authority: Option<String>,
    pub path: String,
    pub query: Option<String>,
    pub fragment: Option<String>,
}

fn is_unreserved(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'-' | b'.' | b'_' | b'~')
}
fn is_sub_delim(c: u8) -> bool {
    matches!(c, b'!' | b'$' | b'&' | b'\'' | b'(' | b')' | b'*' | b'+' | b',' | b';' | b'=')
}

/// Check that `s` consists of `allowed` characters and well-formed pct-encodings.
fn check_chars(s: &[u8], allowed: impl Fn(u8) -> bool) -> bool {
    let mut i = 0;
    while i < s.len() {
        let c = s[i];
        if c == b'%' {
            if i + 2 >= s.len() {
                return false;
            }
            if !(s[i + 1].is_ascii_hexdigit() && s[i + 2].is_ascii_hexdigit()) {
                return false;
            }
            i += 3;
        } else if allowed(c) {
            i += 1;
        } else {
            return false;
        }
    }
    true
}

fn is_pchar(c: u8) -> bool {
    is_unreserved(c) || is_sub_delim(c) || c == b':' || c == b'@'
}

fn valid_scheme(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b[0].is_ascii_alphabetic()
        && b.iter().all(|&c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'-' | b'.'))
}

fn valid_dec_octet(s: &str) -> bool {
    if s.is_empty() || s.len() > 3 || !s.bytes().all(|c| c.is_ascii_digit()) {
        return false;
    }
    if s.len() > 1 && s.starts_with('0') {
        return false;
    }
    s.parse::<u32>().map(|v| v <= 255).unwrap_or(false)
}

fn valid_ipv4(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    parts.len() == 4 && parts.iter().all(|p| valid_dec_octet(p))
}

fn valid_h16(s: &str) -> bool {
    !s.is_empty() && s.len() <= 4 && s.bytes().all(|c| c.is_ascii_hexdigit())
}

/// Count of 16-bit pieces in a `:`-separated list (the last may be IPv4), or None.
fn h16_list(s: &str, allow_v4_tail: bool) -> Option<usize> {
    if s.is_empty() {
        return Some(0);
    }
    let parts: Vec<&str> = s.split(':').collect();
    let mut n = 0;
    for (i, p) in parts.iter().enumerate() {
        if i == parts.len() - 1 && allow_v4_tail && p.contains('.') {
            if !valid_ipv4(p) {
                return None;
            }
            n += 2;
        } else if valid_h16(p) {
            n += 1;
        } else {
            return None;
        }
    }
    Some(n)
}

fn valid_ipv6(s: &str) -> bool {
    if let Some(idx) = s.find("::") {
        let (head, tail) = (&s[..idx], &s[idx + 2..]);
        if tail.contains("::") {
            return false;
        }
        let h = match h16_list(head, false) {
            Some(n) => n,
            None => return false,
        };
        let t = match h16_list(tail, true) {
            Some(n) => n,
            None => return false,
        };
        h + t <= 7
    } else {
        h16_list(s, true) == Some(8)
    }
}

fn valid_ip_literal(s: &str) -> bool {
    // s excludes the brackets
    let b = s.as_bytes();
    if b.first().map(|c| *c == b'v' || *c == b'V').unwrap_or(false) {
        // IPvFuture = "v" 1*HEXDIG "." 1*( unreserved / sub-delims / ":" )
        let rest = &s[1..];
        let dot = match rest.find('.') {
            Some(d) => d,
            None => return false,
        };
        let (hex, tail) = (&rest[..dot], &rest[dot + 1..]);
        return !hex.is_empty()
            && hex.bytes().all(|c| c.is_ascii_hexdigit())
            && !tail.is_empty()
            && tail.bytes().all(|c| is_unreserved(c) || is_sub_delim(c) || c == b':');
    }
    valid_ipv6(s)
}

fn valid_authority(a: &str) -> bool {
    let (userinfo, hostport) = match a.find('@') {
        Some(i) => (Some(&a[..i]), &a[i + 1..]),
        None => (None, a),
    };
    if let Some(u) = userinfo {
        if !check_chars(u.as_bytes(), |c| is_unreserved(c) || is_sub_delim(c) || c == b':') {
            return false;
        }
    }
    let (host_ok, port) = if hostport.starts_with('[') {
        match hostport.find(']') {
            Some(end) => {
                let lit = &hostport[1..end];
                let rest = &hostport[end + 1..];
                let port = if rest.is_empty() {
                    Some("")
                } else if let Some(p) = rest.strip_prefix(':') {
                    Some(p)
                } else {
                    None
                };
                match port {
                    Some(p) => (valid_ip_literal(lit), p),
                    None => return false,
                }
            }
            None => return false,
        }
    } else {
        let (host, port) = match hostport.find(':') {
            Some(i) => (&hostport[..i], &hostport[i + 1..]),
            None => (hostport, ""),
        };
        (check_chars(host.as_bytes(), |c| is_unreserved(c) || is_sub_delim(c)), port)
    };
    host_ok && port.bytes().all(|c| c.is_ascii_digit())
}

/// Parse a string that must match RFC 3986 `URI-reference` exactly.
pub fn parse_uri_reference(s: &str) -> Option<UriRef> {
    if !s.is_ascii() {
        return None;
    }
    // Scheme: a ':' before any '/', '?', '#'.
    let mut rest = s;
    let mut scheme = None;
    if let Some(i) = s.find(|c| c == ':' || c == '/' || c == '?' || c == '#') {
        if s.as_bytes()[i] == b':' {
            let cand = &s[..i];
            if valid_scheme(cand) {
                scheme = Some(cand.to_string());
                rest = &s[i + 1..];
            }
            // otherwise: relative-ref whose first segment contains ':' -> rejected below
        }
    }
    let (before_frag, fragment) = match rest.find('#') {
        Some(i) => (&rest[..i], Some(&rest[i + 1..])),
        None => (rest, None),
    };
    let (before_query, query) = match before_frag.find('?') {
        Some(i) => (&before_frag[..i], Some(&before_frag[i + 1..])),
        None => (before_frag, None),
    };
    let (authority, path) = if let Some(after) = before_query.strip_prefix("//") {
        match after.find('/') {
            Some(i) => (Some(&after[..i]), &after[i..]),
            None => (Some(after), ""),
        }
    } else {
        (None, before_query)
    };
    if let Some(a) = authority {
        if !valid_authority(a) {
            return None;
        }
    }
    if !check_chars(path.as_bytes(), |c| is_pchar(c) || c == b'/') {
        return None;
    }
    if authority.is_none() {
        if path.starts_with("//") {
            return None; // cannot happen (would be authority), kept for clarity
        }
        if scheme.is_none() {
            // path-noscheme: first segment must not contain ':'
            let first = path.split('/').next().unwrap_or("");
            if first.contains(':') {
                return None;
            }
        }
    }
    let qf_ok = |x: &str| check_chars(x.as_bytes(), |c| is_pchar(c) || c == b'/' || c == b'?');
    if let Some(q) = query {
        if !qf_ok(q) {
            return None;
        }
    }
    if let Some(f) = fragment {
        if !qf_ok(f) {
            return None;
        }
    }
    Some(UriRef {
        scheme,
        authority: authority.map(|a| a.to_string()),
        path: path.to_string(),
        query: query.map(|q| q.to_string()),
        fragment: fragment.map(|f| f.to_string()),
    })
}

pub fn is_uri_reference(s: &str) -> bool {
    parse_uri_reference(s).is_some()
}

/// RFC 3986 §5.2.4
pub fn remove_dot_segments(input: &str) -> String {
    let mut input = input.to_string();
    let mut output = String::new();
    while !input.is_empty() {
        if input.starts_with("../") {
            input.drain(..3);
        } else if input.starts_with("./") {
            input.drain(..2);
        } else if input.starts_with("/./") {
            input.replace_range(..3, "/");
        } else if input == "/." {
            input = "/".to_string();
        } else if input.starts_with("/../") || input == "/.." {
            if input == "/.." {
                input = "/".to_string();
            } else {
                input.replace_range(..4, "/");
            }
            match output.rfind('/') {
                Some(i) => output.truncate(i),
                None => output.clear(),
            }
        } else if input == "." || input == ".." {
            input.clear();
        } else {
            let start = if input.starts_with('/') { 1 } else { 0 };
            let end = input[start..].find('/').map(|i| i + start).unwrap_or(input.len());
            output.push_str(&input[..end]);
            input.drain(..end);
        }
    }
    output
}

fn merge(base: &UriRef, rel_path: &str) -> String {
    if base.authority.is_some() && base.path.is_empty() {
        format!("/{rel_path}")
    } else {
        match base.path.rfind('/') {
            Some(i) => format!("{}{}", &base.path[..=i], rel_path),
            None => rel_path.to_string(),
        }
    }
}

/// RFC 3986 §5.2.2, strict.
pub fn resolve(base: &UriRef, r: &UriRef) -> UriRef {
    let (scheme, authority, path, query);
    if r.scheme.is_some() {
        scheme = r.scheme.clone();
        authority = r.authority.clone();
        path = remove_dot_segments(&r.path);
        query = r.query.clone();
    } else {
        if r.authority.is_some() {
            authority = r.authority.clone();
            path = remove_dot_segments(&r.path);
            query = r.query.clone();
        } else {
            if r.path.is_empty() {
                path = base.path.clone();
                query = if r.query.is_some() { r.query.clone() } else { base.query.clone() };
            } else {
                if r.path.starts_with('/') {
                    path = remove_dot_segments(&r.path);
                } else {
                    path = remove_dot_segments(&merge(base, &r.path));
                }
                query = r.query.clone();
            }
            authority = base.authority.clone();
        }
        scheme = base.scheme.clone();
    }
    UriRef { scheme, authority, path, query, fragment: r.fragment.clone() }
}

impl UriRef {
    pub fn to_string(&self) -> String {
        let mut s = String::new();
        if let Some(sc) = &self.scheme {
            s.push_str(sc);
            s.push(':');
        }
        if let Some(a) = &self.authority {
            s.push_str("//");
            s.push_str(a);
        }
        s.push_str(&self.path);
        if let Some(q) = &self.query {
            s.push('?');
            s.push_str(q);
        }
        if let Some(f) = &self.fragment {
            s.push('#');
            s.push_str(f);
        }
        s
    }

    pub fn scheme_lower(&self) -> Option<String> {
        self.scheme.as_ref().map(|s| s.to_ascii_lowercase())
    }
}

fn hexval(c: u8) -> u8 {
    match c {
        b'0'..=b'9' => c - b'0',
        b'a'..=b'f' => c - b'a' + 10,
        _ => c - b'A' + 10,
    }
}

/// Map a `file:` URI to a local path (spec §6). Errors are messages for a
/// resolution error.
pub fn file_uri_to_path(u: &UriRef) -> Result<PathBuf, String> {
    if u.scheme_lower().as_deref() != Some("file") {
        return Err("not a file: URI".into());
    }
    if let Some(a) = &u.authority {
        if !(a.is_empty() || a.eq_ignore_ascii_case("localhost")) {
            return Err(format!("file: URI has a non-local authority {a:?}"));
        }
    }
    if !u.path.starts_with('/') {
        return Err("file: URI path is not absolute".into());
    }
    if u.query.is_some() {
        return Err("file: URI has a query component".into());
    }
    let b = u.path.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            if i + 2 >= b.len() || !b[i + 1].is_ascii_hexdigit() || !b[i + 2].is_ascii_hexdigit() {
                return Err("bad percent-encoding".into());
            }
            let v = hexval(b[i + 1]) * 16 + hexval(b[i + 2]);
            if v == b'/' {
                return Err("file: URI path contains an encoded '/'".into());
            }
            if v == 0 {
                return Err("file: URI path contains a NUL byte".into());
            }
            out.push(v);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    for seg in out.split(|&c| c == b'/') {
        if seg == b"." || seg == b".." {
            return Err("file: URI path contains a dot segment after decoding".into());
        }
    }
    use std::os::unix::ffi::OsStringExt;
    Ok(PathBuf::from(std::ffi::OsString::from_vec(out)))
}

/// Build the base URI of an archive opened from a local path (spec §6).
pub fn base_uri_for_path(p: &Path) -> std::io::Result<String> {
    use std::os::unix::ffi::OsStrExt;
    let abs = if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir()?.join(p)
    };
    let bytes = abs.as_os_str().as_bytes();
    let mut segs: Vec<&[u8]> = Vec::new();
    for seg in bytes.split(|&c| c == b'/') {
        if seg.is_empty() || seg == b"." {
            continue;
        }
        if seg == b".." {
            segs.pop();
            continue;
        }
        segs.push(seg);
    }
    let mut path = Vec::new();
    if segs.is_empty() {
        path.push(b'/');
    }
    for s in segs {
        path.push(b'/');
        path.extend_from_slice(s);
    }
    let mut out = String::from("file://");
    for &c in &path {
        if is_unreserved(c) || is_sub_delim(c) || c == b':' || c == b'@' || c == b'/' {
            out.push(c as char);
        } else {
            out.push_str(&format!("%{:02X}", c));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validation() {
        for ok in [
            "", "a", "a%20b.bin", "%C3%A9.bin", "#x", "?q", "//host/p", "http://h:80/p?q#f",
            "file:///x", "file:/x", "../a/b", "http://[::1]:8080/", "http://[v1.x]/",
            "http://u:p@h/", "x:", "a/b:c", "http://1.2.3.4/", "mailto:a@b",
            "http://[1:2:3:4:5:6:7::]/", "http://[::ffff:1.2.3.4]/",
        ] {
            assert!(is_uri_reference(ok), "{ok}");
        }
        for bad in [
            "a b", "é", "%zz", "%2", "a:b/c%", "1a:b", ":x", "http://h:8a/", "http://[::1/",
            "http://[1:2:3:4:5:6:7:8::]/", "http://a@b@c/", "a#b#c", "http://h/^", "a\\b",
            "http://[::1.2.3.04]/", "x[y]",
        ] {
            assert!(!is_uri_reference(bad), "{bad}");
        }
    }

    #[test]
    fn rfc3986_examples() {
        let base = parse_uri_reference("http://a/b/c/d;p?q").unwrap();
        let cases = [
            ("g:h", "g:h"), ("g", "http://a/b/c/g"), ("./g", "http://a/b/c/g"),
            ("g/", "http://a/b/c/g/"), ("/g", "http://a/g"), ("//g", "http://g"),
            ("?y", "http://a/b/c/d;p?y"), ("g?y", "http://a/b/c/g?y"), ("#s", "http://a/b/c/d;p?q#s"),
            ("g#s", "http://a/b/c/g#s"), (";x", "http://a/b/c/;x"), ("", "http://a/b/c/d;p?q"),
            (".", "http://a/b/c/"), ("./", "http://a/b/c/"), ("..", "http://a/b/"),
            ("../g", "http://a/b/g"), ("../..", "http://a/"), ("../../g", "http://a/g"),
            ("../../../g", "http://a/g"), ("/./g", "http://a/g"), ("/../g", "http://a/g"),
            ("g.", "http://a/b/c/g."), (".g", "http://a/b/c/.g"), ("g..", "http://a/b/c/g.."),
            ("./../g", "http://a/b/g"), ("./g/.", "http://a/b/c/g/"), ("g/./h", "http://a/b/c/g/h"),
            ("g/../h", "http://a/b/c/h"), ("g;x=1/./y", "http://a/b/c/g;x=1/y"),
            ("http:g", "http:g"),
        ];
        for (r, want) in cases {
            let got = resolve(&base, &parse_uri_reference(r).unwrap()).to_string();
            assert_eq!(got, want, "{r}");
        }
    }

    #[test]
    fn file_mapping() {
        let f = |s: &str| file_uri_to_path(&parse_uri_reference(s).unwrap());
        assert_eq!(f("file:///a%20b").unwrap(), PathBuf::from("/a b"));
        assert_eq!(f("file://LOCALHOST/a").unwrap(), PathBuf::from("/a"));
        assert_eq!(f("FILE:/a#frag").unwrap(), PathBuf::from("/a"));
        assert!(f("file://host/a").is_err());
        assert!(f("file:///a?").is_err());
        assert!(f("file:///a%2Fb").is_err());
        assert!(f("file:///a%00b").is_err());
        assert!(f("file:///a/%2E%2E/b").is_err());
        assert!(f("file:a").is_err());
        assert_eq!(base_uri_for_path(Path::new("/data/my file.vzip")).unwrap(), "file:///data/my%20file.vzip");
        assert_eq!(base_uri_for_path(Path::new("/a//b/./c/../d%")).unwrap(), "file:///a/b/d%25");
    }
}
