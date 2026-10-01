//! HARNESS.md command-line interface.

use std::process::ExitCode;
use vzip::json::{self, Value};
use vzip::{Archive, Error, Kind, Request};

enum Query {
    Classify(String),
    Get(String, Request),
    Raw(String),
    List(String),
}

const MAX_NUM: i128 = (1i128 << 53) - 1;

fn num(v: &Value, what: &str) -> Result<u64, String> {
    match v.as_int() {
        Some(n) if (0..=MAX_NUM).contains(&n) => Ok(n as u64),
        _ => Err(format!("{what} must be a non-negative integer below 2^53")),
    }
}

fn parse_query(q: &Value) -> Result<Query, String> {
    if !matches!(q, Value::Object(_)) {
        return Err("query must be an object".into());
    }
    let op = match q.get("op") {
        Some(Value::String(s)) => s.as_str(),
        _ => return Err("query has no string op".into()),
    };
    let key = || match q.get("key") {
        Some(Value::String(s)) => Ok(s.clone()),
        Some(_) => Err("key must be a string".to_string()),
        None => Err("query has no key".to_string()),
    };
    let range = q.get("range");
    if range.is_some() && op != "get" {
        return Err(format!("range is not allowed on {op}"));
    }
    match op {
        "classify" => Ok(Query::Classify(key()?)),
        "get_raw" => Ok(Query::Raw(key()?)),
        "list" => match q.get("prefix") {
            Some(Value::String(s)) => Ok(Query::List(s.clone())),
            Some(_) => Err("prefix must be a string".into()),
            None => Err("list query has no prefix".into()),
        },
        "get" => {
            let k = key()?;
            let req = match range {
                None => Request::Whole,
                Some(r @ Value::Object(_)) => {
                    let (s, e, o, x) = (r.get("start"), r.get("end"), r.get("offset"), r.get("suffix"));
                    match (s, e, o, x) {
                        (Some(s), Some(e), None, None) => Request::Range(num(s, "start")?, num(e, "end")?),
                        (None, None, Some(o), None) => Request::Offset(num(o, "offset")?),
                        (None, None, None, Some(x)) => Request::Suffix(num(x, "suffix")?),
                        _ => return Err("range must have exactly one form".into()),
                    }
                }
                Some(_) => return Err("range must be an object".into()),
            };
            Ok(Query::Get(k, req))
        }
        other => Err(format!("unknown op {other:?}")),
    }
}

fn obj(m: Vec<(&str, Value)>) -> Value {
    Value::Object(m.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn err_value(e: &Error) -> Value {
    obj(vec![
        ("ok", Value::Bool(false)),
        ("class", Value::String(e.class.name().into())),
        ("error", Value::String(e.msg.clone())),
    ])
}

fn bytes_value(r: Result<Option<Vec<u8>>, Error>) -> Value {
    match r {
        Ok(v) => obj(vec![
            ("ok", Value::Bool(true)),
            ("value", v.map(|b| Value::String(json::hex(&b))).unwrap_or(Value::Null)),
        ]),
        Err(e) => err_value(&e),
    }
}

fn cmd_read(archive: &str, queries: &str) -> Result<(), String> {
    let qb = std::fs::read(queries).map_err(|e| format!("cannot read queries: {e}"))?;
    let qv = json::parse(&qb).map_err(|e| format!("invalid queries file: {e}"))?;
    let Value::Array(qs) = qv else { return Err("queries file must be a JSON array".into()) };
    let qs: Vec<Query> = qs.iter().map(parse_query).collect::<Result<_, _>>().map_err(|e| format!("invalid query: {e}"))?;
    let out = match Archive::open(archive) {
        Err(e) => obj(vec![
            ("open", obj(vec![
                ("ok", Value::Bool(false)),
                ("class", Value::String("archive".into())),
                ("error", Value::String(e.msg)),
            ])),
            ("results", Value::Array(vec![])),
        ]),
        Ok(a) => {
            let results = qs
                .iter()
                .map(|q| match q {
                    Query::Classify(k) => match a.classify(k) {
                        Ok(kind) => obj(vec![
                            ("ok", Value::Bool(true)),
                            ("kind", Value::String(match kind {
                                Kind::Bytes => "bytes",
                                Kind::Reference => "reference",
                                Kind::Missing => "missing",
                            }.into())),
                        ]),
                        Err(e) => err_value(&e),
                    },
                    Query::Get(k, r) => bytes_value(a.get(k, *r)),
                    Query::Raw(k) => bytes_value(a.raw(k)),
                    Query::List(p) => match a.list(p) {
                        Ok(keys) => obj(vec![
                            ("ok", Value::Bool(true)),
                            ("keys", Value::Array(keys.into_iter().map(Value::String).collect())),
                        ]),
                        Err(e) => err_value(&e),
                    },
                })
                .collect();
            obj(vec![("open", obj(vec![("ok", Value::Bool(true))])), ("results", Value::Array(results))])
        }
    };
    println!("{}", json::to_string(&out));
    Ok(())
}

fn cmd_write(desc: &str, out: &str) -> Result<(), String> {
    let db = std::fs::read(desc).map_err(|e| format!("cannot read description: {e}"))?;
    let spec = vzip::writer::parse_description(&db).map_err(|e| format!("invalid description: {e}"))?;
    // Write to a temporary sibling, then rename, so no file appears on failure.
    let tmp = format!("{out}.vzip-tmp-{}", std::process::id());
    let f = std::fs::File::create(&tmp).map_err(|e| format!("cannot create {tmp}: {e}"))?;
    let r = vzip::writer::write_archive(&spec, std::io::BufWriter::new(f));
    if let Err(e) = r {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!("invalid description: {e}"));
    }
    std::fs::rename(&tmp, out).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("cannot rename to {out}: {e}")
    })
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let r = match args.get(1).map(|s| s.as_str()) {
        Some("read") if args.len() == 4 => cmd_read(&args[2], &args[3]),
        Some("write") if args.len() == 4 => cmd_write(&args[2], &args[3]),
        _ => Err("usage: vzip read <archive> <queries> | vzip write <description> <out>".into()),
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("vzip: {e}");
            ExitCode::FAILURE
        }
    }
}
