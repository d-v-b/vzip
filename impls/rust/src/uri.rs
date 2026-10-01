//! RFC 3986 URI-reference validation, parsing and strict resolution (§5.2.2).

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Uri {
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

/// Checks that `s` consists of characters allowed by `ok` or pct-encoded triplets.
fn chars_ok(s: &str, ok: impl Fn(u8) -> bool) -> bool {
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            if i + 2 >= b.len() || !b[i + 1].is_ascii_hexdigit() || !b[i + 2].is_ascii_hexdigit() {
                return false;
            }
            i += 3;
        } else if ok(b[i]) {
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

fn valid_ipv4(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    parts.len() == 4
        && parts.iter().all(|p| {
            !p.is_empty()
                && p.len() <= 3
                && p.bytes().all(|c| c.is_ascii_digit())
                && (p.len() == 1 || !p.starts_with('0'))
                && p.parse::<u16>().map(|v| v <= 255).unwrap_or(false)
        })
}

fn valid_ipv6(s: &str) -> bool {
    // Split off an embedded IPv4 tail if present.
    let (head, has_v4) = match s.rfind(':') {
        Some(i) if s[i + 1..].contains('.') => {
            if !valid_ipv4(&s[i + 1..]) {
                return false;
            }
            // keep the trailing ':' so "::1.2.3.4" works
            (&s[..i + 1], true)
        }
        _ => (s, false),
    };
    let groups_needed = if has_v4 { 6 } else { 8 };
    let h16 = |g: &str| !g.is_empty() && g.len() <= 4 && g.bytes().all(|c| c.is_ascii_hexdigit());
    let count = |part: &str| -> Option<usize> {
        if part.is_empty() {
            return Some(0);
        }
        let gs: Vec<&str> = part.split(':').collect();
        if gs.iter().all(|g| h16(g)) { Some(gs.len()) } else { None }
    };
    if let Some(p) = head.find("::") {
        let left = &head[..p];
        let mut right = &head[p + 2..];
        if has_v4 {
            // right ends with ':' (before the IPv4 part) unless empty
            if right.is_empty() {
            } else if let Some(r) = right.strip_suffix(':') {
                right = r;
            } else {
                return false;
            }
        }
        if right.contains("::") {
            return false;
        }
        let (Some(l), Some(r)) = (count(left), count(right)) else { return false };
        l + r < groups_needed
    } else {
        let body = if has_v4 {
            match head.strip_suffix(':') {
                Some(b) => b,
                None => return false,
            }
        } else {
            head
        };
        count(body) == Some(groups_needed) && !body.is_empty()
    }
}

fn valid_authority(a: &str) -> bool {
    let (userinfo, hostport) = match a.rfind('@') {
        Some(i) => (Some(&a[..i]), &a[i + 1..]),
        None => (None, a),
    };
    if let Some(u) = userinfo {
        if !chars_ok(u, |c| is_unreserved(c) || is_sub_delim(c) || c == b':') {
            return false;
        }
    }
    let (host, port) = split_host_port(hostport);
    if let Some(p) = port {
        if !p.bytes().all(|c| c.is_ascii_digit()) {
            return false;
        }
    }
    if host.starts_with('[') {
        let Some(inner) = host.strip_prefix('[').and_then(|h| h.strip_suffix(']')) else { return false };
        if inner.starts_with(['v', 'V']) {
            // IPvFuture = "v" 1*HEXDIG "." 1*( unreserved / sub-delims / ":" )
            let rest = &inner[1..];
            let Some(dot) = rest.find('.') else { return false };
            let (hx, tail) = (&rest[..dot], &rest[dot + 1..]);
            !hx.is_empty()
                && hx.bytes().all(|c| c.is_ascii_hexdigit())
                && !tail.is_empty()
                && tail.bytes().all(|c| is_unreserved(c) || is_sub_delim(c) || c == b':')
        } else {
            valid_ipv6(inner)
        }
    } else {
        chars_ok(host, |c| is_unreserved(c) || is_sub_delim(c))
    }
}

/// Splits `host[:port]`, handling IP literals.
pub fn split_host_port(hp: &str) -> (&str, Option<&str>) {
    if hp.starts_with('[') {
        if let Some(e) = hp.find(']') {
            let host = &hp[..e + 1];
            let rest = &hp[e + 1..];
            if rest.is_empty() {
                return (host, None);
            }
            if let Some(p) = rest.strip_prefix(':') {
                return (host, Some(p));
            }
            // invalid; return whole as host so validation fails
            return (hp, None);
        }
        return (hp, None);
    }
    match hp.find(':') {
        Some(i) => (&hp[..i], Some(&hp[i + 1..])),
        None => (hp, None),
    }
}

/// Splits a string into components (RFC 3986 Appendix B) without validating.
fn split(s: &str) -> Uri {
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
    if let Some(i) = rest.find(':') {
        let cand = &rest[..i];
        if !cand.contains('/') && valid_scheme(cand) {
            scheme = Some(cand.to_string());
            rest = &rest[i + 1..];
        }
    }
    let mut authority = None;
    if let Some(r) = rest.strip_prefix("//") {
        let end = r.find('/').unwrap_or(r.len());
        authority = Some(r[..end].to_string());
        rest = &r[end..];
    }
    Uri { scheme, authority, path: rest.to_string(), query, fragment }
}

/// Parses and validates an RFC 3986 `URI-reference`.
pub fn parse_reference(s: &str) -> Result<Uri, String> {
    if !s.is_ascii() {
        return Err(format!("not a URI reference (non-ASCII): {s:?}"));
    }
    let u = split(s);
    let bad = || Err(format!("not a valid URI reference: {s:?}"));
    if let Some(a) = &u.authority {
        if !valid_authority(a) {
            return bad();
        }
        // path-abempty: empty or starts with '/'
    } else if u.scheme.is_none() {
        // relative-ref without authority: path-absolute / path-noscheme / path-empty.
        let first = u.path.split('/').next().unwrap_or("");
        if first.contains(':') {
            return bad();
        }
    }
    if !chars_ok(&u.path, |c| is_pchar(c) || c == b'/') {
        return bad();
    }
    for q in [&u.query, &u.fragment].into_iter().flatten() {
        if !chars_ok(q, |c| is_pchar(c) || c == b'/' || c == b'?') {
            return bad();
        }
    }
    Ok(u)
}

pub fn remove_dot_segments(input: &str) -> String {
    let mut inp = input.to_string();
    let mut out = String::new();
    while !inp.is_empty() {
        if let Some(r) = inp.strip_prefix("../") {
            inp = r.to_string();
        } else if let Some(r) = inp.strip_prefix("./") {
            inp = r.to_string();
        } else if inp.starts_with("/./") {
            inp = inp[2..].to_string();
        } else if inp == "/." {
            inp = "/".to_string();
        } else if inp.starts_with("/../") || inp == "/.." {
            inp = if inp == "/.." { "/".to_string() } else { inp[3..].to_string() };
            match out.rfind('/') {
                Some(i) => out.truncate(i),
                None => out.clear(),
            }
        } else if inp == "." || inp == ".." {
            inp.clear();
        } else {
            let start = if inp.starts_with('/') { 1 } else { 0 };
            let end = inp[start..].find('/').map(|i| i + start).unwrap_or(inp.len());
            out.push_str(&inp[..end]);
            inp = inp[end..].to_string();
        }
    }
    out
}

fn merge(base: &Uri, rel_path: &str) -> String {
    if base.authority.is_some() && base.path.is_empty() {
        format!("/{rel_path}")
    } else {
        match base.path.rfind('/') {
            Some(i) => format!("{}{}", &base.path[..=i], rel_path),
            None => rel_path.to_string(),
        }
    }
}

/// Strict resolution (RFC 3986 §5.2.2). `base` must have a scheme.
pub fn resolve(base: &Uri, r: &Uri) -> Uri {
    if r.scheme.is_some() {
        return Uri {
            scheme: r.scheme.clone(),
            authority: r.authority.clone(),
            path: remove_dot_segments(&r.path),
            query: r.query.clone(),
            fragment: r.fragment.clone(),
        };
    }
    let (authority, path, query);
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
    Uri { scheme: base.scheme.clone(), authority, path, query, fragment: r.fragment.clone() }
}

impl std::fmt::Display for Uri {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if let Some(s) = &self.scheme {
            write!(f, "{s}:")?;
        }
        if let Some(a) = &self.authority {
            write!(f, "//{a}")?;
        }
        write!(f, "{}", self.path)?;
        if let Some(q) = &self.query {
            write!(f, "?{q}")?;
        }
        if let Some(x) = &self.fragment {
            write!(f, "#{x}")?;
        }
        Ok(())
    }
}

/// Normalises an absolute or relative local path lexically (§6 base URI).
pub fn normalize_path(p: &std::path::Path) -> std::io::Result<Vec<u8>> {
    use std::os::unix::ffi::OsStrExt;
    let raw = p.as_os_str().as_bytes();
    let full: Vec<u8> = if raw.starts_with(b"/") {
        raw.to_vec()
    } else {
        let cwd = std::env::current_dir()?; // getcwd(3)
        let mut v = cwd.as_os_str().as_bytes().to_vec();
        v.push(b'/');
        v.extend_from_slice(raw);
        v
    };
    let mut segs: Vec<&[u8]> = Vec::new();
    for s in full.split(|&c| c == b'/') {
        match s {
            b"" | b"." => {}
            b".." => {
                segs.pop();
            }
            s => segs.push(s),
        }
    }
    let mut out = Vec::new();
    for s in &segs {
        out.push(b'/');
        out.extend_from_slice(s);
    }
    if out.is_empty() {
        out.push(b'/');
    }
    Ok(out)
}

/// Base URI for an archive opened from a local path.
pub fn file_base_uri(p: &std::path::Path) -> std::io::Result<Uri> {
    let path = normalize_path(p)?;
    let mut s = String::new();
    for &c in &path {
        if is_unreserved(c) || is_sub_delim(c) || matches!(c, b':' | b'@' | b'/') {
            s.push(c as char);
        } else {
            s.push_str(&format!("%{c:02X}"));
        }
    }
    Ok(Uri { scheme: Some("file".into()), authority: Some(String::new()), path: s, query: None, fragment: None })
}

/// Maps a resolved `file:` URI to a local path (§6).
pub fn file_uri_to_path(u: &Uri) -> Result<std::path::PathBuf, String> {
    use std::os::unix::ffi::OsStringExt;
    if let Some(a) = &u.authority {
        if !(a.is_empty() || a.eq_ignore_ascii_case("localhost")) {
            return Err(format!("file: URI authority must be empty or localhost, got {a:?}"));
        }
    }
    if !u.path.starts_with('/') {
        return Err("file: URI path must be absolute".into());
    }
    if u.query.is_some() {
        return Err("file: URI must not have a query".into());
    }
    let b = u.path.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let h = std::str::from_utf8(&b[i + 1..i + 3]).map_err(|_| "bad pct-encoding")?;
            let v = u8::from_str_radix(h, 16).map_err(|_| "bad pct-encoding")?;
            if v == b'/' {
                return Err("file: URI path contains encoded '/'".into());
            }
            out.push(v);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    if out.contains(&0) {
        return Err("file: URI path contains NUL".into());
    }
    for seg in out.split(|&c| c == b'/') {
        if seg == b"." || seg == b".." {
            return Err("file: URI path contains a dot segment after decoding".into());
        }
    }
    Ok(std::path::PathBuf::from(std::ffi::OsString::from_vec(out)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validation() {
        for ok in [
            "", "a", "a%20b.bin", "%C3%A9.bin", "#x", "?q", "//h/p", "http://u@h:80/p?q#f",
            "http://[::1]:8080/", "http://[v1.x]/", "file:///a", "file:/x", "../a/./b", "a:b",
            "http://1.2.3.4/", "x//y", "http://[1:2:3:4:5:6:7:8]/", "http://[::ffff:1.2.3.4]/",
            "http://h:/", "mailto:a@b",
        ] {
            assert!(parse_reference(ok).is_ok(), "{ok}");
        }
        for bad in [
            "a b", "é", "%zz", "%2", "1a:b", ":x", "http://[::1/", "http://[1:2:3:4:5:6:7:8:9]/",
            "http://h:8x/", "a<b", "http://a@b@c/", "x#a#b", "http://[::1::2]/", "[x]",
        ] {
            assert!(parse_reference(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn rfc_examples() {
        let base = parse_reference("http://a/b/c/d;p?q").unwrap();
        let cases = [
            ("g:h", "g:h"), ("g", "http://a/b/c/g"), ("./g", "http://a/b/c/g"),
            ("g/", "http://a/b/c/g/"), ("/g", "http://a/g"), ("//g", "http://g"),
            ("?y", "http://a/b/c/d;p?y"), ("g?y", "http://a/b/c/g?y"), ("#s", "http://a/b/c/d;p?q#s"),
            ("", "http://a/b/c/d;p?q"), (".", "http://a/b/c/"), ("..", "http://a/b/"),
            ("../..", "http://a/"), ("../../../g", "http://a/g"), ("/./g", "http://a/g"),
            ("g.", "http://a/b/c/g."), ("..g", "http://a/b/c/..g"), ("./../g", "http://a/b/g"),
            ("g;x=1/../y", "http://a/b/c/y"), ("http:g", "http:g"), ("g?y/./x", "http://a/b/c/g?y/./x"),
        ];
        for (r, want) in cases {
            assert_eq!(resolve(&base, &parse_reference(r).unwrap()).to_string(), want, "{r}");
        }
    }

    #[test]
    fn file_mapping() {
        let base = file_base_uri(std::path::Path::new("/data/my file.vzip")).unwrap();
        assert_eq!(base.to_string(), "file:///data/my%20file.vzip");
        let t = resolve(&base, &parse_reference("x/%C3%A9.bin").unwrap());
        assert_eq!(file_uri_to_path(&t).unwrap(), std::path::PathBuf::from("/data/x/é.bin"));
        let t = resolve(&base, &parse_reference("%2E%2E/x").unwrap());
        assert!(file_uri_to_path(&t).is_err());
        let t = resolve(&base, &parse_reference("a%2Fb").unwrap());
        assert!(file_uri_to_path(&t).is_err());
        let t = resolve(&base, &parse_reference("file://LocalHost/x").unwrap());
        assert!(file_uri_to_path(&t).is_ok());
        let t = resolve(&base, &parse_reference("file://h/x").unwrap());
        assert!(file_uri_to_path(&t).is_err());
        let t = resolve(&base, &parse_reference("x?").unwrap());
        assert!(file_uri_to_path(&t).is_err());
        let t = resolve(&base, &parse_reference("file:x").unwrap());
        assert!(file_uri_to_path(&t).is_err());
        assert_eq!(normalize_path(std::path::Path::new("/a//b/./c/../d")).unwrap(), b"/a/b/d");
    }
}
