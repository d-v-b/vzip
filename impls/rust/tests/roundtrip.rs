mod common;

use common::*;
use std::process::Command;
use vzip::desc::parse_description;
use vzip::{write_archive, Archive, Kind, Request};

fn cli() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("vzip")
}
use std::path::PathBuf;

fn desc_for(page_size: Option<u64>, mirror: bool, compress: bool) -> String {
    let ps = page_size.map(|p| p.to_string()).unwrap_or("null".into());
    format!(
        r#"{{
  "page_size": {ps}, "mirror": {mirror},
  "sources": [{{"url": "data.bin", "size": 26}}, {{"key": "__vz__/hdr"}}, {{"data": "48445221"}},
              {{"url": "sub/%C3%A9%20x.bin"}}, {{"key": "z/bytes"}}],
  "entries": [
    {{"key": "x/zarr.json", "bytes": "7b7d", "compress": {compress}, "pinned": {pinned}}},
    {{"key": "__vz__/hdr", "bytes": "aabbccdd", "compress": {compress}, "pinned": {pinned}}},
    {{"key": "x/c/0", "ranges": [{{"source": 0, "offset": 10, "length": 4}}]}},
    {{"key": "x/c/1", "ranges": [{{"source": 2, "offset": 0, "length": 3}}, {{"data": "00ff"}}, {{"source": 1, "offset": 1, "length": 3}}]}},
    {{"key": "x/c/2", "ranges": []}},
    {{"key": "x/c/3", "ranges": [{{"data": ""}}]}},
    {{"key": "x/c/4", "ranges": [{{"source": 3, "offset": 1, "length": 2}}, {{"source": 4, "offset": 0, "length": 5}}]}},
    {{"key": "x/c/5", "ranges": [{{}}]}},
    {{"key": "z/bytes", "bytes": "68656c6c6f", "compress": {compress}}},
    {{"key": "é", "bytes": "01"}},
    {{"key": "😀", "bytes": "02"}},
    {{"key": "～", "bytes": "03"}},
    {{"key": "﻿bom", "bytes": "04"}},
    {{"key": "a/../b", "bytes": "05"}},
    {{"key": "x/", "bytes": ""}}
  ]
}}"#,
        pinned = page_size.is_some()
    )
}

