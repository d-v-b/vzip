//! Conformance harness CLI (HARNESS.md).

use serde_json::{json, Map, Value};
use std::path::Path;
use std::process::exit;
use vzip::proto::{Source, SourceKind};
use vzip::writer::{ArchiveSpec, EntrySpec, EntryValue, RangeSpec};
use vzip::{Archive, Kind, Request};

fn die(msg: &str) -> ! {
    eprintln!("vzip: {}", msg);
    exit(2)
}

fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{:02x}", x));
    }
    s
}

fn unhex(s: &str) -> Result<Vec<u8>, String> {
    let b = s.as_bytes();
    if b.len() % 2 != 0 {
        return Err(format!("hex string has odd length: {:?}", s));
    }
    let d = |c: u8| -> Result<u8, String> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            _ => Err(format!("invalid (or uppercase) hex digit in {:?}", s)),
        }
    };
    b.chunks(2).map(|p| Ok(d(p[0])? << 4 | d(p[1])?)).collect()
}

// ---------------------------------------------------------------------------
// Strict JSON helpers
// ---------------------------------------------------------------------------

type R<T> = Result<T, String>;

/// Gets a known member; `null` is invalid.
fn member<'a>(o: &'a Map<String, Value>, name: &str) -> R<Option<&'a Value>> {
    match o.get(name) {
        None => Ok(None),
        Some(Value::Null) => Err(format!("member {:?} is null", name)),
        Some(v) => Ok(Some(v)),
    }
}

fn as_u64(v: &Value, name: &str) -> R<u64> {
    match v {
        Value::Number(n) if n.is_u64() => Ok(n.as_u64().unwrap()),
        _ => Err(format!("{:?} must be a non-negative JSON integer", name)),
    }
}

fn as_i64(v: &Value, name: &str) -> R<i64> {
    match v {
        Value::Number(n) if n.is_i64() => Ok(n.as_i64().unwrap()),
        _ => Err(format!("{:?} must be a JSON integer", name)),
    }
}

fn as_bool(v: &Value, name: &str) -> R<bool> {
    v.as_bool().ok_or_else(|| format!("{:?} must be a boolean", name))
}

fn as_str<'a>(v: &'a Value, name: &str) -> R<&'a str> {
    v.as_str().ok_or_else(|| format!("{:?} must be a string", name))
}

fn as_obj<'a>(v: &'a Value, what: &str) -> R<&'a Map<String, Value>> {
    v.as_object().ok_or_else(|| format!("{} must be an object", what))
}

fn as_arr<'a>(v: &'a Value, what: &str) -> R<&'a Vec<Value>> {
    v.as_array().ok_or_else(|| format!("{} must be an array", what))
}

// ---------------------------------------------------------------------------
// write
// ---------------------------------------------------------------------------

