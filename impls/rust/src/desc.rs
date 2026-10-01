//! JSON formats of the conformance harness (HARNESS.md): write descriptions
//! and read queries.

use crate::proto::{Range, Source, SourceKind};
use crate::reader::Request;
use crate::writer::{Value as WValue, WEntry, WriteSpec};
use serde_json::{Map, Value};

type R<T> = Result<T, String>;

const LIMIT: u64 = 1 << 53;

pub fn hex_decode(s: &str) -> R<Vec<u8>> {
    if s.len() % 2 != 0 {
        return Err(format!("hex string {s:?} has odd length"));
    }
    let digit = |c: u8| -> R<u8> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            _ => Err(format!("invalid lowercase hex string {s:?}")),
        }
    };
    s.as_bytes()
        .chunks(2)
        .map(|p| Ok(digit(p[0])? * 16 + digit(p[1])?))
        .collect()
}

pub fn hex_encode(b: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(b.len() * 2);
    for &c in b {
        s.push(H[(c >> 4) as usize] as char);
        s.push(H[(c & 15) as usize] as char);
    }
    s
}

/// Get a known member; `null` is invalid unless `allow_null`.
fn member<'a>(o: &'a Map<String, Value>, k: &str, allow_null: bool) -> R<Option<&'a Value>> {
    match o.get(k) {
        None => Ok(None),
        Some(Value::Null) if !allow_null => Err(format!("member {k:?} must not be null")),
        Some(Value::Null) => Ok(None),
        Some(v) => Ok(Some(v)),
    }
}

fn as_uint(v: &Value, what: &str) -> R<u64> {
    match v.as_u64() {
        Some(n) if v.is_u64() && n < LIMIT => Ok(n),
        _ => Err(format!("{what} must be a non-negative integer below 2^53")),
    }
}

fn as_int(v: &Value, what: &str) -> R<i64> {
    if v.is_i64() || v.is_u64() {
        if let Some(n) = v.as_i64() {
            if n.unsigned_abs() < LIMIT {
                return Ok(n);
            }
        }
    }
    Err(format!("{what} must be an integer of magnitude below 2^53"))
}

fn as_bool(v: &Value, what: &str) -> R<bool> {
    v.as_bool().ok_or_else(|| format!("{what} must be a boolean"))
}

fn as_str<'a>(v: &'a Value, what: &str) -> R<&'a str> {
    v.as_str().ok_or_else(|| format!("{what} must be a string"))
}

fn as_obj<'a>(v: &'a Value, what: &str) -> R<&'a Map<String, Value>> {
    v.as_object().ok_or_else(|| format!("{what} must be an object"))
}

fn as_arr<'a>(v: &'a Value, what: &str) -> R<&'a Vec<Value>> {
    v.as_array().ok_or_else(|| format!("{what} must be an array"))
}

