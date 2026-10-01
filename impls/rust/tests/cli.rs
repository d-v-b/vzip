//! End-to-end tests of the CLI (HARNESS.md), derived from SPEC.md.

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_vzip");

fn tmpdir(name: &str) -> PathBuf {
    let d = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn write(dir: &Path, desc: &Value, name: &str) -> (bool, PathBuf) {
    let dp = dir.join(format!("{name}.json"));
    std::fs::write(&dp, desc.to_string()).unwrap();
    let out = dir.join(name);
    let st = Command::new(BIN).arg("write").arg(&dp).arg(&out).status().unwrap();
    (st.success(), out)
}

fn read(archive: &Path, queries: &Value) -> Value {
    let qp = archive.with_extension("queries.json");
    std::fs::write(&qp, queries.to_string()).unwrap();
    let o = Command::new(BIN).arg("read").arg(archive).arg(&qp).output().unwrap();
    assert!(o.status.success(), "read failed: {}", String::from_utf8_lossy(&o.stderr));
    serde_json::from_slice(&o.stdout).unwrap()
}

fn unzip_ok(p: &Path) {
    let o = Command::new("unzip").arg("-t").arg(p).output().unwrap();
    assert!(o.status.success(), "unzip -t failed: {}", String::from_utf8_lossy(&o.stdout));
}

fn results(archive: &Path, queries: Value) -> Vec<Value> {
    let r = read(archive, &queries);
    assert_eq!(r["open"]["ok"], true, "{r}");
    r["results"].as_array().unwrap().clone()
}

fn ok_value(v: &[u8]) -> Value {
    json!({"ok": true, "value": hex(v)})
}

fn class(r: &Value) -> &str {
    assert_eq!(r["ok"], false, "{r}");
    r["class"].as_str().unwrap()
}

fn sample_desc(page_size: Value, mirror: bool) -> Value {
    json!({
        "page_size": page_size,
        "mirror": mirror,
        "sources": [
            {"url": "data%20file.bin", "size": 16},
            {"key": "__vz__/hdr"},
            {"data": "48445221"},
            {"url": "#frag"},
            {"url": "missing.bin"}
        ],
        "entries": [
            {"key": "x/zarr.json", "bytes": hex(b"{\"zarr\":3}"), "compress": true, "pinned": page_size != Value::Null},
            {"key": "x/raw", "bytes": hex(b"hello world")},
            {"key": "__vz__/hdr", "bytes": hex(b"HEADER"), "compress": true},
            {"key": "x/c/0", "ranges": [{"source": 0, "offset": 2, "length": 4}]},
            {"key": "x/c/1", "ranges": [{"source": 2, "offset": 0, "length": 3}, {"data": "00ff"}, {"source": 1, "offset": 1, "length": 2}]},
            {"key": "x/c/2", "ranges": []},
            {"key": "x/c/3", "ranges": [{"source": 3, "offset": 0, "length": 4}]},
            {"key": "x/c/4", "ranges": [{"data": "aa"}, {"source": 4, "offset": 0, "length": 0}, {"source": 4, "offset": 5, "length": 10}]},
            {"key": "y", "bytes": ""},
            {"key": "\u{1F600}", "bytes": "01"},
            {"key": "\u{FF5E}", "bytes": "02"}
        ]
    })
}

fn check_sample(a: &Path, mirror: bool) {
    let data: Vec<u8> = (0u8..16).collect();
    let r = results(
        a,
        json!([
            {"op": "classify", "key": "x/zarr.json"},
            {"op": "classify", "key": "x/c/1"},
            {"op": "classify", "key": "nope"},
            {"op": "classify", "key": "__vz__/hdr"},
            {"op": "get", "key": "x/zarr.json"},
            {"op": "get", "key": "x/raw", "range": {"start": 6, "end": 100}},
            {"op": "get", "key": "x/raw", "range": {"offset": 6}},
            {"op": "get", "key": "x/raw", "range": {"suffix": 5}},
            {"op": "get", "key": "x/raw", "range": {"suffix": 500}},
            {"op": "get", "key": "x/c/0"},
            {"op": "get", "key": "x/c/1"},
            {"op": "get", "key": "x/c/1", "range": {"start": 2, "end": 4}},
            {"op": "get", "key": "x/c/2"},
            {"op": "get", "key": "x/c/3"},
            {"op": "get", "key": "x/c/4", "range": {"start": 0, "end": 1}},
            {"op": "get", "key": "x/c/4"},
            {"op": "get", "key": "x/raw", "range": {"start": 5, "end": 4}},
            {"op": "get", "key": "__vz__/hdr"},
            {"op": "get_raw", "key": "__vz__/hdr"},
            {"op": "get", "key": "nope"},
            {"op": "list", "prefix": ""},
            {"op": "list", "prefix": "x/c/"},
            {"op": "list", "prefix": "__vz__/"},
            {"op": "get_raw", "key": "x/c/0"},
            {"op": "get", "key": "y"},
            {"op": "get_raw", "key": "nope"},
            {"op": "get", "key": "nope", "range": {"start": 2, "end": 1}},
        ]),
    );
    assert_eq!(r[0], json!({"ok": true, "kind": "bytes"}));
    assert_eq!(r[1], json!({"ok": true, "kind": "reference"}));
    assert_eq!(r[2], json!({"ok": true, "kind": "missing"}));
    assert_eq!(r[3], json!({"ok": true, "kind": "missing"}));
    assert_eq!(r[4], ok_value(b"{\"zarr\":3}"));
    assert_eq!(r[5], ok_value(b"world"));
    assert_eq!(r[6], ok_value(b"world"));
    assert_eq!(r[7], ok_value(b"world"));
    assert_eq!(r[8], ok_value(b"hello world"));
    assert_eq!(r[9], ok_value(&data[2..6]));
    assert_eq!(r[10], ok_value(b"HDR\x00\xffEA"));
    assert_eq!(r[11], ok_value(b"R\x00"));
    assert_eq!(r[12], ok_value(b""));
    assert_eq!(r[13], ok_value(b"PK\x03\x04"));
    assert_eq!(r[14], ok_value(b"\xaa"));
    assert_eq!(class(&r[15]), "resolution");
    assert_eq!(class(&r[16]), "request");
    assert_eq!(r[17], json!({"ok": true, "value": null}));
    assert_eq!(r[18], ok_value(b"HEADER"));
    assert_eq!(r[19], json!({"ok": true, "value": null}));
    assert_eq!(
        r[20],
        json!({"ok": true, "keys": ["x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/c/4", "x/raw", "x/zarr.json", "y", "\u{FF5E}", "\u{1F600}"]})
    );
    assert_eq!(r[21], json!({"ok": true, "keys": ["x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/c/4"]}));
    assert_eq!(r[22], json!({"ok": true, "keys": []}));
    let payload = vzip::proto::Range { source: 0, offset: 2, length: 4, data: None }.encode();
    assert_eq!(r[23], ok_value(if mirror { &payload } else { b"" }));
    assert_eq!(r[24], ok_value(b""));
    assert_eq!(r[25], json!({"ok": true, "value": null}));
    assert_eq!(class(&r[26]), "request");
}

#[test]
fn roundtrip_unpaged_and_paged() {
    for (name, ps, mirror) in [
        ("unpaged", Value::Null, true),
        ("unpaged_nomirror", Value::Null, false),
        ("paged1", json!(1), true),
        ("paged200", json!(200), false),
        ("paged_big", json!(100000), true),
    ] {
        let d = tmpdir(name);
        std::fs::write(d.join("data file.bin"), (0u8..16).collect::<Vec<_>>()).unwrap();
        let (ok, a) = write(&d, &sample_desc(ps.clone(), mirror), "a.vzip");
        assert!(ok, "{name}");
        unzip_ok(&a);
        check_sample(&a, mirror);
        // read via a relative path from another cwd
        let qp = d.join("q.json");
        std::fs::write(&qp, json!([{"op": "get", "key": "x/c/0"}]).to_string()).unwrap();
        let o = Command::new(BIN).current_dir(&d).args(["read", "./sub/../a.vzip", "q.json"]).output().unwrap();
        let _ = std::fs::create_dir_all(d.join("sub"));
        let o2 = Command::new(BIN).current_dir(&d).args(["read", "sub/../a.vzip", "q.json"]).output().unwrap();
        let v: Value = serde_json::from_slice(&o2.stdout).unwrap();
        assert_eq!(v["results"][0], ok_value(&[2, 3, 4, 5]), "{name}");
        let _ = o;
    }
}

#[test]
fn pins() {
    let d = tmpdir("pins");
    std::fs::write(d.join("f.bin"), b"0123456789").unwrap();
    let desc = json!({
        "sources": [
            {"url": "f.bin", "size": 10, "modified_not_after": 4102444800i64},
            {"url": "f.bin", "size": 11},
            {"url": "f.bin", "etag": "\"abc\""},
            {"url": "f.bin", "modified_not_after": 0},
            {"url": "file:///nonexistent/dir/x"},
            {"url": "http://example.invalid/x"},
            {"url": "f.bin%2Fx"},
            {"url": "file://otherhost/x"}
        ],
        "entries": (0..8).map(|i| json!({"key": format!("k{i}"), "ranges": [{"source": i, "offset": 1, "length": 2}]})).collect::<Vec<_>>()
    });
    let (ok, a) = write(&d, &desc, "a.vzip");
    assert!(ok);
    unzip_ok(&a);
    let qs: Vec<Value> = (0..8).map(|i| json!({"op": "get", "key": format!("k{i}")})).collect();
    let r = results(&a, json!(qs));
    assert_eq!(r[0], ok_value(b"12"));
    for x in &r[1..] {
        assert_eq!(class(x), "resolution", "{x}");
    }
}

fn patch(p: &Path, f: impl FnOnce(&mut Vec<u8>)) {
    let mut b = std::fs::read(p).unwrap();
    f(&mut b);
    std::fs::write(p, b).unwrap();
}

fn rfind(h: &[u8], n: &[u8]) -> usize {
    (0..=h.len() - n.len()).rev().find(|&i| &h[i..i + n.len()] == n).unwrap()
}

/// Find the central directory record for `name` and return its offset.
fn cd_record(b: &[u8], name: &str) -> usize {
    let mut sig = b"PK\x01\x02".to_vec();
    sig.extend_from_slice(&[0; 0]);
    (0..b.len() - 46)
        .find(|&i| {
            &b[i..i + 4] == b"PK\x01\x02" && {
                let nl = u16::from_le_bytes([b[i + 28], b[i + 29]]) as usize;
                b.get(i + 46..i + 46 + nl) == Some(name.as_bytes())
            }
        })
        .unwrap()
}

#[test]
fn entry_body_payload_errors() {
    for ps in [Value::Null, json!(1)] {
        let d = tmpdir(&format!("errs{}", ps));
        let desc = json!({
            "page_size": ps,
            "sources": [{"key": "__vz__/z"}],
            "entries": [
                {"key": "badmethod", "bytes": "0102"},
                {"key": "badpayload", "ranges": [{"data": "ee"}]},
                {"key": "__vz__/z", "bytes": hex(&[7u8; 100]), "compress": true},
                {"key": "viaz", "ranges": [{"source": 0, "offset": 0, "length": 2}]},
                {"key": "zz", "bytes": "0102"}
            ]
        });
        let (ok, a) = write(&d, &desc, "a.vzip");
        assert!(ok);
        patch(&a, |b| {
            let i = cd_record(b, "badmethod");
            b[i + 10] = 9;
            let j = rfind(b, &[0x2a, 0x01, 0xee]);
            b[j] = 0x2b; // wire type 3: malformed
            // corrupt the DEFLATE body of __vz__/z: first local header with that name
            let k = (0..b.len()).find(|&i| &b[i..i + 4] == b"PK\x03\x04" && &b[i + 30..i + 38] == b"__vz__/z").unwrap();
            let body = k + 30 + 8;
            b[body] = 0xff; // reserved block type 3
            // STORED size mismatch for zz
            let z = cd_record(b, "zz");
            b[z + 24] = 3;
        });
        let r = results(
            &a,
            json!([
                {"op": "classify", "key": "badmethod"},
                {"op": "get", "key": "badmethod"},
                {"op": "get_raw", "key": "badmethod"},
                {"op": "list", "prefix": "bad"},
                {"op": "classify", "key": "badpayload"},
                {"op": "get", "key": "badpayload"},
                {"op": "get_raw", "key": "__vz__/z"},
                {"op": "get", "key": "viaz"},
                {"op": "get", "key": "zz"},
                {"op": "get", "key": "badpayload", "range": {"start": 3, "end": 1}},
            ]),
        );
        assert_eq!(class(&r[0]), "entry");
        assert_eq!(class(&r[1]), "entry");
        assert_eq!(class(&r[2]), "entry");
        assert_eq!(r[3], json!({"ok": true, "keys": ["badmethod", "badpayload"]}));
        assert_eq!(r[4], json!({"ok": true, "kind": "reference"}));
        assert_eq!(class(&r[5]), "payload");
        assert_eq!(class(&r[6]), "body");
        assert_eq!(class(&r[7]), "resolution");
        assert_eq!(class(&r[8]), "body");
        assert_eq!(class(&r[9]), "request");
    }
}

#[test]
fn archive_errors() {
    let d = tmpdir("archerr");
    let (ok, a) = write(&d, &json!({"entries": [{"key": "a", "bytes": "00"}]}), "a.vzip");
    assert!(ok);
    let orig = std::fs::read(&a).unwrap();
    let cases: Vec<(&str, Box<dyn Fn(&mut Vec<u8>)>)> = vec![
        ("truncated", Box::new(|b: &mut Vec<u8>| b.truncate(b.len() - 1))),
        ("magic", Box::new(|b: &mut Vec<u8>| { let n = b.len(); b[n - 22] = b'x'; })),
        ("cdoffset", Box::new(|b: &mut Vec<u8>| { let n = b.len(); b[n - 44 + 16] = 0xf0; })),
        ("sources_off", Box::new(|b: &mut Vec<u8>| { let n = b.len(); b[n - 22 + 6] = 1; })),
        ("trailing", Box::new(|b: &mut Vec<u8>| b.extend_from_slice(b"junk"))),
        ("empty", Box::new(|b: &mut Vec<u8>| b.clear())),
    ];
    for (name, f) in cases {
        let mut b = orig.clone();
        f(&mut b);
        let p = d.join(format!("{name}.vzip"));
        std::fs::write(&p, &b).unwrap();
        let r = read(&p, &json!([{"op": "classify", "key": "a"}]));
        assert_eq!(r["open"]["ok"], false, "{name}");
        assert_eq!(r["open"]["class"], "archive", "{name}");
        assert_eq!(r["results"], json!([]));
    }
    let r = read(&d.join("does-not-exist"), &json!([]));
    assert_eq!(r["open"]["class"], "archive");
}

#[test]
fn writer_rejects() {
    let d = tmpdir("rejects");
    let base = |entries: Value, sources: Value| json!({"sources": sources, "entries": entries});
    let b = |k: &str| json!({"key": k, "bytes": "00"});
    let cases = vec![
        base(json!([b("a"), b("a")]), json!([])),
        base(json!([b("")]), json!([])),
        base(json!([b("__vz__/sources")]), json!([])),
        base(json!([b("__vz__/index")]), json!([])),
        base(json!([{"key": "r", "ranges": [{}]}]), json!([])),
        base(json!([{"key": "r", "ranges": [{"source": 1}]}]), json!([{"data": ""}])),
        base(json!([]), json!([{"url": ""}])),
        base(json!([]), json!([{"url": "a b"}])),
        base(json!([]), json!([{"url": "é"}])),
        base(json!([]), json!([{"key": "absent"}])),
        base(json!([{"key": "r", "ranges": []}]), json!([{"key": "r"}])),
        base(json!([]), json!([{"key": "__vz__/sources"}])),
        base(json!([b("a")]), json!([{"key": "a", "size": 1}])),
        base(json!([]), json!([{"data": "00", "etag": "\"x\""}])),
        base(json!([]), json!([{"url": "x", "etag": "W/\"x\""}])),
        base(json!([]), json!([{"url": "x", "etag": "x"}])),
        base(json!([]), json!([{"url": "x", "data": "00"}])),
        base(json!([{"key": "r", "ranges": [{"data": hex(&vec![0u8; 65520])}]}]), json!([])),
        base(json!([{"key": "a", "bytes": "00", "pinned": true}]), json!([])),
        json!({"page_size": 10, "entries": [{"key": "r", "ranges": [], "pinned": true}]}),
        base(json!([{"key": "r", "ranges": [], "compress": true}]), json!([])),
        base(json!([{"key": "r", "ranges": [{"data": "00", "offset": 0}]}]), json!([])),
        base(json!([{"key": "a", "bytes": "0A"}]), json!([])),
        base(json!([{"key": "a", "bytes": "0"}]), json!([])),
        base(json!([{"key": "a", "bytes": "00", "ranges": []}]), json!([])),
        base(json!([{"key": "a"}]), json!([])),
        base(json!([{"key": "a", "bytes": "00", "compress": 1}]), json!([])),
        base(json!([{"key": "a", "bytes": "00", "compress": null}]), json!([])),
        json!({"page_size": 0}),
        json!({"page_size": 1.0}),
        json!({"page_size": "1"}),
        json!({"mirror": null}),
        json!({"entries": [{"key": "r", "ranges": [{"source": 0, "offset": 1.0}]}], "sources": [{"data": ""}]}),
        base(json!([{"key": "r", "ranges": [{"source": 0, "offset": u64::MAX, "length": 1}]}]), json!([{"data": ""}])),
    ];
    for (i, c) in cases.iter().enumerate() {
        let (ok, out) = write(&d, c, &format!("r{i}.vzip"));
        assert!(!ok, "case {i} should be rejected: {c}");
        assert!(!out.exists(), "case {i} created a file");
    }
    // Accepted edge cases
    let good = vec![
        json!({}),
        json!({"page_size": 1}),
        json!({"page_size": 1, "entries": [{"key": "__vz__/p", "bytes": "00", "pinned": true}]}),
        json!({"sources": [{"url": "a"}], "entries": [{"key": "r", "ranges": [{}], "unknown": 1}], "extra": {}}),
        base(json!([{"key": "r", "ranges": [{"data": hex(&vec![0u8; 65515])}]}]), json!([])),
    ];
    for (i, c) in good.iter().enumerate() {
        let (ok, out) = write(&d, c, &format!("g{i}.vzip"));
        assert!(ok, "good case {i} rejected: {c}");
        unzip_ok(&out);
        let r = read(&out, &json!([{"op": "list", "prefix": ""}]));
        assert_eq!(r["open"]["ok"], true);
    }
}

#[test]
fn zip64_many_entries() {
    let d = tmpdir("zip64");
    let n = 70000;
    let entries: Vec<Value> = (0..n).map(|i| json!({"key": format!("k/{i:06}"), "bytes": hex(&(i as u32).to_le_bytes())})).collect();
    for ps in [Value::Null, json!(4096)] {
        let (ok, a) = write(&d, &json!({"page_size": ps, "entries": entries}), &format!("a{ps}.vzip"));
        assert!(ok);
        let b = std::fs::read(&a).unwrap();
        assert_eq!(&b[b.len() - 44 - 20 - (if ps.is_null() { 0 } else { 16 })..][..4], b"PK\x06\x07");
        unzip_ok(&a);
        let r = results(&a, json!([
            {"op": "get", "key": "k/069999"},
            {"op": "get", "key": "k/000000"},
            {"op": "classify", "key": "k/0700000"},
            {"op": "list", "prefix": "k/06999"}
        ]));
        assert_eq!(r[0], ok_value(&69999u32.to_le_bytes()));
        assert_eq!(r[1], ok_value(&0u32.to_le_bytes()));
        assert_eq!(r[2], json!({"ok": true, "kind": "missing"}));
        assert_eq!(r[3]["keys"].as_array().unwrap().len(), 10);
    }
}

#[test]
fn invalid_queries_exit_nonzero() {
    let d = tmpdir("badq");
    let (ok, a) = write(&d, &json!({}), "a.vzip");
    assert!(ok);
    for q in [
        json!([{"op": "frob", "key": "a"}]),
        json!([{"op": "get"}]),
        json!([{"op": "get", "key": "a", "range": {"offset": 1, "suffix": 2}}]),
        json!([{"op": "get", "key": "a", "range": {"start": 1}}]),
        json!({"op": "get"}),
    ] {
        let qp = d.join("q.json");
        std::fs::write(&qp, q.to_string()).unwrap();
        let st = Command::new(BIN).arg("read").arg(&a).arg(&qp).status().unwrap();
        assert!(!st.success(), "{q}");
    }
}
