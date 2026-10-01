//! Conformance harness CLI (HARNESS.md).

use serde_json::{json, Value};
use std::io::Write;
use std::path::Path;
use std::process::ExitCode;
use vzip::desc::{hex_encode, parse_description, parse_queries, Query};
use vzip::{Archive, VzError};

fn err_json(e: &VzError) -> Value {
    json!({"ok": false, "class": e.class.as_str(), "error": e.message})
}

fn value_json(r: Result<Option<Vec<u8>>, VzError>) -> Value {
    match r {
        Ok(Some(b)) => json!({"ok": true, "value": hex_encode(&b)}),
        Ok(None) => json!({"ok": true, "value": null}),
        Err(e) => err_json(&e),
    }
}

fn cmd_read(archive: &str, queries: &str) -> ExitCode {
    let text = match std::fs::read_to_string(queries) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("cannot read queries file: {e}");
            return ExitCode::from(2);
        }
    };
    let qs = match parse_queries(&text) {
        Ok(q) => q,
        Err(e) => {
            eprintln!("invalid queries file: {e}");
            return ExitCode::from(2);
        }
    };
    let out = match Archive::open_path(Path::new(archive)) {
        Err(e) => json!({"open": {"ok": false, "class": "archive", "error": e.message}, "results": []}),
        Ok(a) => {
            let results: Vec<Value> = qs
                .iter()
                .map(|q| match q {
                    Query::Classify(k) => match a.classify(k) {
                        Ok(kind) => json!({"ok": true, "kind": kind.as_str()}),
                        Err(e) => err_json(&e),
                    },
                    Query::Get(k, r) => value_json(a.get(k, *r)),
                    Query::GetRaw(k) => value_json(a.raw(k)),
                    Query::List(p) => match a.list(p) {
                        Ok(keys) => json!({"ok": true, "keys": keys}),
                        Err(e) => err_json(&e),
                    },
                })
                .collect();
            json!({"open": {"ok": true}, "results": results})
        }
    };
    let mut stdout = std::io::stdout().lock();
    let _ = writeln!(stdout, "{}", out);
    ExitCode::SUCCESS
}

fn cmd_write(desc: &str, out_path: &str) -> ExitCode {
    let text = match std::fs::read_to_string(desc) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("cannot read description: {e}");
            return ExitCode::from(2);
        }
    };
    let spec = match parse_description(&text) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("invalid description: {e}");
            return ExitCode::from(1);
        }
    };
    let bytes = match vzip::write_archive(&spec) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("invalid description: {e}");
            return ExitCode::from(1);
        }
    };
    let res = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(out_path)
        .and_then(|mut f| f.write_all(&bytes));
    if let Err(e) = res {
        eprintln!("cannot write {out_path}: {e}");
        let _ = std::fs::remove_file(out_path);
        return ExitCode::from(2);
    }
    ExitCode::SUCCESS
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("read") if args.len() == 4 => cmd_read(&args[2], &args[3]),
        Some("write") if args.len() == 4 => cmd_write(&args[2], &args[3]),
        _ => {
            eprintln!("usage: vzip read <archive> <queries.json> | vzip write <description.json> <out.vzip>");
            ExitCode::from(2)
        }
    }
}