fn parse_description(v: &Value) -> R<ArchiveSpec> {
    let o = as_obj(v, "description")?;
    let page_size = match o.get("page_size") {
        None | Some(Value::Null) => None,
        Some(v) => {
            let n = as_u64(v, "page_size")?;
            if n < 1 {
                return Err("page_size must be at least 1".into());
            }
            Some(n)
        }
    };
    let mirror = match member(o, "mirror")? {
        None => true,
        Some(v) => as_bool(v, "mirror")?,
    };
    let mut sources = Vec::new();
    if let Some(sv) = member(o, "sources")? {
        for (i, s) in as_arr(sv, "sources")?.iter().enumerate() {
            let so = as_obj(s, &format!("sources[{}]", i))?;
            let url = member(so, "url")?;
            let key = member(so, "key")?;
            let data = member(so, "data")?;
            let n = url.is_some() as u8 + key.is_some() as u8 + data.is_some() as u8;
            if n != 1 {
                return Err(format!("sources[{}] must have exactly one of url, key, data", i));
            }
            let kind = if let Some(u) = url {
                SourceKind::Url(as_str(u, "url")?.to_string())
            } else if let Some(k) = key {
                SourceKind::Key(as_str(k, "key")?.to_string())
            } else {
                SourceKind::Data(unhex(as_str(data.unwrap(), "data")?)?)
            };
            let size = member(so, "size")?.map(|v| as_u64(v, "size")).transpose()?;
            let etag = member(so, "etag")?.map(|v| as_str(v, "etag").map(|s| s.to_string())).transpose()?;
            let mna = member(so, "modified_not_after")?
                .map(|v| as_i64(v, "modified_not_after"))
                .transpose()?;
            sources.push(Source { kind: Some(kind), size, etag, modified_not_after: mna });
        }
    }
    let mut entries = Vec::new();
    if let Some(ev) = member(o, "entries")? {
        for (i, e) in as_arr(ev, "entries")?.iter().enumerate() {
            let eo = as_obj(e, &format!("entries[{}]", i))?;
            let key = as_str(
                member(eo, "key")?.ok_or_else(|| format!("entries[{}] has no key", i))?,
                "key",
            )?
            .to_string();
            let compress = member(eo, "compress")?.map(|v| as_bool(v, "compress")).transpose()?.unwrap_or(false);
            let pinned = member(eo, "pinned")?.map(|v| as_bool(v, "pinned")).transpose()?.unwrap_or(false);
            let bytes = member(eo, "bytes")?;
            let ranges = member(eo, "ranges")?;
            let value = match (bytes, ranges) {
                (Some(b), None) => EntryValue::Bytes { data: unhex(as_str(b, "bytes")?)?, compress },
                (None, Some(r)) => {
                    if compress {
                        return Err(format!("entries[{}]: compress is only allowed on bytes entries", i));
                    }
                    let mut out = Vec::new();
                    for (k, rv) in as_arr(r, "ranges")?.iter().enumerate() {
                        let ro = as_obj(rv, &format!("entries[{}].ranges[{}]", i, k))?;
                        let src = member(ro, "source")?;
                        let off = member(ro, "offset")?;
                        let len = member(ro, "length")?;
                        let data = member(ro, "data")?;
                        if let Some(d) = data {
                            if src.is_some() || off.is_some() || len.is_some() {
                                return Err(format!("entries[{}].ranges[{}] mixes data with source fields", i, k));
                            }
                            out.push(RangeSpec::Literal(unhex(as_str(d, "data")?)?));
                        } else {
                            out.push(RangeSpec::Source {
                                source: src.map(|v| as_u64(v, "source")).transpose()?.unwrap_or(0),
                                offset: off.map(|v| as_u64(v, "offset")).transpose()?.unwrap_or(0),
                                length: len.map(|v| as_u64(v, "length")).transpose()?.unwrap_or(0),
                            });
                        }
                    }
                    EntryValue::Ranges(out)
                }
                _ => return Err(format!("entries[{}] must have exactly one of bytes, ranges", i)),
            };
            entries.push(EntrySpec { key, value, pinned });
        }
    }
    Ok(ArchiveSpec { page_size, mirror, sources, entries })
}

fn cmd_write(desc_path: &str, out_path: &str) {
    let text = std::fs::read(desc_path).unwrap_or_else(|e| die(&format!("cannot read description: {}", e)));
    let v: Value = serde_json::from_slice(&text).unwrap_or_else(|e| die(&format!("invalid JSON: {}", e)));
    let spec = parse_description(&v).unwrap_or_else(|e| die(&format!("invalid description: {}", e)));
    let bytes = vzip::writer::write_archive(&spec).unwrap_or_else(|e| die(&format!("invalid description: {}", e)));
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(out_path)
        .unwrap_or_else(|e| die(&format!("cannot create {}: {}", out_path, e)));
    use std::io::Write;
    if let Err(e) = f.write_all(&bytes).and_then(|_| f.sync_all()) {
        let _ = std::fs::remove_file(out_path);
        die(&format!("write failed: {}", e));
    }
}

// ---------------------------------------------------------------------------
// read
// ---------------------------------------------------------------------------

enum Query {
    Classify(String),
    Get(String, Request),
    Raw(String),
    List(String),
}

