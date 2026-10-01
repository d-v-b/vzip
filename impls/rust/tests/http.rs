//! HTTP source resolution (spec §6.1, §6.2) against an in-process server.

mod common;

use common::*;
use std::sync::Arc;
use vzip::proto::{self, Range, Source, SourceKind};
use vzip::{Archive, ErrorClass, Request};

const OBJ: &[u8] = b"0123456789abcdefghij";
const LM: &str = "Sun, 06 Nov 1994 08:49:37 GMT"; // 784111777

fn handler(req: &HttpRequest) -> Vec<u8> {
    let path = req.path.split('?').next().unwrap();
    let etag = ("ETag", "\"v1\"".to_string());
    let lm = ("Last-Modified", LM.to_string());
    match path {
        "/dir/obj" => range_response(req, OBJ, &[etag, lm]),
        "/ignore-range" => response(200, &[etag, lm], OBJ),
        "/star" => {
            let r = req.header("Range").unwrap().strip_prefix("bytes=").unwrap().to_string();
            let (a, b) = r.split_once('-').unwrap();
            let (a, b): (usize, usize) = (a.parse().unwrap(), b.parse().unwrap());
            response(206, &[("Content-Range", format!("bytes {a}-{b}/*"))], &OBJ[a..=b])
        }
        "/honour-if-match" => {
            if req.header("If-Match").map(|v| v != "\"v1\"").unwrap_or(false) {
                return response(412, &[], b"");
            }
            range_response(req, OBJ, &[etag])
        }
        "/weak" => range_response(req, OBJ, &[("ETag", "W/\"v1\"".into())]),
        "/no-headers" => range_response(req, OBJ, &[]),
        "/bad-lm" => range_response(req, OBJ, &[("Last-Modified", "yesterday".into())]),
        "/old-lm" => range_response(req, OBJ, &[("Last-Modified", "Sunday, 06-Nov-94 08:49:37 GMT".into())]),
        "/gzip" => range_response(req, OBJ, &[("Content-Encoding", "gzip".into())]),
        "/identity" => range_response(req, OBJ, &[("Content-Encoding", "identity".into())]),
        "/wrong-range" => response(206, &[("Content-Range", "bytes 0-1/20".into())], b"01"),
        "/416" => response(416, &[], b""),
        "/404" => response(404, &[], b"nope"),
        "/412" => response(412, &[], b""),
        "/chunked" => {
            let r = req.header("Range").unwrap().strip_prefix("bytes=").unwrap().to_string();
            let (a, b) = r.split_once('-').unwrap();
            let (a, b): (usize, usize) = (a.parse().unwrap(), b.parse().unwrap());
            let body = &OBJ[a..=b];
            let mut out = format!(
                "HTTP/1.1 206 Partial\r\nTransfer-Encoding: chunked\r\nContent-Range: bytes {a}-{b}/20\r\n\r\n"
            )
            .into_bytes();
            for c in body.chunks(2) {
                out.extend_from_slice(format!("{:x}\r\n", c.len()).as_bytes());
                out.extend_from_slice(c);
                out.extend_from_slice(b"\r\n");
            }
            out.extend_from_slice(b"0\r\n\r\n");
            out
        }
        "/to-file" => response(302, &[("Location", "file:///etc/passwd".into())], b""),
        p if p.starts_with("/redir/") => {
            let n: u32 = p["/redir/".len()..].parse().unwrap();
            let loc = if n == 0 { "../dir/obj".to_string() } else { format!("{}", n - 1) };
            let status = [301, 302, 303, 307, 308][n as usize % 5];
            response(status, &[("Location", loc)], b"")
        }
        _ => response(404, &[], b""),
    }
}

fn server() -> Server {
    serve(Arc::new(handler))
}

struct Fx {
    a: Archive,
    srv: Server,
}

/// Archive with reference "r" = source 0 [offset, offset+length) where
/// source 0 is `url` (with pins), opened with an http base URI.
fn fx_with(srv: Server, mut s: Source, offset: u64, length: u64) -> Fx {
    let dir = tmpdir();
    if let Some(SourceKind::Url(u)) = &mut s.kind {
        *u = u.replace("PORT", &srv.port.to_string());
    }
    let payload = proto::encode_range(&Range { source: 0, offset, length, data: None });
    let b = build_raw(&RawSpec::new(vec![RawEntry::reference("r", 0x7A76, &payload)], &[s]));
    std::fs::write(dir.join("a.vzip"), &b).unwrap();
    let base = format!("http://127.0.0.1:{}/dir/a.vzip", srv.port);
    let a = Archive::open_with_base(&dir.join("a.vzip"), &base).unwrap();
    Fx { a, srv }
}

fn url(u: &str) -> Source {
    Source { kind: Some(SourceKind::Url(u.into())), size: None, etag: None, modified_not_after: None }
}

fn ok(s: Source, off: u64, len: u64) -> (Vec<u8>, Vec<HttpRequest>) {
    let fx = fx_with(server(), s, off, len);
    let v = fx.a.get("r", Request::Whole).expect("get should succeed").unwrap();
    let log = fx.srv.log.lock().unwrap().clone();
    (v, log)
}

fn fails(s: Source) -> Vec<HttpRequest> {
    let fx = fx_with(server(), s, 2, 3);
    let e = fx.a.get("r", Request::Whole).expect_err("get should fail");
    assert_eq!(e.class, ErrorClass::Resolution, "{e}");
    let log = fx.srv.log.lock().unwrap().clone();
    log
}