#[test]
fn roundtrip_all_layouts() {
    let dir = tmpdir();
    std::fs::write(dir.join("data.bin"), b"abcdefghijklmnopqrstuvwxyz").unwrap();
    std::fs::create_dir_all(dir.join("sub")).unwrap();
    std::fs::write(dir.join("sub/é x.bin"), b"PQRS").unwrap();
    let mut n = 0;
    for page_size in [None, Some(1), Some(60), Some(100), Some(1 << 20)] {
        for mirror in [true, false] {
            for compress in [false, true] {
                n += 1;
                let spec = parse_description(&desc_for(page_size, mirror, compress)).unwrap();
                let bytes = write_archive(&spec).unwrap();
                let path = dir.join(format!("a{n}.vzip"));
                std::fs::write(&path, &bytes).unwrap();
                let a = Archive::open_path(&path).unwrap();
                let get = |k: &str, r: Request| a.get(k, r).unwrap();
                assert_eq!(a.classify("x/zarr.json").unwrap(), Kind::Bytes);
                assert_eq!(a.classify("x/c/0").unwrap(), Kind::Reference);
                assert_eq!(a.classify("__vz__/hdr").unwrap(), Kind::Missing);
                assert_eq!(a.classify("__vz__/sources").unwrap(), Kind::Missing);
                assert_eq!(a.classify("nope").unwrap(), Kind::Missing);
                assert_eq!(get("x/zarr.json", Request::Whole).unwrap(), b"{}");
                assert_eq!(get("x/c/0", Request::Whole).unwrap(), b"klmn");
                assert_eq!(get("x/c/1", Request::Whole).unwrap(), b"HDR\x00\xff\xbb\xcc\xdd");
                assert_eq!(get("x/c/1", Request::Range(2, 6)).unwrap(), b"R\x00\xff\xbb");
                assert_eq!(get("x/c/1", Request::Range(6, 100)).unwrap(), b"\xcc\xdd");
                assert_eq!(get("x/c/1", Request::Range(100, 200)).unwrap(), b"");
                assert_eq!(get("x/c/1", Request::Offset(5)).unwrap(), b"\xbb\xcc\xdd");
                assert_eq!(get("x/c/1", Request::Suffix(2)).unwrap(), b"\xcc\xdd");
                assert_eq!(get("x/c/1", Request::Suffix(200)).unwrap().len(), 8);
                assert_eq!(get("x/c/2", Request::Whole).unwrap(), b"");
                assert_eq!(get("x/c/3", Request::Whole).unwrap(), b"");
                assert_eq!(get("x/c/4", Request::Whole).unwrap(), b"QRhello");
                assert_eq!(get("x/c/5", Request::Whole).unwrap(), b"");
                assert_eq!(get("z/bytes", Request::Range(1, 3)).unwrap(), b"el");
                assert_eq!(get("\u{feff}bom", Request::Whole).unwrap(), b"\x04");
                assert_eq!(get("__vz__/hdr", Request::Whole), None);
                assert_eq!(get("nope", Request::Whole), None);
                assert_eq!(a.get("nope", Request::Range(3, 2)).unwrap_err().class, vzip::ErrorClass::Request);
                assert_eq!(a.raw("__vz__/hdr").unwrap().unwrap(), b"\xaa\xbb\xcc\xdd");
                assert!(a.raw("__vz__/sources").unwrap().is_some());
                assert_eq!(a.raw("__vz__/index").unwrap().is_some(), page_size.is_some());
                let raw_ref = a.raw("x/c/0").unwrap().unwrap();
                assert_eq!(raw_ref.is_empty(), !mirror);
                let all = a.list("").unwrap();
                assert_eq!(
                    all,
                    vec!["a/../b", "x/", "x/c/0", "x/c/1", "x/c/2", "x/c/3", "x/c/4", "x/c/5", "x/zarr.json", "z/bytes", "\u{e9}", "\u{feff}bom", "\u{ff5e}", "\u{1F600}"]
                );
                assert_eq!(a.list("x/c/").unwrap().len(), 6);
                assert_eq!(a.list("x/c/4").unwrap(), vec!["x/c/4"]);
                assert_eq!(a.list("q").unwrap(), Vec::<String>::new());
                assert_eq!(a.list("__vz__/").unwrap(), Vec::<String>::new());
                // Info-ZIP accepts it.
                let st = Command::new("unzip").arg("-tqq").arg(&path).status().unwrap();
                assert!(st.success(), "unzip -t failed for {path:?}");
                // Determinism.
                assert_eq!(write_archive(&spec).unwrap(), bytes);
            }
        }
    }
}

