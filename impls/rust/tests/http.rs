mod common;
use common::*;
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use vzip::Request;

const OBJ: &[u8] = b"0123456789abcdef";
const MTIME: i64 = 1_000_000_000;
const ETAG: &str = "\"v1\"";

type Log = Arc<Mutex<Vec<(String, String, Vec<(String, String)>)>>>;

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn parse_imf(s: &str) -> i64 {
    // "Sun, 06 Nov 1994 08:49:37 GMT"
    let p: Vec<&str> = s.split_whitespace().collect();
    let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let m = months.iter().position(|x| *x == p[2]).unwrap() as i64 + 1;
    let t: Vec<i64> = p[4].split(':').map(|x| x.parse().unwrap()).collect();
    days_from_civil(p[3].parse().unwrap(), m, p[1].parse().unwrap()) * 86400 + t[0] * 3600 + t[1] * 60 + t[2]
}

fn serve() -> (u16, Log) {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    let log: Log = Arc::new(Mutex::new(Vec::new()));
    let log2 = log.clone();
    std::thread::spawn(move || {
        for s in l.incoming() {
            let mut s = match s {
                Ok(s) => s,
                Err(_) => continue,
            };
            let mut rd = BufReader::new(s.try_clone().unwrap());
            let mut line = String::new();
            rd.read_line(&mut line).unwrap();
            let mut parts = line.split_whitespace();
            let method = parts.next().unwrap_or("").to_string();
            let path = parts.next().unwrap_or("").to_string();
            let mut headers = Vec::new();
            loop {
                let mut h = String::new();
                rd.read_line(&mut h).unwrap();
                let h = h.trim_end();
                if h.is_empty() {
                    break;
                }
                let (k, v) = h.split_once(':').unwrap();
                headers.push((k.trim().to_ascii_lowercase(), v.trim().to_string()));
            }
            log2.lock().unwrap().push((method, path.clone(), headers.clone()));
            let get = |n: &str| headers.iter().find(|(k, _)| k == n).map(|(_, v)| v.clone());
            let (a, b) = match get("range") {
                Some(r) => {
                    let r = r.strip_prefix("bytes=").unwrap();
                    let (a, b) = r.split_once('-').unwrap();
                    (a.parse::<u64>().unwrap(), b.parse::<u64>().unwrap())
                }
                None => (0, OBJ.len() as u64 - 1),
            };
            let resp: Vec<u8> = {
                let n = OBJ.len() as u64;
                let precond_fail = get("if-match").map_or(false, |e| e != ETAG)
                    || get("if-unmodified-since").map_or(false, |d| parse_imf(&d) < MTIME);
                let partial = |extra: &str, total: &str, a: u64, b: u64| -> Vec<u8> {
                    let body = &OBJ[a as usize..=(b.min(n - 1)) as usize];
                    let mut v = format!(
                        "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {}-{}/{}\r\nContent-Length: {}\r\n{}\r\n",
                        a,
                        b.min(n - 1),
                        total,
                        body.len(),
                        extra
                    )
                    .into_bytes();
                    v.extend_from_slice(body);
                    v
                };
                if precond_fail {
                    b"HTTP/1.1 412 Precondition Failed\r\nContent-Length: 0\r\n\r\n".to_vec()
                } else if path == "/redir" {
                    b"HTTP/1.1 302 Found\r\nLocation: /obj\r\nContent-Length: 0\r\n\r\n".to_vec()
                } else if path == "/loop" {
                    b"HTTP/1.1 302 Found\r\nLocation: loop\r\nContent-Length: 0\r\n\r\n".to_vec()
                } else if a >= n {
                    format!("HTTP/1.1 416 Range Not Satisfiable\r\nContent-Range: bytes */{}\r\nContent-Length: 0\r\n\r\n", n).into_bytes()
                } else if path == "/full" {
                    let mut v = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", n).into_bytes();
                    v.extend_from_slice(OBJ);
                    v
                } else if path == "/full-close" {
                    let mut v = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n".to_vec();
                    v.extend_from_slice(OBJ);
                    v
                } else if path == "/star" {
                    partial("", "*", a, b)
                } else if path == "/gzip" {
                    partial("Content-Encoding: gzip\r\n", "16", a, b)
                } else if path == "/shifted" {
                    partial("", "16", a + 1, b + 1)
                } else if path == "/chunked" {
                    let body = &OBJ[a as usize..=(b.min(n - 1)) as usize];
                    let mut v = format!(
                        "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {}-{}/16\r\nTransfer-Encoding: chunked\r\n\r\n",
                        a,
                        b.min(n - 1)
                    )
                    .into_bytes();
                    for c in body.chunks(3) {
                        v.extend_from_slice(format!("{:x}\r\n", c.len()).as_bytes());
                        v.extend_from_slice(c);
                        v.extend_from_slice(b"\r\n");
                    }
                    v.extend_from_slice(b"0\r\n\r\n");
                    v
                } else if path == "/obj" || path == "/obj?q=1" {
                    partial("ETag: \"v1\"\r\n", "16", a, b)
                } else {
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n".to_vec()
                }
            };
            let _ = s.write_all(&resp);
        }
    });
    (port, log)
}

