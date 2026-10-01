//! RFC 3986 URI-reference validation, parsing and strict resolution (§5.2.2),
//! plus the vzip `file:` mapping and base-URI construction (spec §6).

use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Uri {
    pub scheme: Option<String>,
    pub authority: Option<String>,
    pub path: String,
    pub query: Option<String>,
    pub fragment: Option<String>,
}

impl Uri {
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
}

fn is_unreserved(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'-' | b'.' | b'_' | b'~')
}

fn is_sub_delim(c: u8) -> bool {
    matches!(
        c,
        b'!' | b'$' | b'&' | b'\'' | b'(' | b')' | b'*' | b'+' | b',' | b';' | b'='
    )
}

/// Checks that `s` consists of chars allowed by `allowed` or pct-encoded triplets.
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
        && b[1..]
            .iter()
            .all(|&c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'-' | b'.'))
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

fn valid_ipv6(s: &str) -> bool {
    // Split into head and tail around "::" (at most one).
    let (head, tail, compressed) = match s.find("::") {
        Some(i) => {
            if s[i + 2..].contains("::") {
                return false;
            }
            (&s[..i], &s[i + 2..], true)
        }
        None => (s, "", false),
    };
    let split = |x: &str| -> Option<Vec<String>> {
        if x.is_empty() {
            Some(vec![])
        } else {
            Some(x.split(':').map(|p| p.to_string()).collect())
        }
    };
    let h = match split(head) {
        Some(v) => v,
        None => return false,
    };
    let t = match split(tail) {
        Some(v) => v,
        None => return false,
    };
    // The last group overall may be an IPv4 address (counts as 2 groups).
    let mut groups: Vec<String> = h.clone();
    groups.extend(t.clone());
    let mut count = 0usize;
    for (idx, g) in groups.iter().enumerate() {
        let last = idx == groups.len() - 1;
        if last && g.contains('.') {
            // ls32 position as IPv4
            if !valid_ipv4(g) {
                return false;
            }
            // IPv4 must be in the tail if compressed, or the end if not
            count += 2;
        } else if valid_h16(g) {
            count += 1;
        } else {
            return false;
        }
    }
    if compressed {
        count <= 7
    } else {
        count == 8
    }
}

fn valid_ip_literal(s: &str) -> bool {
    // s excludes brackets
    if s.starts_with('v') || s.starts_with('V') {
        // IPvFuture = "v" 1*HEXDIG "." 1*( unreserved / sub-delims / ":" )
        let rest = &s[1..];
        let dot = match rest.find('.') {
            Some(d) => d,
            None => return false,
        };
        let hex = &rest[..dot];
        let tail = &rest[dot + 1..];
        return !hex.is_empty()
            && hex.bytes().all(|c| c.is_ascii_hexdigit())
            && !tail.is_empty()
            && tail
                .bytes()
                .all(|c| is_unreserved(c) || is_sub_delim(c) || c == b':');
    }
    valid_ipv6(s)
}

/// Splits an authority into (userinfo, host, port). Host includes brackets.
pub fn split_authority(a: &str) -> Option<(Option<&str>, &str, Option<&str>)> {
    let (userinfo, hostport) = match a.rfind('@') {
        Some(i) => (Some(&a[..i]), &a[i + 1..]),
        None => (None, a),
    };
    if hostport.starts_with('[') {
        let close = hostport.find(']')?;
        let host = &hostport[..=close];
        let rest = &hostport[close + 1..];
        if rest.is_empty() {
            Some((userinfo, host, None))
        } else if let Some(p) = rest.strip_prefix(':') {
            Some((userinfo, host, Some(p)))
        } else {
            None
        }
    } else {
        match hostport.find(':') {
            Some(i) => Some((userinfo, &hostport[..i], Some(&hostport[i + 1..]))),
            None => Some((userinfo, hostport, None)),
        }
    }
}

fn valid_authority(a: &str) -> bool {
    let (userinfo, host, port) = match split_authority(a) {
        Some(x) => x,
        None => return false,
    };
    if let Some(u) = userinfo {
        if !check_chars(u.as_bytes(), |c| is_unreserved(c) || is_sub_delim(c) || c == b':') {
            return false;
        }
    }
    if let Some(p) = port {
        if !p.bytes().all(|c| c.is_ascii_digit()) {
            return false;
        }
    }
    if host.starts_with('[') {
        if !host.ends_with(']') {
            return false;
        }
        valid_ip_literal(&host[1..host.len() - 1])
    } else {
        // reg-name (IPv4address is a subset syntactically)
        check_chars(host.as_bytes(), |c| is_unreserved(c) || is_sub_delim(c))
    }
}

