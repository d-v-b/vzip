//! JSON value model (also used for decoded LV structures) and serializer.

#[derive(Clone, Debug)]
pub enum Value {
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    List(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

impl Value {
    pub fn get(&self, name: &str) -> Option<&Value> {
        match self {
            Value::Obj(m) => m.iter().find(|(k, _)| k == name).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn path(&self, p: &str) -> Option<&Value> {
        let mut v = self;
        for part in p.split('/') {
            v = v.get(part)?;
        }
        Some(v)
    }
    /// The members of a level or list, in order.
    pub fn members(&self) -> Vec<&Value> {
        match self {
            Value::Obj(m) => m.iter().map(|(_, v)| v).collect(),
            Value::List(l) => l.iter().collect(),
            _ => Vec::new(),
        }
    }
    pub fn as_int(&self) -> Option<i128> {
        match self {
            Value::Int(i) => Some(*i),
            Value::Bool(b) => Some(*b as i128),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Float(f) => Some(*f),
            Value::Int(i) => Some(*i as f64),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn truthy(&self) -> bool {
        match self {
            Value::Bool(b) => *b,
            Value::Int(i) => *i != 0,
            Value::Float(f) => *f != 0.0,
            _ => false,
        }
    }
}

pub fn obj(members: Vec<(&str, Value)>) -> Value {
    Value::Obj(members.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}
pub fn s(x: &str) -> Value {
    Value::Str(x.to_string())
}
pub fn int<T: Into<i128>>(x: T) -> Value {
    Value::Int(x.into())
}

pub fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
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

pub fn write(out: &mut String, v: &Value) {
    match v {
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => out.push_str(&i.to_string()),
        Value::Float(f) => {
            assert!(f.is_finite(), "non-finite number in JSON output");
            // Debug formatting is shortest round-trip and always valid JSON
            // for finite values (e.g. "1.0", "1e-7", "-0.0").
            out.push_str(&format!("{f:?}"));
        }
        Value::Str(s) => write_str(out, s),
        Value::List(l) => {
            out.push('[');
            for (i, x) in l.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(out, x);
            }
            out.push(']');
        }
        Value::Obj(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(out, k);
                out.push(':');
                write(out, x);
            }
            out.push('}');
        }
    }
}

pub fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let b = [c[0], *c.get(1).unwrap_or(&0), *c.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if c.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if c.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    out
}
