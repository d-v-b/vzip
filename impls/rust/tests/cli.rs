mod common;
use common::*;
use serde_json::{json, Value};
use std::path::Path;
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_vzip-cli");

fn run(args: &[&str]) -> (i32, String, String) {
    let o = Command::new(BIN).args(args).output().unwrap();
    (
        o.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&o.stdout).into_owned(),
        String::from_utf8_lossy(&o.stderr).into_owned(),
    )
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn write_desc(dir: &Path, desc: &Value) -> (i32, std::path::PathBuf) {
    let d = write_tmp(dir, "desc.json", desc.to_string().as_bytes());
    let out = dir.join("out.vzip");
    let (code, _, _) = run(&[d.to_str().unwrap(), out.to_str().unwrap()].iter().fold(vec!["write"], |mut v, s| {
        v.push(s);
        v
    }));
    (code, out)
}

fn read(archive: &Path, dir: &Path, queries: &Value) -> Value {
    let q = write_tmp(dir, "q.json", queries.to_string().as_bytes());
    let (code, out, err) = run(&["read", archive.to_str().unwrap(), q.to_str().unwrap()]);
    assert_eq!(code, 0, "{}", err);
    serde_json::from_str(&out).unwrap()
}

fn unzip_ok(p: &Path) {
    let o = Command::new("unzip").arg("-t").arg(p).output().unwrap();
    assert!(o.status.success(), "unzip -t failed: {}", String::from_utf8_lossy(&o.stdout));
}

