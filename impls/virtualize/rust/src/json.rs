/// Minimal JSON value with deterministic member order.
#[derive(Clone)]
pub enum J {
    Bool(bool),
    Int(i64),
    UInt(u64),
    Num(f64),
    Str(String),
    Arr(Vec<J>),
    Obj(Vec<(String, J)>),
}

impl J {
    pub fn s(v: &str) -> J {
        J::Str(v.to_string())
    }
    pub fn write(&self, out: &mut String) {
        match self {
            J::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            J::Int(i) => out.push_str(&i.to_string()),
            J::UInt(i) => out.push_str(&i.to_string()),
            J::Num(f) => {
                assert!(f.is_finite());
                // Rust's Display for f64 is the shortest string that round-trips.
                out.push_str(&format!("{f}"));
            }
            J::Str(s) => write_str(out, s),
            J::Arr(a) => {
                out.push('[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    v.write(out);
                }
                out.push(']');
            }
            J::Obj(m) => {
                out.push('{');
                for (i, (k, v)) in m.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_str(out, k);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

pub fn obj(m: Vec<(&str, J)>) -> J {
    J::Obj(m.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}