#[test]
fn http_successes() {
    // Relative URL against an http base; request headers.
    let (v, log) = ok(url("obj"), 2, 3);
    assert_eq!(v, b"234");
    assert_eq!(log.len(), 1);
    assert_eq!(log[0].method, "GET");
    assert_eq!(log[0].path, "/dir/obj");
    assert_eq!(log[0].header("Range"), Some("bytes=2-4"));
    assert_eq!(log[0].header("Accept-Encoding"), Some("identity"));
    assert_eq!(log[0].header("If-Match"), None);
    // Absolute URL, all pins satisfied.
    let mut s = url("http://127.0.0.1:PORT/dir/obj");
    s.size = Some(20);
    s.etag = Some("\"v1\"".into());
    s.modified_not_after = Some(784111777);
    let (v, log) = ok(s, 10, 10);
    assert_eq!(v, b"abcdefghij");
    assert_eq!(log[0].header("If-Match"), Some("\"v1\""));
    assert_eq!(log[0].header("If-Unmodified-Since"), Some(LM));
    // Server ignores Range: 200 with whole body.
    let mut s = url("/ignore-range");
    s.size = Some(20);
    assert_eq!(ok(s, 18, 2).0, b"ij");
    // Unknown total, no size pin.
    assert_eq!(ok(url("/star"), 1, 2).0, b"12");
    // Chunked transfer coding.
    assert_eq!(ok(url("/chunked"), 3, 7).0, b"3456789");
    // Content-Encoding: identity is fine.
    assert_eq!(ok(url("/identity"), 0, 1).0, b"0");
    // RFC 850 Last-Modified.
    let mut s = url("/old-lm");
    s.modified_not_after = Some(784111777);
    assert_eq!(ok(s, 0, 1).0, b"0");
    // Five redirects, mixed statuses, relative Location; headers re-sent.
    let mut s = url("/redir/4");
    s.etag = Some("\"v1\"".into());
    let (v, log) = ok(s, 0, 2);
    assert_eq!(v, b"01");
    assert_eq!(log.len(), 6);
    assert!(log.iter().all(|r| r.header("Range") == Some("bytes=0-1") && r.header("If-Match") == Some("\"v1\"")));
    assert_eq!(log[5].path, "/dir/obj");
    // A zero-length request needs no HTTP at all.
    let fx = fx_with(server(), url("/404"), 0, 5);
    assert_eq!(fx.a.get("r", Request::Range(1, 1)).unwrap().unwrap(), b"");
    assert!(fx.srv.log.lock().unwrap().is_empty());
}

#[test]
fn http_six_redirects() {
    let log = fails(url("/redir/5"));
    assert_eq!(log.len(), 6);
}

#[test]
fn http_redirect_to_file() {
    fails(url("/to-file"));
}

#[test]
fn http_size_pin_mismatch_206() {
    let mut s = url("/dir/obj");
    s.size = Some(21);
    fails(s);
}

#[test]
fn http_size_pin_mismatch_200() {
    let mut s = url("/ignore-range");
    s.size = Some(19);
    fails(s);
}

#[test]
fn http_size_pin_unknown_total() {
    let mut s = url("/star");
    s.size = Some(20);
    fails(s);
}

#[test]
fn http_etag_412() {
    let mut s = url("/honour-if-match");
    s.etag = Some("\"v2\"".into());
    let log = fails(s);
    assert_eq!(log[0].header("If-Match"), Some("\"v2\""));
}

#[test]
fn http_etag_mismatch_ignored_by_server() {
    let mut s = url("/dir/obj");
    s.etag = Some("\"v2\"".into());
    fails(s);
}

#[test]
fn http_etag_weak_response() {
    let mut s = url("/weak");
    s.etag = Some("\"v1\"".into());
    fails(s);
}

#[test]
fn http_etag_missing_header() {
    let mut s = url("/no-headers");
    s.etag = Some("\"v1\"".into());
    fails(s);
}

#[test]
fn http_modified_after_pin() {
    let mut s = url("/dir/obj");
    s.modified_not_after = Some(784111776);
    fails(s);
}

#[test]
fn http_last_modified_missing() {
    let mut s = url("/no-headers");
    s.modified_not_after = Some(i64::MAX / 2);
    fails(s);
}

#[test]
fn http_last_modified_unparseable() {
    let mut s = url("/bad-lm");
    s.modified_not_after = Some(784111777);
    fails(s);
}

#[test]
fn http_modified_pin_unsendable() {
    let mut s = url("/dir/obj");
    s.modified_not_after = Some(253402300800); // year 10000
    let log = fails(s);
    assert!(log.is_empty());
}

#[test]
fn http_content_encoding_gzip() {
    fails(url("/gzip"));
}

#[test]
fn http_wrong_range_returned() {
    fails(url("/wrong-range"));
}

#[test]
fn http_status_416() {
    fails(url("/416"));
}

#[test]
fn http_status_412() {
    fails(url("/412"));
}

#[test]
fn http_status_404() {
    fails(url("/404"));
}

#[test]
fn http_object_too_short() {
    let fx = fx_with(server(), url("/dir/obj"), 15, 10);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap_err().class, ErrorClass::Resolution);
    // ...but a window inside the object works.
    assert_eq!(fx.a.get("r", Request::Range(0, 5)).unwrap().unwrap(), b"fghij");
}

#[test]
fn http_object_too_short_200() {
    let fx = fx_with(server(), url("/ignore-range"), 15, 10);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap_err().class, ErrorClass::Resolution);
}

#[test]
fn http_connection_refused() {
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    drop(l);
    let fx = fx_with(server(), url(&format!("http://127.0.0.1:{port}/x")), 0, 1);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap_err().class, ErrorClass::Resolution);
}
