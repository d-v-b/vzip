//! Minimal strict JSON parser/serializer.
//!
//! Differences from a typical parser, required by HARNESS.md:
//! - objects with duplicate member names are rejected;
//! - numbers keep their textual form so integers and non-integers
//!   (`1.0`, `1e2`) can be told apart;
//! - strings must decode to valid Unicode (lone surrogates rejected).

use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    /// Raw number text.
    Number(String),
    String(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}

impl Value {
    pub fn get(&self, k: &str) -> Option<&Value> {
        match self {
            Value::Object(m) => m.iter().find(|(n, _)| n == k).map(|(_, v)| v),
            _ => None,
        }
    }
    /// The value as an integer, if it is a JSON number written as an integer
    /// (no fraction, no exponent).
    pub fn as_int(&self) -> Option<i128> {
        match self {
            Value::Number(s) => {
                if s.contains(['.', 'e', 'E']) {
                    return None;
                }
                s.parse::<i128>().ok()
            }
            _ => None,
        }
    }
}

pub fn parse(input: &[u8]) -> Result<Value, String> {
    let s = std::str::from_utf8(input).map_err(|_| "JSON is not valid UTF-8".to_string())?;
    let mut p = Parser { s: s.as_bytes(), i: 0, depth: 0 };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return Err(format!("trailing data at byte {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }
    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }
    fn expect(&mut self, lit: &str) -> Result<(), String> {
        if self.s[self.i..].starts_with(lit.as_bytes()) {
            self.i += lit.len();
            Ok(())
        } else {
            Err(format!("unexpected token at byte {}", self.i))
        }
    }
    fn value(&mut self) -> Result<Value, String> {
        match self.peek() {
            None => Err("unexpected end of JSON".into()),
            Some(b'n') => self.expect("null").map(|_| Value::Null),
            Some(b't') => self.expect("true").map(|_| Value::Bool(true)),
            Some(b'f') => self.expect("false").map(|_| Value::Bool(false)),
            Some(b'"') => self.string().map(Value::String),
            Some(b'[') => {
                self.enter()?;
                self.i += 1;
                let mut v = Vec::new();
                self.ws();
                if self.peek() == Some(b']') {
                    self.i += 1;
                    self.depth -= 1;
                    return Ok(Value::Array(v));
                }
                loop {
                    self.ws();
                    v.push(self.value()?);
                    self.ws();
                    match self.peek() {
                        Some(b',') => self.i += 1,
                        Some(b']') => {
                            self.i += 1;
                            break;
                        }
                        _ => return Err(format!("expected , or ] at byte {}", self.i)),
                    }
                }
                self.depth -= 1;
                Ok(Value::Array(v))
            }
            Some(b'{') => {
                self.enter()?;
                self.i += 1;
                let mut m: Vec<(String, Value)> = Vec::new();
                self.ws();
                if self.peek() == Some(b'}') {
                    self.i += 1;
                    self.depth -= 1;
                    return Ok(Value::Object(m));
                }
                loop {
                    self.ws();
                    if self.peek() != Some(b'"') {
                        return Err(format!("expected member name at byte {}", self.i));
                    }
                    let k = self.string()?;
                    self.ws();
                    if self.peek() != Some(b':') {
                        return Err(format!("expected : at byte {}", self.i));
                    }
                    self.i += 1;
                    self.ws();
                    let v = self.value()?;
                    if m.iter().any(|(n, _)| *n == k) {
                        return Err(format!("duplicate member name {k:?}"));
                    }
                    m.push((k, v));
                    self.ws();
                    match self.peek() {
                        Some(b',') => self.i += 1,
                        Some(b'}') => {
                            self.i += 1;
                            break;
                        }
                        _ => return Err(format!("expected , or }} at byte {}", self.i)),
                    }
                }
                self.depth -= 1;
                Ok(Value::Object(m))
            }
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => Err(format!("unexpected character at byte {}", self.i)),
        }
    }
    fn enter(&mut self) -> Result<(), String> {
        self.depth += 1;
        if self.depth > 512 {
            return Err("JSON nested too deeply".into());
        }
        Ok(())
    }
    fn number(&mut self) -> Result<Value, String> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        match self.peek() {
            Some(b'0') => self.i += 1,
            Some(c) if c.is_ascii_digit() => {
                while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                    self.i += 1;
                }
            }
            _ => return Err(format!("bad number at byte {start}")),
        }
        if self.peek() == Some(b'.') {
            self.i += 1;
            let d = self.i;
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.i += 1;
            }
            if d == self.i {
                return Err(format!("bad number at byte {start}"));
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.i += 1;
            }
            let d = self.i;
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.i += 1;
            }
            if d == self.i {
                return Err(format!("bad number at byte {start}"));
            }
        }
        Ok(Value::Number(String::from_utf8(self.s[start..self.i].to_vec()).unwrap()))
    }
    fn hex4(&mut self) -> Result<u32, String> {
        if self.i + 4 > self.s.len() {
            return Err("truncated \\u escape".into());
        }
        let h = std::str::from_utf8(&self.s[self.i..self.i + 4]).map_err(|_| "bad \\u escape")?;
        let v = u32::from_str_radix(h, 16).map_err(|_| "bad \\u escape")?;
        if !h.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err("bad \\u escape".into());
        }
        self.i += 4;
        Ok(v)
    }
    fn string(&mut self) -> Result<String, String> {
        self.i += 1; // opening quote
        let mut out = String::new();
        loop {
            let c = self.peek().ok_or("unterminated string")?;
            match c {
                b'"' => {
                    self.i += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.i += 1;
                    let e = self.peek().ok_or("unterminated string")?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let u = self.hex4()?;
                            if (0xD800..0xDC00).contains(&u) {
                                if !self.s[self.i..].starts_with(b"\\u") {
                                    return Err("lone surrogate in string".into());
                                }
                                self.i += 2;
                                let l = self.hex4()?;
                                if !(0xDC00..0xE000).contains(&l) {
                                    return Err("lone surrogate in string".into());
                                }
                                let cp = 0x10000 + ((u - 0xD800) << 10) + (l - 0xDC00);
                                out.push(char::from_u32(cp).unwrap());
                            } else if (0xDC00..0xE000).contains(&u) {
                                return Err("lone surrogate in string".into());
                            } else {
                                out.push(char::from_u32(u).unwrap());
                            }
                        }
                        _ => return Err("bad escape".into()),
                    }
                }
                c if c < 0x20 => return Err("control character in string".into()),
                _ => {
                    // copy one UTF-8 char (input already validated as UTF-8)
                    let rest = std::str::from_utf8(&self.s[self.i..]).unwrap();
                    let ch = rest.chars().next().unwrap();
                    out.push(ch);
                    self.i += ch.len_utf8();
                }
            }
        }
    }
}