#[test]
fn zip64_many_entries() {
    let dir = tmpdir();
    let mut entries = Vec::new();
    for i in 0..0xFFFFu32 {
        entries.push(format!(r#"{{"key": "k/{i:05}", "bytes": "{:02x}"}}"#, i & 0xff));
    }
    let d = format!(r#"{{"page_size": 4096, "entries": [{}]}}"#, entries.join(","));
    let bytes = write_archive(&parse_description(&d).unwrap()).unwrap();
    // zip64 end record present, EOCD count fields all ones.
    let eocd = bytes.len() - 60;
    assert_eq!(&bytes[eocd + 8..eocd + 12], &[0xff, 0xff, 0xff, 0xff]);
    assert_eq!(&bytes[eocd - 20..eocd - 16], &0x07064b50u32.to_le_bytes());
    let path = dir.join("z.vzip");
    std::fs::write(&path, &bytes).unwrap();
    let a = Archive::open_path(&path).unwrap();
    assert_eq!(a.get("k/65534", Request::Whole).unwrap().unwrap(), vec![0xfe]);
    assert_eq!(a.list("k/6553").unwrap().len(), 5);
    assert_eq!(a.list("").unwrap().len(), 0xFFFF);
    let st = Command::new("unzip").arg("-tqq").arg(&path).status().unwrap();
    assert!(st.success());
    // Unpaged as well.
    let d = format!(r#"{{"entries": [{}]}}"#, entries.join(","));
    let bytes = write_archive(&parse_description(&d).unwrap()).unwrap();
    std::fs::write(dir.join("z2.vzip"), &bytes).unwrap();
    let a = Archive::open_path(&dir.join("z2.vzip")).unwrap();
    assert_eq!(a.get("k/00001", Request::Whole).unwrap().unwrap(), vec![1]);
}

#[test]
fn cli_read_write() {
    let dir = tmpdir();
    std::fs::write(dir.join("data.bin"), b"abcdefghijklmnopqrstuvwxyz").unwrap();
    std::fs::write(dir.join("d.json"), desc_for(Some(64), true, true)).unwrap();
    std::fs::write(
        dir.join("q.json"),
        r#"[{"op":"classify","key":"x/c/0"},{"op":"get","key":"x/c/0","range":{"start":1,"end":3}},
            {"op":"get","key":"x/c/0","range":{"start":3,"end":1}},{"op":"get_raw","key":"__vz__/hdr"},
            {"op":"list","prefix":"x/c/"},{"op":"get","key":"missing"}]"#,
    )
    .unwrap();
    // Run from another directory with relative paths.
    let st = Command::new(cli()).current_dir(&dir).args(["write", "d.json", "out.vzip"]).status().unwrap();
    assert!(st.success());
    let out = Command::new(cli()).current_dir(&dir).args(["read", "out.vzip", "q.json"]).output().unwrap();
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["open"]["ok"], true);
    let r = &v["results"];
    assert_eq!(r[0]["kind"], "reference");
    assert_eq!(r[1]["value"], "6c6d");
    assert_eq!(r[2]["class"], "request");
    assert_eq!(r[3]["value"], "aabbccdd");
    assert_eq!(r[4]["keys"].as_array().unwrap().len(), 6);
    assert_eq!(r[5]["value"], serde_json::Value::Null);

    // Open failure is reported as JSON with exit 0.
    std::fs::write(dir.join("junk.vzip"), b"not a zip").unwrap();
    let out = Command::new(cli()).current_dir(&dir).args(["read", "junk.vzip", "q.json"]).output().unwrap();
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["open"]["class"], "archive");
    let out = Command::new(cli()).current_dir(&dir).args(["read", "nonexistent.vzip", "q.json"]).output().unwrap();
    assert!(out.status.success());

    // Invalid queries -> non-zero.
    for bad in [r#"[{"op":"frob","key":"a"}]"#, r#"[{"op":"get"}]"#, r#"[{"op":"get","key":"a","range":{"offset":1,"suffix":2}}]"#, r#"[{"op":"get","key":"a","range":{"start":-1,"end":2}}]"#, "{"] {
        std::fs::write(dir.join("bad.json"), bad).unwrap();
        let out = Command::new(cli()).current_dir(&dir).args(["read", "out.vzip", "bad.json"]).output().unwrap();
        assert!(!out.status.success(), "{bad}");
    }

    // Invalid description -> non-zero and no file.
    std::fs::write(dir.join("bad.json"), r#"{"entries":[{"key":"a","bytes":"0"}]}"#).unwrap();
    let out = Command::new(cli()).current_dir(&dir).args(["write", "bad.json", "bad.vzip"]).output().unwrap();
    assert!(!out.status.success());
    assert!(!dir.join("bad.vzip").exists());
}
