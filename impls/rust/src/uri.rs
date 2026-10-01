//! RFC 3986 URI-reference validation and resolution, plus the `file:` URI
//! mapping of SPEC §6.

use std::path::{Path, PathBuf};

fn is_unreserved(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'-' | b'.' | b'_' | b'~')
}
fn is_sub_delim(c: u8) -> bool {
    matches!(c, b'!' | b'$' | b'&' | b'\'' | b'(' | b')' | b'*' | b'+' | b',' | b';' | b'=')
}

/// Check that `s` consists of the allowed single characters or pct-encoded
/// triplets.
fn check_chars(s: &[u8], extra: impl Fn(u8) -> bool) -> bool {
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
            continue;
        }
        if !(is_unreserved(c) || is_sub_delim(c) || extra(c)) {
            return false;
        }
        i += 1;
    }
    true
}

fn is_pchar_extra(c: u8) -> bool {
    c == b':' || c == b'@'
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

/// Count 16-bit pieces in a ':'-separated list of h16 (the last may be an
/// IPv4 address if `allow_v4`). Returns None if invalid.
fn h16_list(s: &str, allow_v4: bool) -> Option<usize> {
    if s.is_empty() {
        return Some(0);
    }
    let parts: Vec<&str> = s.split(':').collect();
    let mut n = 0;
    for (i, p) in parts.iter().enumerate() {
        if i == parts.len() - 1 && allow_v4 && p.contains('.') {
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
    match s.find("::") {
        None => h16_list(s, true) == Some(8),
        Some(i) => {
            let left = &s[..i];
            let right = &s[i + 2..];
            if right.contains("::") {
                return false;
            }
            let (Some(l), Some(r)) = (h16_list(left, false), h16_list(right, true)) else {
                return false;
            };
            l + r <= 7
        }
    }
}

fn valid_ipvfuture(s: &str) -> bool {
    let b = s.as_bytes();
    if b.is_empty() || !(b[0] == b'v' || b[0] == b'V') {
        return false;
    }
    let rest = &s[1..];
    let Some(dot) = rest.find('.') else { return false };
    let hex = &rest[..dot];
    let tail = &rest[dot + 1..];
    !hex.is_empty()
        && hex.bytes().all(|c| c.is_ascii_hexdigit())
        && !tail.is_empty()
        && tail.bytes().all(|c| is_unreserved(c) || is_sub_delim(c) || c == b':')
}

fn valid_authority(a: &str) -> bool {
    let hostport = match a.find('@') {
        Some(i) => {
            if !check_chars(a[..i].as_bytes(), |c| c == b':') {
                return false;
            }
            &a[i + 1..]
        }
        None => a,
    };
    let (host_ok, port) = if let Some(rest) = hostport.strip_prefix('[') {
        let Some(close) = rest.find(']') else { return false };
        let lit = &rest[..close];
        let ok = valid_ipv6(lit) || valid_ipvfuture(lit);
        let after = &rest[close + 1..];
        if after.is_empty() {
            (ok, "")
        } else if let Some(p) = after.strip_prefix(':') {
            (ok, p)
        } else {
            return false;
        }
    } else {
        match hostport.find(':') {
            Some(i) => (check_chars(hostport[..i].as_bytes(), |_| false), &hostport[i + 1..]),
            None => (check_chars(hostport.as_bytes(), |_| false), ""),
        }
    };
    host_ok && port.bytes().all(|c| c.is_ascii_digit())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Uri {
    pub scheme: Option<String>,
    pub authority: Option<String>,
    pub path: String,
    pub query: Option<String>,
    pub fragment: Option<String>,
}

/// Split per RFC 3986 Appendix B and validate against the `URI-reference`
/// ABNF of §4.1.
pub fn parse_uri_reference(s: &str) -> Result<Uri, String> {
    if !s.is_ascii() {
        return Err("URI reference contains non-ASCII characters".into());
    }
    let mut rest = s;
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
    let mut scheme = None;
    if let Some(i) = rest.find(|c| c == ':' || c == '/') {
        if rest.as_bytes()[i] == b':' && i > 0 {
            let sch = &rest[..i];
            let b = sch.as_bytes();
            if !(b[0].is_ascii_alphabetic()
                && b.iter().all(|&c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'-' | b'.')))
            {
                return Err(format!("invalid scheme {sch:?}"));
            }
            scheme = Some(sch.to_string());
            rest = &rest[i + 1..];
        }
    }
    let mut authority = None;
    if let Some(r) = rest.strip_prefix("//") {
        let end = r.find('/').unwrap_or(r.len());
        let a = &r[..end];
        if !valid_authority(a) {
            return Err(format!("invalid authority {a:?}"));
        }
        authority = Some(a.to_string());
        rest = &r[end..];
    }
    let path = rest;
    if !check_chars(path.as_bytes(), |c| is_pchar_extra(c) || c == b'/') {
        return Err(format!("invalid path {path:?}"));
    }
    if scheme.is_none() && authority.is_none() {
        // path-noscheme: first segment must not contain ':'
        let first = path.split('/').next().unwrap_or("");
        if first.contains(':') {
            return Err("relative path's first segment contains ':'".into());
        }
    }
    let qf_ok = |q: &str| check_chars(q.as_bytes(), |c| is_pchar_extra(c) || c == b'/' || c == b'?');
    if let Some(q) = &query {
        if !qf_ok(q) {
            return Err(format!("invalid query {q:?}"));
        }
    }
    if let Some(f) = &fragment {
        if !qf_ok(f) {
            return Err(format!("invalid fragment {f:?}"));
        }
    }
    Ok(Uri { scheme, authority, path: path.to_string(), query, fragment })
}

/// RFC 3986 §5.2.4
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
            pop_last(&mut out);
        } else if inp == "/.." {
            inp = "/".into();
            pop_last(&mut out);
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

fn pop_last(out: &mut String) {
    match out.rfind('/') {
        Some(i) => out.truncate(i),
        None => out.clear(),
    }
}

/// RFC 3986 §5.2.2 strict resolution.
pub fn resolve(base: &Uri, r: &Uri) -> Uri {
    let (scheme, authority, path, query);
    if r.scheme.is_some() {
        scheme = r.scheme.clone();
        authority = r.authority.clone();
        path = remove_dot_segments(&r.path);
        query = r.query.clone();
    } else {
        scheme = base.scheme.clone();
        if r.authority.is_some() {
            authority = r.authority.clone();
            path = remove_dot_segments(&r.path);
            query = r.query.clone();
        } else {
            authority = base.authority.clone();
            if r.path.is_empty() {
                path = base.path.clone();
                query = if r.query.is_some() { r.query.clone() } else { base.query.clone() };
            } else {
                if r.path.starts_with('/') {
                    path = remove_dot_segments(&r.path);
                } else {
                    let merged = if base.authority.is_some() && base.path.is_empty() {
                        format!("/{}", r.path)
                    } else {
                        match base.path.rfind('/') {
                            Some(i) => format!("{}{}", &base.path[..=i], r.path),
                            None => r.path.clone(),
                        }
                    };
                    path = remove_dot_segments(&merged);
                }
                query = r.query.clone();
            }
        }
    }
    Uri { scheme, authority, path, query, fragment: r.fragment.clone() }
}

/// Lexically normalise an absolute path (§6): drop empty and `.` segments,
/// `..` removes the previous segment.
pub fn normalize_abs_path(p: &Path) -> Result<PathBuf, String> {
    use std::os::unix::ffi::OsStrExt;
    let abs = if p.is_absolute() {
        p.to_path_buf()
    } else {
        let cwd = std::env::current_dir().map_err(|e| format!("getcwd: {e}"))?;
        cwd.join(p)
    };
    let bytes = abs.as_os_str().as_bytes();
    let mut segs: Vec<&[u8]> = Vec::new();
    for seg in bytes.split(|&c| c == b'/') {
        match seg {
            b"" | b"." => {}
            b".." => {
                segs.pop();
            }
            s => segs.push(s),
        }
    }
    let mut out = Vec::new();
    for s in segs {
        out.push(b'/');
        out.extend_from_slice(s);
    }
    if out.is_empty() {
        out.push(b'/');
    }
    Ok(PathBuf::from(std::ffi::OsStr::from_bytes(&out)))
}

/// Build the `file:` base URI of a local path (§6).
pub fn base_uri_for_path(p: &Path) -> Result<String, String> {
    use std::os::unix::ffi::OsStrExt;
    let abs = normalize_abs_path(p)?;
    let mut s = String::from("file://");
    for &c in abs.as_os_str().as_bytes() {
        if is_unreserved(c) || is_sub_delim(c) || matches!(c, b':' | b'@' | b'/') {
            s.push(c as char);
        } else {
            s.push_str(&format!("%{c:02X}"));
        }
    }
    Ok(s)
}

fn hexval(c: u8) -> u8 {
    match c {
        b'0'..=b'9' => c - b'0',
        b'a'..=b'f' => c - b'a' + 10,
        _ => c - b'A' + 10,
    }
}

/// Map a resolved `file:` URI to a local path (§6).
pub fn file_uri_to_path(u: &Uri) -> Result<PathBuf, String> {
    use std::os::unix::ffi::OsStrExt;
    match &u.authority {
        None => {}
        Some(a) if a.is_empty() || a.eq_ignore_ascii_case("localhost") => {}
        Some(a) => return Err(format!("file: URI has non-local authority {a:?}")),
    }
    if !u.path.starts_with('/') {
        return Err("file: URI path is not absolute".into());
    }
    if u.query.is_some() {
        return Err("file: URI has a query component".into());
    }
    let mut out = Vec::new();
    for (i, seg) in u.path.as_bytes().split(|&c| c == b'/').enumerate() {
        let mut dec = Vec::new();
        let mut j = 0;
        while j < seg.len() {
            if seg[j] == b'%' {
                dec.push(hexval(seg[j + 1]) * 16 + hexval(seg[j + 2]));
                j += 3;
            } else {
                dec.push(seg[j]);
                j += 1;
            }
        }
        if dec.contains(&b'/') {
            return Err("file: URI path contains an encoded '/'".into());
        }
        if dec.contains(&0) {
            return Err("file: URI path contains a NUL byte".into());
        }
        if dec == b"." || dec == b".." {
            return Err("file: URI path contains a '.' or '..' segment".into());
        }
        if i > 0 {
            out.push(b'/');
        }
        out.extend_from_slice(&dec);
    }
    Ok(PathBuf::from(std::ffi::OsStr::from_bytes(&out)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uri_reference_validity() {
        let good = [
            "a.bin", "a%20b.bin", "%C3%A9.bin", "../x/y", "/abs", "//host/p", "file:///x",
            "file:/x", "http://u:p@h:80/p?q#f", "#x", "?q", "", "http://[::1]/", "http://[v1.a]/",
            "http://[1:2:3:4:5:6:1.2.3.4]/", "x?a/b?c", ".", "a/b:c",
        ];
        for g in good {
            assert!(parse_uri_reference(g).is_ok(), "{g}");
        }
        let bad = [
            "a b", "é", "1a:b", ":a", "%zz", "%2", "http://h:x/", "http://[::1/",
            "a#b#c", "http://a@b@c/", "http://[1:2:3:4:5:6:7:8:9]/", "x<y",
        ];
        for b in bad {
            assert!(parse_uri_reference(b).is_err(), "{b}");
        }
    }

    #[test]
    fn rfc3986_examples() {
        let base = parse_uri_reference("http://a/b/c/d;p?q").unwrap();
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
            ("", "http://a/b/c/d;p?q"),
            ("..", "http://a/b/"),
            ("../..", "http://a/"),
            ("../../../g", "http://a/g"),
            ("/./g", "http://a/g"),
            ("g.", "http://a/b/c/g."),
            ("./../g", "http://a/b/g"),
            ("g;x=1/../y", "http://a/b/c/y"),
            ("http:g", "http:g"),
        ];
        for (r, want) in cases {
            let t = resolve(&base, &parse_uri_reference(r).unwrap());
            assert_eq!(to_string(&t), want, "{r}");
        }
    }

    fn to_string(u: &Uri) -> String {
        let mut s = String::new();
        if let Some(x) = &u.scheme {
            s += x;
            s += ":";
        }
        if let Some(a) = &u.authority {
            s += "//";
            s += a;
        }
        s += &u.path;
        if let Some(q) = &u.query {
            s += "?";
            s += q;
        }
        if let Some(f) = &u.fragment {
            s += "#";
            s += f;
        }
        s
    }

    #[test]
    fn base_uri() {
        assert_eq!(
            base_uri_for_path(Path::new("/data/my file.vzip")).unwrap(),
            "file:///data/my%20file.vzip"
        );
        assert_eq!(base_uri_for_path(Path::new("/a//./b/../c")).unwrap(), "file:///a/c");
    }

    #[test]
    fn file_mapping() {
        let p = |s: &str| file_uri_to_path(&parse_uri_reference(s).unwrap());
        assert_eq!(p("file:///a%20b").unwrap(), PathBuf::from("/a b"));
        assert_eq!(p("file://localhost/x").unwrap(), PathBuf::from("/x"));
        assert_eq!(p("file:/x#frag").unwrap(), PathBuf::from("/x"));
        assert!(p("file://host/x").is_err());
        assert!(p("file:///x?").is_err());
        assert!(p("file:///a%2Fb").is_err());
        assert!(p("file:///a%00").is_err());
        assert!(p("file:///a/%2E%2E/b").is_err());
    }
}