fn parse_query(v: &Value) -> R<Query> {
    let o = as_obj(v, "query")?;
    let op = as_str(member(o, "op")?.ok_or("query has no op")?, "op")?;
    let key = || -> R<String> { Ok(as_str(member(o, "key")?.ok_or("query has no key")?, "key")?.to_string()) };
    match op {
        "classify" => Ok(Query::Classify(key()?)),
        "get_raw" => {
            if o.contains_key("range") {
                return Err("get_raw takes no range".into());
            }
            Ok(Query::Raw(key()?))
        }
        "list" => Ok(Query::List(
            as_str(member(o, "prefix")?.ok_or("list query has no prefix")?, "prefix")?.to_string(),
        )),
        "get" => {
            let k = key()?;
            let req = match member(o, "range")? {
                None => Request::Whole,
                Some(r) => {
                    let ro = as_obj(r, "range")?;
                    let start = member(ro, "start")?;
                    let end = member(ro, "end")?;
                    let offset = member(ro, "offset")?;
                    let suffix = member(ro, "suffix")?;
                    match (start, end, offset, suffix) {
                        (Some(s), Some(e), None, None) => Request::Range(as_u64(s, "start")?, as_u64(e, "end")?),
                        (None, None, Some(s), None) => Request::Offset(as_u64(s, "offset")?),
                        (None, None, None, Some(n)) => Request::Suffix(as_u64(n, "suffix")?),
                        _ => return Err("range must have exactly one form".into()),
                    }
                }
            };
            Ok(Query::Get(k, req))
        }
        other => Err(format!("unknown op {:?}", other)),
    }
}

fn err_json(e: &vzip::Error) -> Value {
    json!({"ok": false, "class": e.class(), "error": e.message()})
}

fn cmd_read(archive_path: &str, queries_path: &str) {
    let text = std::fs::read(queries_path).unwrap_or_else(|e| die(&format!("cannot read queries: {}", e)));
    let v: Value = serde_json::from_slice(&text).unwrap_or_else(|e| die(&format!("invalid queries JSON: {}", e)));
    let arr = as_arr(&v, "queries").unwrap_or_else(|e| die(&e));
    let queries: Vec<Query> = arr
        .iter()
        .map(parse_query)
        .collect::<R<Vec<_>>>()
        .unwrap_or_else(|e| die(&format!("invalid query: {}", e)));

    let ar = match Archive::open(Path::new(archive_path)) {
        Ok(a) => a,
        Err(e) => {
            let out = json!({"open": {"ok": false, "class": "archive", "error": e.message()}, "results": []});
            println!("{}", out);
            return;
        }
    };
    let mut results = Vec::new();
    for q in &queries {
        let r = match q {
            Query::Classify(k) => match ar.classify(k) {
                Ok(kind) => json!({"ok": true, "kind": match kind {
                    Kind::Bytes => "bytes", Kind::Reference => "reference", Kind::Missing => "missing"}}),
                Err(e) => err_json(&e),
            },
            Query::Get(k, req) => match ar.get(k, *req) {
                Ok(Some(b)) => json!({"ok": true, "value": hex(&b)}),
                Ok(None) => json!({"ok": true, "value": null}),
                Err(e) => err_json(&e),
            },
            Query::Raw(k) => match ar.raw(k) {
                Ok(Some(b)) => json!({"ok": true, "value": hex(&b)}),
                Ok(None) => json!({"ok": true, "value": null}),
                Err(e) => err_json(&e),
            },
            Query::List(p) => match ar.list(p) {
                Ok(keys) => json!({"ok": true, "keys": keys}),
                Err(e) => err_json(&e),
            },
        };
        results.push(r);
    }
    println!("{}", json!({"open": {"ok": true}, "results": results}));
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("read") if args.len() == 4 => cmd_read(&args[2], &args[3]),
        Some("write") if args.len() == 4 => cmd_write(&args[2], &args[3]),
        _ => die("usage: vzip read <archive> <queries> | vzip write <description> <out>"),
    }
}
