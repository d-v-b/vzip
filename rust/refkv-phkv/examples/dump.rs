//! Prints every key of a phkv store (standalone or hybrid) as JSON, `{key: [kind, crc32c of the
//! value]}`, resolving `file:` sources (relative ones against the store's own location) and checking
//! their size pins: `cargo run --release --example dump -- store.phkv`. A test aid, not a reader
//! policy (spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>).
use refkv_phkv::{Kind, Request, Resolver, Store};
use refkv_vzip::pb::Source;

struct LocalFiles(String);

impl Resolver for LocalFiles {
    fn read(&self, src: &Source, offset: u64, length: u64) -> Result<Vec<u8>, String> {
        let url = refkv_vzip::uri::resolve(&self.0, src.url_str().unwrap())?;
        let path = url.strip_prefix("file://").ok_or_else(|| format!("not a file: URL: {url}"))?;
        let path = percent_decode(path);
        let f = std::fs::read(&path).map_err(|e| format!("{path}: {e}"))?;
        if let Some(n) = src.size
            && f.len() as u64 != n
        {
            return Err(format!("{path}: size pin {n} fails"));
        }
        f.get(offset as usize..(offset + length) as usize).map(|b| b.to_vec()).ok_or_else(|| format!("{path}: too short"))
    }
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let (mut out, mut i) = (Vec::new(), 0);
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            out.push(u8::from_str_radix(&s[i + 1..i + 3], 16).unwrap());
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(out).unwrap()
}

fn main() {
    let path = std::env::args().nth(1).expect("a store");
    let base = refkv_vzip::uri::file_uri(std::path::Path::new(&path)).unwrap();
    let s = Store::open(std::fs::File::open(&path).unwrap()).unwrap();
    let r = LocalFiles(base);
    let mut out = Vec::new();
    for k in s.list("").unwrap() {
        let kind = match s.classify(&k).unwrap() {
            Some(Kind::Bytes) => "bytes",
            Some(Kind::Reference) => "reference",
            None => "missing",
        };
        let v = match s.get(&k, Request::Whole, &r) {
            Ok(Some(v)) => format!("{}", crc32c::crc32c(&v)),
            Ok(None) => "missing".into(),
            Err(e) => format!("{:?}", e.class),
        };
        out.push(format!("{}: [\"{kind}\", \"{v}\"]", serde_json_string(&k)));
    }
    println!("{{{}}}", out.join(", "));
}

fn serde_json_string(s: &str) -> String {
    let mut o = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}