pub fn parse_description(text: &str) -> R<WriteSpec> {
    let v: Value = serde_json::from_str(text).map_err(|e| format!("invalid JSON: {e}"))?;
    let o = as_obj(&v, "description")?;
    let mut spec = WriteSpec::default();
    if let Some(ps) = member(o, "page_size", true)? {
        let n = as_uint(ps, "page_size")?;
        if n < 1 {
            return Err("page_size must be at least 1".into());
        }
        spec.page_size = Some(n);
    }
    if let Some(m) = member(o, "mirror", false)? {
        spec.mirror = as_bool(m, "mirror")?;
    }
    if let Some(s) = member(o, "sources", false)? {
        for (i, sv) in as_arr(s, "sources")?.iter().enumerate() {
            let so = as_obj(sv, "source")?;
            let url = member(so, "url", false)?;
            let key = member(so, "key", false)?;
            let data = member(so, "data", false)?;
            let count = url.is_some() as u8 + key.is_some() as u8 + data.is_some() as u8;
            if count != 1 {
                return Err(format!("source {i} must have exactly one of url, key, data"));
            }
            let kind = if let Some(u) = url {
                SourceKind::Url(as_str(u, "url")?.to_string())
            } else if let Some(k) = key {
                SourceKind::Key(as_str(k, "key")?.to_string())
            } else {
                SourceKind::Data(hex_decode(as_str(data.unwrap(), "data")?)?)
            };
            let size = member(so, "size", false)?.map(|v| as_uint(v, "size")).transpose()?;
            let etag = member(so, "etag", false)?.map(|v| as_str(v, "etag").map(String::from)).transpose()?;
            let mna = member(so, "modified_not_after", false)?
                .map(|v| as_int(v, "modified_not_after"))
                .transpose()?;
            spec.sources.push(Source { kind: Some(kind), size, etag, modified_not_after: mna });
        }
    }
    if let Some(es) = member(o, "entries", false)? {
        for ev in as_arr(es, "entries")? {
            let eo = as_obj(ev, "entry")?;
            let key = as_str(member(eo, "key", false)?.ok_or("entry without key")?, "key")?.to_string();
            let compress = member(eo, "compress", false)?.map(|v| as_bool(v, "compress")).transpose()?.unwrap_or(false);
            let pinned = member(eo, "pinned", false)?.map(|v| as_bool(v, "pinned")).transpose()?.unwrap_or(false);
            let bytes = member(eo, "bytes", false)?;
            let ranges = member(eo, "ranges", false)?;
            let value = match (bytes, ranges) {
                (Some(b), None) => WValue::Bytes { data: hex_decode(as_str(b, "bytes")?)?, compress },
                (None, Some(r)) => {
                    if compress {
                        return Err(format!("entry {key:?}: compress is only allowed on bytes entries"));
                    }
                    if pinned {
                        return Err(format!("entry {key:?}: only bytes entries may be pinned"));
                    }
                    let mut parts = Vec::new();
                    for rv in as_arr(r, "ranges")? {
                        let ro = as_obj(rv, "range")?;
                        let data = member(ro, "data", false)?;
                        let src = member(ro, "source", false)?;
                        let off = member(ro, "offset", false)?;
                        let len = member(ro, "length", false)?;
                        if let Some(d) = data {
                            if src.is_some() || off.is_some() || len.is_some() {
                                return Err("a range mixes data with source/offset/length".into());
                            }
                            parts.push(Range { data: Some(hex_decode(as_str(d, "data")?)?), ..Default::default() });
                        } else {
                            let source = src.map(|v| as_uint(v, "source")).transpose()?.unwrap_or(0);
                            if source > u32::MAX as u64 {
                                return Err("source index exceeds 2^32-1".into());
                            }
                            parts.push(Range {
                                source: source as u32,
                                offset: off.map(|v| as_uint(v, "offset")).transpose()?.unwrap_or(0),
                                length: len.map(|v| as_uint(v, "length")).transpose()?.unwrap_or(0),
                                data: None,
                            });
                        }
                    }
                    WValue::Ranges(parts)
                }
                _ => return Err(format!("entry {key:?} must have exactly one of bytes, ranges")),
            };
            spec.entries.push(WEntry { key, value, pinned });
        }
    }
    Ok(spec)
}

#[derive(Debug, Clone)]
pub enum Query {
    Classify(String),
    Get(String, Request),
    GetRaw(String),
    List(String),
}

pub fn parse_queries(text: &str) -> R<Vec<Query>> {
    let v: Value = serde_json::from_str(text).map_err(|e| format!("invalid JSON: {e}"))?;
    let arr = as_arr(&v, "queries")?;
    let mut out = Vec::new();
    for q in arr {
        let o = as_obj(q, "query")?;
        let op = as_str(o.get("op").ok_or("query without op")?, "op")?;
        let key = || -> R<String> { Ok(as_str(o.get("key").ok_or("query without key")?, "key")?.to_string()) };
        out.push(match op {
            "classify" => Query::Classify(key()?),
            "get" => {
                let req = match o.get("range") {
                    None => Request::Whole,
                    Some(r) => {
                        let ro = as_obj(r, "range")?;
                        let has = |k: &str| ro.contains_key(k);
                        let forms = (has("start") || has("end")) as u8 + has("offset") as u8 + has("suffix") as u8;
                        if forms != 1 {
                            return Err("range must have exactly one form".into());
                        }
                        if has("offset") {
                            Request::Offset(as_uint(&ro["offset"], "offset")?)
                        } else if has("suffix") {
                            Request::Suffix(as_uint(&ro["suffix"], "suffix")?)
                        } else {
                            let s = as_uint(ro.get("start").ok_or("range without start")?, "start")?;
                            let e = as_uint(ro.get("end").ok_or("range without end")?, "end")?;
                            Request::Range(s, e)
                        }
                    }
                };
                Query::Get(key()?, req)
            }
            "get_raw" => {
                if o.contains_key("range") {
                    return Err("get_raw does not take a range".into());
                }
                Query::GetRaw(key()?)
            }
            "list" => Query::List(as_str(o.get("prefix").ok_or("list without prefix")?, "prefix")?.to_string()),
            other => return Err(format!("unknown op {other:?}")),
        });
    }
    Ok(out)
}