pub fn escape_into(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn to_string(v: &Value) -> String {
    let mut s = String::new();
    write_value(&mut s, v);
    s
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(n),
        Value::String(s) => escape_into(out, s),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(out, x);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                escape_into(out, k);
                out.push_str(": ");
                write_value(out, x);
            }
            out.push('}');
        }
    }
}

pub fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        let _ = write!(s, "{x:02x}");
    }
    s
}

/// Strict lowercase, even-length hex.
pub fn unhex(s: &str) -> Option<Vec<u8>> {
    let b = s.as_bytes();
    if b.len() % 2 != 0 {
        return None;
    }
    let d = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        _ => None,
    };
    b.chunks(2).map(|p| Some(d(p[0])? << 4 | d(p[1])?)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses() {
        let v = parse(r#"{"a": [1, 2.0, "x\u00e9\ud83d\ude00"], "b": null}"#.as_bytes()).unwrap();
        assert_eq!(v.get("a").unwrap(), &Value::Array(vec![
            Value::Number("1".into()), Value::Number("2.0".into()), Value::String("xé😀".into())]));
        assert_eq!(Value::Number("2.0".into()).as_int(), None);
        assert_eq!(Value::Number("-7".into()).as_int(), Some(-7));
    }
    #[test]
    fn rejects_duplicates() {
        assert!(parse(br#"{"a":1,"a":2}"#).is_err());
    }
    #[test]
    fn rejects_lone_surrogate() {
        assert!(parse(br#""\ud800""#).is_err());
    }
    #[test]
    fn hex_strict() {
        assert_eq!(unhex("00ff"), Some(vec![0, 255]));
        assert_eq!(unhex("00FF"), None);
        assert_eq!(unhex("0"), None);
    }
}
