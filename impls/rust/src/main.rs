//! CLI implementing HARNESS.md: `vzip read <archive> <queries>` and
//! `vzip write <description> <out>`.

use serde_json::{json, Map, Value};
use std::path::Path;
use std::process::ExitCode;
use vzip::proto::{Range, Source, SourceKind};
use vzip::reader::{Archive, Error, Kind, Request};
use vzip::writer::{self, Content, Entry, Spec};

fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{x:02x}"));
    }
    s
}

fn unhex(s: &str) -> Result<Vec<u8>, String> {
    if s.len() % 2 != 0 {
        return Err(format!("hex string {s:?} has odd length"));
    }
    let d = |c: u8| -> Result<u8, String> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            _ => Err(format!("invalid lowercase hex digit {:?}", c as char)),
        }
    };
    s.as_bytes().chunks(2).map(|p| Ok(d(p[0])? * 16 + d(p[1])?)).collect()
}

// ------------------------------------------------------------------ read

enum Query {
    Classify(String),
    Get(String, Request),
    GetRaw(String),
    List(String),
}

fn get_u64(o: &Map<String, Value>, k: &str) -> Result<u64, String> {
    o.get(k)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{k} must be a non-negative integer"))
}

fn parse_query(v: &Value) -> Result<Query, String> {
    let o = v.as_object().ok_or("query is not an object")?;
    let op = o.get("op").and_then(Value::as_str).ok_or("query has no string op")?;
    let key = || -> Result<String, String> {
        o.get("key").and_then(Value::as_str).map(str::to_string).ok_or_else(|| "query has no string key".into())
    };
    match op {
        "classify" => Ok(Query::Classify(key()?)),
        "get_raw" => {
            if o.contains_key("range") {
                return Err("get_raw takes no range".into());
            }
            Ok(Query::GetRaw(key()?))
        }
        "list" => Ok(Query::List(
            o.get("prefix").and_then(Value::as_str).ok_or("list query has no string prefix")?.to_string(),
        )),
        "get" => {
            let k = key()?;
            let req = match o.get("range") {
                None => Request::Whole,
                Some(r) => {
                    let r = r.as_object().ok_or("range is not an object")?;
                    let has = |n: &str| r.contains_key(n);
                    let forms = [has("start") || has("end"), has("offset"), has("suffix")];
                    if forms.iter().filter(|&&f| f).count() != 1 {
                        return Err("range must have exactly one form".into());
                    }
                    if forms[0] {
                        Request::Range(get_u64(r, "start")?, get_u64(r, "end")?)
                    } else if forms[1] {
                        Request::Offset(get_u64(r, "offset")?)
                    } else {
                        Request::Suffix(get_u64(r, "suffix")?)
                    }
                }
            };
            Ok(Query::Get(k, req))
        }
        _ => Err(format!("unknown op {op:?}")),
    }
}

fn err_json(e: &Error) -> Value {
    json!({"ok": false, "class": e.class(), "error": e.message()})
}

fn cmd_read(archive: &str, queries: &str) -> Result<(), String> {
    let text = std::fs::read(queries).map_err(|e| format!("cannot read queries file: {e}"))?;
    let v: Value = serde_json::from_slice(&text).map_err(|e| format!("invalid queries JSON: {e}"))?;
    let arr = v.as_array().ok_or("queries file is not a JSON array")?;
    let qs: Vec<Query> = arr.iter().map(parse_query).collect::<Result<_, _>>()?;
    let a = match Archive::open_path(Path::new(archive)) {
        Ok(a) => a,
        Err(e) => {
            let out = json!({"open": err_json(&e), "results": []});
            println!("{out}");
            return Ok(());
        }
    };
    let mut results = Vec::new();
    for q in qs {
        let r = match q {
            Query::Classify(k) => a.classify(&k).map(|kind| {
                let s = match kind {
                    Kind::Bytes => "bytes",
                    Kind::Reference => "reference",
                    Kind::Missing => "missing",
                };
                json!({"ok": true, "kind": s})
            }),
            Query::Get(k, req) => a.get(&k, req).map(|v| json!({"ok": true, "value": v.map(|b| hex(&b))})),
            Query::GetRaw(k) => a.raw(&k).map(|v| json!({"ok": true, "value": v.map(|b| hex(&b))})),
            Query::List(p) => a.list(&p).map(|keys| json!({"ok": true, "keys": keys})),
        };
        results.push(r.unwrap_or_else(|e| err_json(&e)));
    }
    println!("{}", json!({"open": {"ok": true}, "results": results}));
    Ok(())
}

// ------------------------------------------------------------------ write

/// Reject `null` for every member (only `page_size` may be null, and is
/// handled before this is called).
fn member<'a>(o: &'a Map<String, Value>, k: &str) -> Result<Option<&'a Value>, String> {
    match o.get(k) {
        None => Ok(None),
        Some(Value::Null) => Err(format!("member {k:?} is null")),
        Some(v) => Ok(Some(v)),
    }
}

fn opt_bool(o: &Map<String, Value>, k: &str, default: bool) -> Result<bool, String> {
    match member(o, k)? {
        None => Ok(default),
        Some(v) => v.as_bool().ok_or_else(|| format!("{k} must be a boolean")),
    }
}

fn opt_u64(o: &Map<String, Value>, k: &str) -> Result<Option<u64>, String> {
    match member(o, k)? {
        None => Ok(None),
        Some(v) => v.as_u64().map(Some).ok_or_else(|| format!("{k} must be a non-negative integer")),
    }
}