/// Parses and validates a URI-reference (RFC 3986 §4.1). Returns None if `s`
/// does not match the grammar exactly.
pub fn parse_uri_reference(s: &str) -> Option<Uri> {
    if !s.is_ascii() {
        return None;
    }
    // Appendix B split.
    let mut rest = s;
    let mut scheme = None;
    if let Some(i) = rest.find(|c| c == ':' || c == '/' || c == '?' || c == '#') {
        if rest.as_bytes()[i] == b':' && i > 0 {
            let sc = &rest[..i];
            if !valid_scheme(sc) {
                return None;
            }
            scheme = Some(sc.to_string());
            rest = &rest[i + 1..];
        }
    }
    let mut fragment = None;
    if let Some(i) = rest.find('#') {
        fragment = Some(rest[i + 1..].to_string());
        rest = &rest[..i];
    }
    let mut query = None;
    if let Some(i) = rest.find('?') {
        query = Some(rest[i + 1..].to_string());
        rest = &rest[..i];
    }
    let mut authority = None;
    if let Some(r) = rest.strip_prefix("//") {
        let end = r.find('/').unwrap_or(r.len());
        authority = Some(r[..end].to_string());
        rest = &r[end..];
    }
    let path = rest.to_string();

    // Validate components.
    if let Some(a) = &authority {
        if !valid_authority(a) {
            return None;
        }
    }
    for seg in path.split('/') {
        if !check_chars(seg.as_bytes(), is_pchar) {
            return None;
        }
    }
    if scheme.is_none() && authority.is_none() {
        // path-noscheme: first segment must not contain ':'
        let first = path.split('/').next().unwrap_or("");
        if first.contains(':') {
            return None;
        }
    }
    let qf_ok = |x: &str| check_chars(x.as_bytes(), |c| is_pchar(c) || c == b'/' || c == b'?');
    if let Some(q) = &query {
        if !qf_ok(q) {
            return None;
        }
    }
    if let Some(f) = &fragment {
        if !qf_ok(f) {
            return None;
        }
    }
    Some(Uri { scheme, authority, path, query, fragment })
}

/// RFC 3986 §5.2.4.
pub fn remove_dot_segments(input: &str) -> String {
    let mut inp = input.to_string();
    let mut out = String::new();
    while !inp.is_empty() {
        if inp.starts_with("../") {
            inp.drain(..3);
        } else if inp.starts_with("./") {
            inp.drain(..2);
        } else if inp.starts_with("/./") {
            inp.replace_range(..3, "/");
        } else if inp == "/." {
            inp = "/".into();
        } else if inp.starts_with("/../") {
            inp.replace_range(..4, "/");
            pop_last_segment(&mut out);
        } else if inp == "/.." {
            inp = "/".into();
            pop_last_segment(&mut out);
        } else if inp == "." || inp == ".." {
            inp.clear();
        } else {
            let start = if inp.starts_with('/') { 1 } else { 0 };
            let end = inp[start..].find('/').map(|i| i + start).unwrap_or(inp.len());
            out.push_str(&inp[..end]);
            inp.drain(..end);
        }
    }
    out
}

fn pop_last_segment(out: &mut String) {
    match out.rfind('/') {
        Some(i) => out.truncate(i),
        None => out.clear(),
    }
}

fn merge(base: &Uri, rel_path: &str) -> String {
    if base.authority.is_some() && base.path.is_empty() {
        format!("/{}", rel_path)
    } else {
        match base.path.rfind('/') {
            Some(i) => format!("{}{}", &base.path[..=i], rel_path),
            None => rel_path.to_string(),
        }
    }
}

/// Strict resolution, RFC 3986 §5.2.2.
pub fn resolve(base: &Uri, r: &Uri) -> Uri {
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
    Uri { scheme, authority, path, query, fragment: r.fragment.clone() }
}

/// Percent-decodes `s` into bytes. `s` must already be syntactically valid.
pub fn pct_decode(s: &str) -> Vec<u8> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let h = std::str::from_utf8(&b[i + 1..i + 3]).unwrap();
            out.push(u8::from_str_radix(h, 16).unwrap());
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    out
}

/// Maps a resolved `file:` URI to a local POSIX path (spec §6).
pub fn file_uri_to_path(u: &Uri) -> Result<PathBuf, String> {
    match &u.authority {
        None => {}
        Some(a) if a.is_empty() || a.eq_ignore_ascii_case("localhost") => {}
        Some(a) => return Err(format!("file: URI has non-local authority {:?}", a)),
    }
    if !u.path.starts_with('/') {
        return Err("file: URI path is not absolute".into());
    }
    if u.query.is_some() {
        return Err("file: URI has a query component".into());
    }
    let mut bytes = Vec::new();
    for (i, seg) in u.path.split('/').enumerate() {
        if i > 0 {
            bytes.push(b'/');
        }
        let d = pct_decode(seg);
        if d.contains(&b'/') {
            return Err("file: URI path contains an encoded '/'".into());
        }
        if d.contains(&0) {
            return Err("file: URI path contains a NUL byte".into());
        }
        if d == b"." || d == b".." {
            return Err("file: URI path contains a dot segment after decoding".into());
        }
        bytes.extend_from_slice(&d);
    }
    use std::os::unix::ffi::OsStringExt;
    Ok(PathBuf::from(std::ffi::OsString::from_vec(bytes)))
}