fn archive_for(url: &str, pins: (Option<u64>, Option<&str>, Option<i64>), ranges: Vec<vzip::proto::Range>) -> vzip::Archive {
    let mut s = url_src(url);
    s.size = pins.0;
    s.etag = pins.1.map(|x| x.to_string());
    s.modified_not_after = pins.2;
    open_bytes(&build(&[e_concat("k", ranges)], &table(vec![s]))).unwrap()
}

fn get(port: u16, path: &str, pins: (Option<u64>, Option<&str>, Option<i64>), a: u64, b: u64) -> Result<Vec<u8>, vzip::Error> {
    let ar = archive_for(&format!("http://127.0.0.1:{}{}", port, path), pins, vec![src_range(0, a, b - a)]);
    ar.get("k", Request::Whole).map(|v| v.unwrap())
}

#[test]
fn http_success_cases() {
    let (port, log) = serve();
    let none = (None, None, None);
    let all = (Some(16), Some(ETAG), Some(MTIME));
    for (path, pins) in [
        ("/obj", none),
        ("/obj", all),
        ("/obj?q=1", all),
        ("/full", all),
        ("/full-close", (Some(16), None, None)),
        ("/star", none),
        ("/redir", all),
        ("/chunked", (Some(16), None, None)),
    ] {
        assert_eq!(get(port, path, pins, 2, 6).unwrap(), b"2345", "{}", path);
    }
    // Requests: only GETs with the right headers.
    let log = log.lock().unwrap();
    for (m, _p, h) in log.iter() {
        assert_eq!(m, "GET");
        let hv = |n: &str| h.iter().find(|(k, _)| k == n).map(|(_, v)| v.as_str());
        assert_eq!(hv("range"), Some("bytes=2-5"));
        assert_eq!(hv("accept-encoding"), Some("identity"));
    }
    let pinned = &log[1].2;
    assert!(pinned.contains(&("if-match".into(), ETAG.into())));
    assert!(pinned.contains(&("if-unmodified-since".into(), "Sun, 09 Sep 2001 01:46:40 GMT".into())));
}

#[test]
fn http_window_and_multiple_ranges() {
    let (port, log) = serve();
    let u = format!("http://127.0.0.1:{}/obj", port);
    let ar = archive_for(&u, (None, None, None), vec![src_range(0, 0, 4), lit(b"--"), src_range(0, 10, 6)]);
    assert_eq!(ar.get("k", Request::Range(2, 8)).unwrap().unwrap(), b"23--ab");
    let ranges: Vec<String> = log.lock().unwrap().iter().map(|r| r.2.iter().find(|h| h.0 == "range").unwrap().1.clone()).collect();
    assert_eq!(ranges, vec!["bytes=2-3", "bytes=10-11"]);
}

fn http_err(path: &str, pins: (Option<u64>, Option<&str>, Option<i64>), a: u64, b: u64) {
    let (port, _) = serve();
    assert_eq!(class(get(port, path, pins, a, b)), "resolution", "{}", path);
}

#[test]
fn http_error_size_pin_mismatch() {
    http_err("/obj", (Some(15), None, None), 0, 2);
}

#[test]
fn http_error_size_pin_mismatch_on_200() {
    http_err("/full", (Some(15), None, None), 0, 2);
}

#[test]
fn http_error_size_pin_with_unknown_total() {
    http_err("/star", (Some(16), None, None), 0, 2);
}

#[test]
fn http_error_etag_412() {
    http_err("/obj", (None, Some("\"other\""), None), 0, 2);
}

#[test]
fn http_error_mtime_412() {
    http_err("/obj", (None, None, Some(MTIME - 1)), 0, 2);
}

#[test]
fn http_error_mtime_out_of_range() {
    http_err("/obj", (None, None, Some(-62135596801)), 0, 2);
}

#[test]
fn http_error_416() {
    http_err("/obj", (None, None, None), 20, 22);
}

#[test]
fn http_error_short_object() {
    http_err("/obj", (None, None, None), 14, 18);
}

#[test]
fn http_error_short_object_on_200() {
    http_err("/full", (None, None, None), 14, 18);
}

#[test]
fn http_error_content_encoding() {
    http_err("/gzip", (None, None, None), 0, 2);
}

#[test]
fn http_error_wrong_range_returned() {
    http_err("/shifted", (None, None, None), 0, 2);
}

#[test]
fn http_error_404() {
    http_err("/nope", (None, None, None), 0, 2);
}

#[test]
fn http_error_redirect_loop() {
    http_err("/loop", (None, None, None), 0, 2);
}

#[test]
fn http_error_connection_refused() {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    drop(l);
    assert_eq!(class(get(port, "/obj", (None, None, None), 0, 2)), "resolution");
}