fn opt_str<'a>(o: &'a Map<String, Value>, k: &str) -> Result<Option<&'a str>, String> {
    match member(o, k)? {
        None => Ok(None),
        Some(v) => v.as_str().map(Some).ok_or_else(|| format!("{k} must be a string")),
    }
}

fn check_no_nulls(o: &Map<String, Value>, allow: &[&str]) -> Result<(), String> {
    for (k, v) in o {
        if v.is_null() && !allow.contains(&k.as_str()) {
            return Err(format!("member {k:?} is null"));
        }
    }
    Ok(())
}

fn parse_source(v: &Value) -> Result<Source, String> {
    let o = v.as_object().ok_or("source is not an object")?;
    check_no_nulls(o, &[])?;
    let url = opt_str(o, "url")?;
    let key = opt_str(o, "key")?;
    let data = opt_str(o, "data")?;
    let kind = match (url, key, data) {
        (Some(u), None, None) => SourceKind::Url(u.to_string()),
        (None, Some(k), None) => SourceKind::Key(k.to_string()),
        (None, None, Some(d)) => SourceKind::Data(unhex(d)?),
        _ => return Err("source must have exactly one of url, key, data".into()),
    };
    let size = opt_u64(o, "size")?;
    let etag = opt_str(o, "etag")?.map(str::to_string);
    let modified_not_after = match member(o, "modified_not_after")? {
        None => None,
        Some(v) => Some(v.as_i64().ok_or("modified_not_after must be an integer")?),
    };
    Ok(Source { kind, size, etag, modified_not_after })
}

fn parse_range(v: &Value) -> Result<Range, String> {
    let o = v.as_object().ok_or("range is not an object")?;
    check_no_nulls(o, &[])?;
    if let Some(d) = opt_str(o, "data")? {
        if o.contains_key("source") || o.contains_key("offset") || o.contains_key("length") {
            return Err("range mixes data with source/offset/length".into());
        }
        if !o.get("data").unwrap().is_string() {
            return Err("data must be a hex string".into());
        }
        return Ok(Range { data: Some(unhex(d)?), ..Default::default() });
    }
    let source = opt_u64(o, "source")?.unwrap_or(0);
    let source = u32::try_from(source).map_err(|_| "source index out of bounds".to_string())?;
    Ok(Range {
        source,
        offset: opt_u64(o, "offset")?.unwrap_or(0),
        length: opt_u64(o, "length")?.unwrap_or(0),
        data: None,
    })
}

fn parse_entry(v: &Value) -> Result<Entry, String> {
    let o = v.as_object().ok_or("entry is not an object")?;
    check_no_nulls(o, &[])?;
    let key = opt_str(o, "key")?.ok_or("entry has no key")?.to_string();
    let compress = opt_bool(o, "compress", false)?;
    let pinned = opt_bool(o, "pinned", false)?;
    let bytes = opt_str(o, "bytes")?;
    let ranges = member(o, "ranges")?;
    if o.contains_key("bytes") && !o.get("bytes").unwrap().is_string() {
        return Err("bytes must be a hex string".into());
    }
    let content = match (bytes, ranges) {
        (Some(b), None) => Content::Bytes { data: unhex(b)?, compress },
        (None, Some(r)) => {
            if compress {
                return Err(format!("entry {key:?}: compress is only allowed on bytes entries"));
            }
            let arr = r.as_array().ok_or("ranges must be an array")?;
            Content::Ranges(arr.iter().map(parse_range).collect::<Result<_, _>>()?)
        }
        _ => return Err(format!("entry {key:?} must have exactly one of bytes and ranges")),
    };
    Ok(Entry { key, content, pinned })
}

fn parse_description(v: &Value) -> Result<Spec, String> {
    let o = v.as_object().ok_or("description is not an object")?;
    check_no_nulls(o, &["page_size"])?;
    let page_size = match o.get("page_size") {
        None | Some(Value::Null) => None,
        Some(v) => match v.as_u64() {
            Some(n) if n >= 1 => Some(n),
            _ => return Err("page_size must be null or an integer >= 1".into()),
        },
    };
    let mirror = opt_bool(o, "mirror", true)?;
    let sources = match member(o, "sources")? {
        None => vec![],
        Some(v) => v.as_array().ok_or("sources must be an array")?.iter().map(parse_source).collect::<Result<_, _>>()?,
    };
    let entries = match member(o, "entries")? {
        None => vec![],
        Some(v) => v.as_array().ok_or("entries must be an array")?.iter().map(parse_entry).collect::<Result<_, _>>()?,
    };
    Ok(Spec { page_size, mirror, sources, entries })
}

fn cmd_write(desc: &str, out: &str) -> Result<(), String> {
    let text = std::fs::read(desc).map_err(|e| format!("cannot read description: {e}"))?;
    let v: Value = serde_json::from_slice(&text).map_err(|e| format!("invalid description JSON: {e}"))?;
    let spec = parse_description(&v)?;
    let bytes = writer::write_archive(&spec)?;
    std::fs::write(out, bytes).map_err(|e| format!("cannot write {out}: {e}"))?;
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let res = match args.get(1).map(String::as_str) {
        Some("read") if args.len() == 4 => cmd_read(&args[2], &args[3]),
        Some("write") if args.len() == 4 => cmd_write(&args[2], &args[3]),
        _ => Err("usage: vzip read <archive> <queries> | vzip write <description> <out>".into()),
    };
    match res {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("vzip: {e}");
            ExitCode::from(2)
        }
    }
}