/// Builds the base URI for an archive opened from a local path (spec §6).
pub fn base_uri_for_path(p: &Path) -> std::io::Result<Uri> {
    use std::os::unix::ffi::OsStrExt;
    let raw = p.as_os_str().as_bytes();
    let mut full: Vec<u8> = Vec::new();
    if !raw.starts_with(b"/") {
        let cwd = std::env::current_dir()?; // getcwd(3)
        full.extend_from_slice(cwd.as_os_str().as_bytes());
        full.push(b'/');
    }
    full.extend_from_slice(raw);
    let mut segs: Vec<&[u8]> = Vec::new();
    for seg in full.split(|&c| c == b'/') {
        match seg {
            b"" | b"." => {}
            b".." => {
                segs.pop();
            }
            s => segs.push(s),
        }
    }
    let mut path = String::new();
    if segs.is_empty() {
        path.push('/');
    }
    for s in segs {
        path.push('/');
        for &c in s {
            if is_unreserved(c) || is_sub_delim(c) || c == b':' || c == b'@' {
                path.push(c as char);
            } else {
                path.push_str(&format!("%{:02X}", c));
            }
        }
    }
    Ok(Uri {
        scheme: Some("file".into()),
        authority: Some(String::new()),
        path,
        query: None,
        fragment: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r(base: &str, rel: &str) -> String {
        let b = parse_uri_reference(base).unwrap();
        let x = parse_uri_reference(rel).unwrap();
        resolve(&b, &x).to_string()
    }

    #[test]
    fn rfc3986_examples() {
        let b = "http://a/b/c/d;p?q";
        let cases = [
            ("g:h", "g:h"),
            ("g", "http://a/b/c/g"),
            ("./g", "http://a/b/c/g"),
            ("g/", "http://a/b/c/g/"),
            ("/g", "http://a/g"),
            ("//g", "http://g"),
            ("?y", "http://a/b/c/d;p?y"),
            ("g?y", "http://a/b/c/g?y"),
            ("#s", "http://a/b/c/d;p?q#s"),
            ("g#s", "http://a/b/c/g#s"),
            (";x", "http://a/b/c/;x"),
            ("", "http://a/b/c/d;p?q"),
            (".", "http://a/b/c/"),
            ("./", "http://a/b/c/"),
            ("..", "http://a/b/"),
            ("../g", "http://a/b/g"),
            ("../..", "http://a/"),
            ("../../g", "http://a/g"),
            ("../../../g", "http://a/g"),
            ("/./g", "http://a/g"),
            ("/../g", "http://a/g"),
            ("g.", "http://a/b/c/g."),
            ("..g", "http://a/b/c/..g"),
            ("./../g", "http://a/b/g"),
            ("g/./h", "http://a/b/c/g/h"),
            ("g/../h", "http://a/b/c/h"),
            ("g;x=1/./y", "http://a/b/c/g;x=1/y"),
            ("g?y/./x", "http://a/b/c/g?y/./x"),
            ("http:g", "http:g"),
        ];
        for (rel, exp) in cases {
            assert_eq!(r(b, rel), exp, "rel {}", rel);
        }
    }

    #[test]
    fn validation() {
        for ok in ["a%20b.bin", "%C3%A9.bin", "http://[::1]:8080/x", "file:///x", "#x", "", "a/b?c#d",
                   "http://[v1.x]/", "http://[1:2:3:4:5:6:7:8]/", "http://[::ffff:1.2.3.4]/", "x:"] {
            assert!(parse_uri_reference(ok).is_some(), "{}", ok);
        }
        for bad in ["a b", "é", "%zz", "%2", "1a:b", ":x", "a:b c", "http://[::1/", "http://a b/",
                    "http://[1:2:3:4:5:6:7:8:9]/", "http://h:80x/", "a#b#c", "http://[]/", "x{y}"] {
            assert!(parse_uri_reference(bad).is_none(), "{}", bad);
        }
    }

    #[test]
    fn file_mapping() {
        let p = |s: &str| file_uri_to_path(&parse_uri_reference(s).unwrap());
        assert_eq!(p("file:///a%20b").unwrap(), PathBuf::from("/a b"));
        assert_eq!(p("file:/x").unwrap(), PathBuf::from("/x"));
        assert_eq!(p("file://LOCALHOST/x#f").unwrap(), PathBuf::from("/x"));
        assert!(p("file://host/x").is_err());
        assert!(p("file:///x?").is_err());
        assert!(p("file:///a%2Fb").is_err());
        assert!(p("file:///a%00b").is_err());
        assert!(p("file:///a/%2E%2E/b").is_err());
        assert!(p("file:x").is_err());
    }

    #[test]
    fn base_uri() {
        let u = base_uri_for_path(Path::new("/data//./x/../my file.vzip")).unwrap();
        assert_eq!(u.to_string(), "file:///data/my%20file.vzip");
    }
}