/// Expected value of each entry, computed from the description.
fn expected(desc: &Value, dir: &Path) -> Vec<(String, String, Vec<u8>, Vec<u8>)> {
    let unhex = |s: &str| (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect::<Vec<u8>>();
    let empty = vec![];
    let entries = desc["entries"].as_array().unwrap_or(&empty);
    let bytes_of = |k: &str| -> Vec<u8> {
        unhex(entries.iter().find(|e| e["key"] == k).unwrap()["bytes"].as_str().unwrap())
    };
    let sources = desc["sources"].as_array().cloned().unwrap_or_default();
    let mut out = Vec::new();
    for e in entries {
        let key = e["key"].as_str().unwrap().to_string();
        if let Some(b) = e.get("bytes") {
            out.push((key, "bytes".into(), unhex(b.as_str().unwrap()), vec![]));
        } else {
            let mut v = Vec::new();
            for r in e["ranges"].as_array().unwrap() {
                if let Some(d) = r.get("data") {
                    v.extend(unhex(d.as_str().unwrap()));
                    continue;
                }
                let s = &sources[r.get("source").and_then(|x| x.as_u64()).unwrap_or(0) as usize];
                let off = r.get("offset").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
                let len = r.get("length").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
                let val = if let Some(d) = s.get("data") {
                    unhex(d.as_str().unwrap())
                } else if let Some(k) = s.get("key") {
                    bytes_of(k.as_str().unwrap())
                } else {
                    std::fs::read(dir.join(s["url"].as_str().unwrap())).unwrap()
                };
                v.extend_from_slice(&val[off..off + len]);
            }
            out.push((key, "reference".into(), v, vec![]));
        }
    }
    out
}

fn roundtrip(desc: Value) {
    let dir = tmpdir();
    write_tmp(&dir, "src.bin", b"The quick brown fox jumps over the lazy dog");
    let (code, out) = write_desc(&dir, &desc);
    assert_eq!(code, 0, "write failed for {}", desc);
    unzip_ok(&out);
    let exp = expected(&desc, &dir);
    let mut queries = vec![json!({"op": "list", "prefix": ""})];
    for (k, _, _, _) in &exp {
        queries.push(json!({"op": "classify", "key": k}));
        queries.push(json!({"op": "get", "key": k}));
        queries.push(json!({"op": "get", "key": k, "range": {"start": 1, "end": 3}}));
        queries.push(json!({"op": "get", "key": k, "range": {"suffix": 2}}));
        queries.push(json!({"op": "get_raw", "key": k}));
    }
    let res = read(&out, &dir, &Value::Array(queries));
    assert_eq!(res["open"]["ok"], true, "{}", res);
    let r = res["results"].as_array().unwrap();
    let mut visible: Vec<&String> = exp.iter().map(|e| &e.0).filter(|k| !k.starts_with("__vz__/")).collect();
    visible.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    assert_eq!(r[0]["keys"], json!(visible));
    let mirror = desc.get("mirror").and_then(|m| m.as_bool()).unwrap_or(true);
    for (i, (k, kind, v, _)) in exp.iter().enumerate() {
        let base = 1 + i * 5;
        let hidden = k.starts_with("__vz__/");
        let kind_exp = if hidden { "missing" } else { kind.as_str() };
        assert_eq!(r[base]["kind"], kind_exp, "{}", k);
        if hidden {
            assert_eq!(r[base + 1]["value"], Value::Null);
        } else {
            assert_eq!(r[base + 1]["value"], hex(v), "{}", k);
            let n = v.len();
            assert_eq!(r[base + 2]["value"], hex(&v[1.min(n)..3.min(n)]));
            assert_eq!(r[base + 3]["value"], hex(&v[n.saturating_sub(2)..]));
        }
        let raw = r[base + 4]["value"].as_str().unwrap();
        if kind == "bytes" {
            assert_eq!(raw, hex(v));
        } else if !mirror {
            assert_eq!(raw, "");
        } else {
            assert!(!raw.is_empty() || v.is_empty());
        }
    }
}

fn sample(page_size: Value, mirror: bool) -> Value {
    let mut entries = vec![
        json!({"key": "x/zarr.json", "bytes": hex(b"{\"zarr_format\":3}"), "compress": false, "pinned": !page_size.is_null()}),
        json!({"key": "__vz__/hdr", "bytes": "48445221", "pinned": !page_size.is_null(), "compress": true}),
        json!({"key": "x/c/0", "ranges": [{"source": 0, "offset": 4, "length": 5}]}),
        json!({"key": "x/c/1", "ranges": [{"source": 2, "offset": 0, "length": 3}, {"data": "00ff"}, {"source": 1, "offset": 1, "length": 2}]}),
        json!({"key": "x/c/2", "ranges": []}),
        json!({"key": "x/c/3", "ranges": [{"data": ""}]}),
        json!({"key": "x/c/4", "ranges": [{}], "compress": false}),
        json!({"key": "empty", "bytes": ""}),
        json!({"key": "\u{1F600}", "bytes": "01"}),
        json!({"key": "\u{FF5E}", "bytes": "02", "compress": true}),
        json!({"key": "\u{FEFF}bom", "bytes": "03"}),
        json!({"key": "a/../b", "bytes": "04"}),
        json!({"key": "x/", "bytes": "05"}),
    ];
    for i in 0..120 {
        entries.push(json!({"key": format!("arr/c/{}/{}", i % 7, i), "ranges": [{"source": 0, "offset": i % 40, "length": 3}, {"data": format!("{:02x}", i)}]}));
        entries.push(json!({"key": format!("big/{}", i), "bytes": hex(&vec![i as u8; i * 3]), "compress": i % 2 == 0}));
    }
    json!({
        "page_size": page_size,
        "mirror": mirror,
        "sources": [
            {"url": "src.bin", "size": 43},
            {"key": "__vz__/hdr"},
            {"data": "deadbeef"}
        ],
        "entries": entries,
        "unknown_member": {"whatever": null}
    })
}

#[test]
fn roundtrips() {
    roundtrip(sample(Value::Null, true));
    roundtrip(sample(Value::Null, false));
    roundtrip(sample(json!(1), true));
    roundtrip(sample(json!(200), false));
    roundtrip(sample(json!(100000), true));
    roundtrip(json!({}));
    roundtrip(json!({"page_size": 10, "entries": []}));
    roundtrip(json!({"entries": [{"key": "only", "bytes": "aa"}], "page_size": null}));
}

#[test]
fn written_paged_archive_matches_spec_layout() {
    let dir = tmpdir();
    let (code, out) = write_desc(&dir, &sample(json!(500), true));
    assert_eq!(code, 0);
    let ar = vzip::Archive::open(&out).unwrap();
    let ix = vzip::proto::decode_cd_index(&ar.raw("__vz__/index").unwrap().unwrap()).unwrap();
    assert!(ix.pages.len() > 3);
    assert_eq!(ix.pinned.len(), 2);
    for w in ix.pages.windows(2) {
        assert!(w[0].first_key.as_bytes() < w[1].first_key.as_bytes());
        assert_eq!(w[0].offset + w[0].length, w[1].offset);
    }
}

#[test]
fn read_open_failure_is_reported_as_json() {
    let dir = tmpdir();
    let bad = write_tmp(&dir, "bad.vzip", b"nope");
    let res = read(&bad, &dir, &json!([{"op": "classify", "key": "a"}]));
    assert_eq!(res["open"]["ok"], false);
    assert_eq!(res["open"]["class"], "archive");
    assert_eq!(res["results"], json!([]));
    let res = read(&dir.join("does-not-exist"), &dir, &json!([]));
    assert_eq!(res["open"]["class"], "archive");
}

#[test]
fn read_query_errors_do_not_stop_others() {
    let dir = tmpdir();
    let (_, out) = write_desc(&dir, &json!({"sources": [{"url": "missing.bin"}], "entries": [
        {"key": "r", "ranges": [{"source": 0, "length": 2}]}, {"key": "b", "bytes": "01"}]}));
    let res = read(&out, &dir, &json!([
        {"op": "get", "key": "r"},
        {"op": "get", "key": "b", "range": {"start": 3, "end": 1}},
        {"op": "get", "key": "b", "range": {"offset": 0}},
        {"op": "list", "prefix": "zz"}
    ]));
    assert_eq!(res["results"][0]["class"], "resolution");
    assert_eq!(res["results"][1]["class"], "request");
    assert_eq!(res["results"][2]["value"], "01");
    assert_eq!(res["results"][3]["keys"], json!([]));
}

fn bad_queries(q: Value) {
    let dir = tmpdir();
    let (_, out) = write_desc(&dir, &json!({}));
    let qp = write_tmp(&dir, "q.json", q.to_string().as_bytes());
    let (code, _, _) = run(&["read", out.to_str().unwrap(), qp.to_str().unwrap()]);
    assert_ne!(code, 0, "{}", q);
}

#[test]
fn invalid_queries_unknown_op() {
    bad_queries(json!([{"op": "frob", "key": "a"}]));
}
#[test]
fn invalid_queries_missing_key() {
    bad_queries(json!([{"op": "get"}]));
}
#[test]
fn invalid_queries_range_with_several_forms() {
    bad_queries(json!([{"op": "get", "key": "a", "range": {"offset": 1, "suffix": 2}}]));
}
#[test]
fn invalid_queries_not_json() {
    bad_queries(json!("not an array"));
}

// ------------------------------------------------------------ invalid descriptions

fn bad_desc(desc: Value) {
    let dir = tmpdir();
    write_tmp(&dir, "src.bin", b"x");
    let (code, out) = write_desc(&dir, &desc);
    assert_ne!(code, 0, "accepted {}", desc);
    assert!(!out.exists(), "created a file for {}", desc);
}

#[test]
fn invalid_empty_key() {
    bad_desc(json!({"entries": [{"key": "", "bytes": ""}]}));
}
#[test]
fn invalid_duplicate_key() {
    bad_desc(json!({"entries": [{"key": "a", "bytes": ""}, {"key": "a", "bytes": "00"}]}));
}
#[test]
fn invalid_format_entry_key() {
    bad_desc(json!({"entries": [{"key": "__vz__/sources", "bytes": ""}]}));
    bad_desc(json!({"entries": [{"key": "__vz__/index", "bytes": ""}]}));
}
#[test]
fn invalid_source_index_out_of_range() {
    bad_desc(json!({"entries": [{"key": "a", "ranges": [{}]}]}));
    bad_desc(json!({"sources": [{"data": "00"}], "entries": [{"key": "a", "ranges": [{"source": 1}]}]}));
}
#[test]
fn invalid_empty_url() {
    bad_desc(json!({"sources": [{"url": ""}]}));
}
#[test]
fn invalid_url_syntax() {
    bad_desc(json!({"sources": [{"url": "a b.bin"}]}));
    bad_desc(json!({"sources": [{"url": "\u{e9}.bin"}]}));
    bad_desc(json!({"sources": [{"url": "1x:y"}]}));
}
#[test]
fn invalid_key_source_absent() {
    bad_desc(json!({"sources": [{"key": "nope"}]}));
}
#[test]
fn invalid_key_source_reference() {
    bad_desc(json!({"sources": [{"key": "r"}], "entries": [{"key": "r", "ranges": []}]}));
}
#[test]
fn invalid_key_source_format_entry() {
    bad_desc(json!({"sources": [{"key": "__vz__/sources"}]}));
}
#[test]
fn invalid_pin_on_key_or_data_source() {
    bad_desc(json!({"sources": [{"data": "00", "size": 1}]}));
    bad_desc(json!({"sources": [{"key": "a", "etag": "\"x\""}], "entries": [{"key": "a", "bytes": ""}]}));
}
#[test]
fn invalid_weak_or_unquoted_etag() {
    bad_desc(json!({"sources": [{"url": "x", "etag": "W/\"x\""}]}));
    bad_desc(json!({"sources": [{"url": "x", "etag": "x"}]}));
}
#[test]
fn invalid_payload_too_large() {
    bad_desc(json!({"entries": [{"key": "a", "ranges": [{"data": "00".repeat(65520)}]}]}));
}
#[test]
fn invalid_offset_plus_length_overflow() {
    bad_desc(json!({"sources": [{"data": ""}], "entries": [{"key": "a", "ranges": [{"offset": u64::MAX, "length": 1}]}]}));
}
#[test]
fn invalid_total_size_overflow() {
    bad_desc(json!({"sources": [{"data": ""}], "entries": [{"key": "a", "ranges": [{"length": u64::MAX}, {"length": 1}]}]}));
}
#[test]
fn invalid_key_too_long() {
    bad_desc(json!({"entries": [{"key": "k".repeat(65536), "bytes": ""}]}));
}
#[test]
fn invalid_pinned_reference_or_unpaged() {
    bad_desc(json!({"page_size": 10, "entries": [{"key": "a", "ranges": [], "pinned": true}]}));
    bad_desc(json!({"entries": [{"key": "a", "bytes": "", "pinned": true}]}));
}
#[test]
fn invalid_compress_on_reference() {
    bad_desc(json!({"entries": [{"key": "a", "ranges": [], "compress": true}]}));
}
#[test]
fn invalid_mixed_range() {
    bad_desc(json!({"sources": [{"data": "00"}], "entries": [{"key": "a", "ranges": [{"data": "00", "length": 1}]}]}));
}
#[test]
fn invalid_bytes_and_ranges() {
    bad_desc(json!({"entries": [{"key": "a", "ranges": [], "bytes": ""}]}));
    bad_desc(json!({"entries": [{"key": "a"}]}));
}
#[test]
fn invalid_source_with_two_kinds() {
    bad_desc(json!({"sources": [{"data": "00", "url": "x"}]}));
    bad_desc(json!({"sources": [{}]}));
}
#[test]
fn invalid_page_size() {
    bad_desc(json!({"page_size": 0}));
    bad_desc(json!({"page_size": -1}));
    bad_desc(json!({"page_size": 1.0}));
    bad_desc(json!({"page_size": "1"}));
}
#[test]
fn invalid_types() {
    bad_desc(json!({"mirror": 1}));
    bad_desc(json!({"entries": [{"key": "a", "bytes": "", "compress": "yes"}]}));
    bad_desc(json!({"sources": [{"data": "00"}], "entries": [{"key": "a", "ranges": [{"offset": 1.5}]}]}));
    bad_desc(json!({"sources": [{"url": "x", "size": -1}]}));
    bad_desc(json!({"entries": {}}));
}
#[test]
fn invalid_hex() {
    bad_desc(json!({"entries": [{"key": "a", "bytes": "ABCD"}]}));
    bad_desc(json!({"entries": [{"key": "a", "bytes": "abc"}]}));
    bad_desc(json!({"entries": [{"key": "a", "bytes": "zz"}]}));
}
#[test]
fn invalid_null_member() {
    bad_desc(json!({"mirror": null}));
    bad_desc(json!({"entries": [{"key": "a", "bytes": "", "pinned": null}]}));
    bad_desc(json!({"sources": [{"url": "x", "size": null}]}));
}
