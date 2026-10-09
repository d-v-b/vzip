//! The schema's expressions: arithmetic, comparisons, `&&`, `||`, `!`, `?:`, `??`
//! (the first operand that is present), dotted paths (a path through a list maps
//! over it), list literals `[a, b]`, string literals `'text'`, and a few functions
//! (abs, finite, sum, same, len; in, distinct, first, at, min, max, present).
//! Numbers are binary64; a comparison with an absent operand is false, arithmetic
//! with one is absent; `==` and `!=` compare strings and lists as values.

use std::collections::HashMap;

#[derive(Clone, Debug, PartialEq)]
pub enum V {
    Absent,
    Num(f64),
    Bool(bool),
    Str(String),
    List(Vec<V>),
    Obj(Vec<(String, V)>),
}

impl V {
    pub fn get(&self, k: &str) -> V {
        match self {
            V::Obj(m) => m
                .iter()
                .find(|(n, _)| n == k)
                .map(|(_, v)| v.clone())
                .unwrap_or(V::Absent),
            V::List(items) => V::List(items.iter().map(|x| x.get(k)).collect()),
            _ => V::Absent,
        }
    }
    pub fn num(&self) -> Option<f64> {
        match self {
            V::Num(x) => Some(*x),
            V::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
            _ => None,
        }
    }
    pub fn truthy(&self) -> bool {
        match self {
            V::Bool(b) => *b,
            V::Num(x) => *x != 0.0,
            V::Absent => false,
            _ => true,
        }
    }
    pub fn to_json(&self) -> serde_json::Value {
        use serde_json::Value as J;
        match self {
            V::Absent => J::Null,
            V::Num(x) => serde_json::Number::from_f64(*x)
                .map(J::Number)
                .unwrap_or(J::Null),
            V::Bool(b) => J::Bool(*b),
            V::Str(s) => J::String(s.clone()),
            V::List(v) => J::Array(v.iter().map(|x| x.to_json()).collect()),
            V::Obj(m) => J::Object(m.iter().map(|(k, v)| (k.clone(), v.to_json())).collect()),
        }
    }
    pub fn from_json(j: &serde_json::Value) -> V {
        use serde_json::Value as J;
        match j {
            J::Null => V::Absent,
            J::Bool(b) => V::Bool(*b),
            J::Number(n) => V::Num(n.as_f64().unwrap_or(f64::NAN)),
            J::String(s) => V::Str(s.clone()),
            J::Array(a) => V::List(a.iter().map(V::from_json).collect()),
            J::Object(o) => V::Obj(
                o.iter()
                    .map(|(k, v)| (k.clone(), V::from_json(v)))
                    .collect(),
            ),
        }
    }
}

#[derive(Clone, Debug)]
pub enum E {
    Num(f64),
    Path(Vec<String>),
    Call(String, Vec<E>),
    Not(Box<E>),
    Neg(Box<E>),
    Bin(String, Box<E>, Box<E>),
    If(Box<E>, Box<E>, Box<E>),
    List(Vec<E>),
    Str(String),
    Bool(bool),
}

fn lex(s: &str) -> Result<Vec<String>, String> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if c.is_ascii_whitespace() {
            i += 1;
            continue;
        }
        let two = if i + 1 < b.len() { &s[i..i + 2] } else { "" };
        if ["&&", "||", "==", "!=", "<=", ">=", "??"].contains(&two) {
            out.push(two.to_string());
            i += 2;
        } else if c.is_ascii_digit() {
            let j = i;
            while i < b.len() && (b[i].is_ascii_digit() || b[i] == b'.' || b[i] == b'e') {
                i += 1;
            }
            out.push(s[j..i].to_string());
        } else if c.is_ascii_alphabetic() || c == b'_' {
            let j = i;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_' || b[i] == b'.') {
                i += 1;
            }
            out.push(s[j..i].to_string());
        } else if c == b'\'' {
            let j = i + 1;
            i = j;
            while i < b.len() && b[i] != b'\'' {
                i += 1;
            }
            if i >= b.len() {
                return Err(format!("unterminated string in {s:?}"));
            }
            out.push(format!("'{}", &s[j..i]));
            i += 1;
        } else if b"+-*/<>!?:(),[]".contains(&c) {
            out.push((c as char).to_string());
            i += 1;
        } else {
            return Err(format!("bad expression {s:?}"));
        }
    }
    Ok(out)
}

struct Parser {
    t: Vec<String>,
    p: usize,
}

impl Parser {
    fn peek(&self) -> Option<&str> {
        self.t.get(self.p).map(|x| x.as_str())
    }
    fn next(&mut self) -> Result<String, String> {
        self.p += 1;
        self.t
            .get(self.p - 1)
            .cloned()
            .ok_or_else(|| "unexpected end of expression".to_string())
    }
    fn expr(&mut self) -> Result<E, String> {
        let c = self.coalesce()?;
        if self.peek() == Some("?") {
            self.next()?;
            let a = self.expr()?;
            if self.next()? != ":" {
                return Err("expected ':'".into());
            }
            let b = self.expr()?;
            return Ok(E::If(Box::new(c), Box::new(a), Box::new(b)));
        }
        Ok(c)
    }
    fn binary(
        &mut self,
        ops: &[&str],
        next: fn(&mut Parser) -> Result<E, String>,
    ) -> Result<E, String> {
        let mut l = next(self)?;
        while let Some(op) = self
            .peek()
            .filter(|o| ops.contains(o))
            .map(|o| o.to_string())
        {
            self.next()?;
            let r = next(self)?;
            l = E::Bin(op, Box::new(l), Box::new(r));
        }
        Ok(l)
    }
    fn coalesce(&mut self) -> Result<E, String> {
        self.binary(&["??"], Parser::or)
    }
    fn or(&mut self) -> Result<E, String> {
        self.binary(&["||"], Parser::and)
    }
    fn and(&mut self) -> Result<E, String> {
        self.binary(&["&&"], Parser::cmp)
    }
    fn cmp(&mut self) -> Result<E, String> {
        self.binary(&["==", "!=", "<", "<=", ">", ">="], Parser::add)
    }
    fn add(&mut self) -> Result<E, String> {
        self.binary(&["+", "-"], Parser::mul)
    }
    fn mul(&mut self) -> Result<E, String> {
        self.binary(&["*", "/"], Parser::unary)
    }
    fn unary(&mut self) -> Result<E, String> {
        match self.peek() {
            Some("!") => {
                self.next()?;
                Ok(E::Not(Box::new(self.unary()?)))
            }
            Some("-") => {
                self.next()?;
                Ok(E::Neg(Box::new(self.unary()?)))
            }
            _ => self.primary(),
        }
    }
    fn primary(&mut self) -> Result<E, String> {
        let t = self.next()?;
        if t == "(" {
            let e = self.expr()?;
            if self.next()? != ")" {
                return Err("expected ')'".into());
            }
            return Ok(e);
        }
        if t == "[" {
            let mut items = Vec::new();
            if self.peek() != Some("]") {
                loop {
                    items.push(self.expr()?);
                    if self.peek() == Some(",") {
                        self.next()?;
                        continue;
                    }
                    break;
                }
            }
            if self.next()? != "]" {
                return Err("expected ']'".into());
            }
            return Ok(E::List(items));
        }
        if t == "true" || t == "false" {
            return Ok(E::Bool(t == "true"));
        }
        if let Some(text) = t.strip_prefix('\'') {
            return Ok(E::Str(text.to_string()));
        }
        if t.as_bytes()[0].is_ascii_digit() {
            return t.parse().map(E::Num).map_err(|_| format!("bad number {t}"));
        }
        if self.peek() == Some("(") {
            self.next()?;
            let mut args = Vec::new();
            if self.peek() != Some(")") {
                loop {
                    args.push(self.expr()?);
                    if self.peek() == Some(",") {
                        self.next()?;
                        continue;
                    }
                    break;
                }
            }
            if self.next()? != ")" {
                return Err("expected ')'".into());
            }
            return Ok(E::Call(t, args));
        }
        Ok(E::Path(t.split('.').map(|x| x.to_string()).collect()))
    }
}

pub fn parse(s: &str) -> Result<E, String> {
    let mut p = Parser { t: lex(s)?, p: 0 };
    let e = p.expr()?;
    if p.p != p.t.len() {
        return Err(format!("trailing tokens in {s:?}"));
    }
    Ok(e)
}

pub type Env = HashMap<String, V>;

/// Where an expression's names are looked up.
pub trait Scope {
    fn lookup(&self, name: &str) -> V;
}

impl Scope for Env {
    fn lookup(&self, name: &str) -> V {
        self.get(name).cloned().unwrap_or(V::Absent)
    }
}

/// Two scopes: names in the first, then in the second.
pub struct Layered<'a, A: Scope, B: Scope>(pub &'a A, pub &'a B);

impl<A: Scope, B: Scope> Scope for Layered<'_, A, B> {
    fn lookup(&self, name: &str) -> V {
        match self.0.lookup(name) {
            V::Absent => self.1.lookup(name),
            v => v,
        }
    }
}

fn items(v: &V) -> Vec<V> {
    match v {
        V::List(items) => items.clone(),
        V::Absent => vec![],
        other => vec![other.clone()],
    }
}

fn same_value(a: &V, b: &V) -> bool {
    match (a.num(), b.num()) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

fn key_of(v: &V) -> Option<String> {
    match v {
        V::Str(s) => Some(s.clone()),
        V::Num(x) if x.fract() == 0.0 && x.abs() < 1e18 => Some(format!("{}", *x as i64)),
        _ => None,
    }
}

pub fn eval<S: Scope>(e: &E, env: &S) -> V {
    match e {
        E::Num(x) => V::Num(*x),
        E::Str(s) => V::Str(s.clone()),
        E::Bool(b) => V::Bool(*b),
        E::List(items) => V::List(items.iter().map(|x| eval(x, env)).collect()),
        E::Path(p) => {
            let mut v = env.lookup(&p[0]);
            for k in &p[1..] {
                v = v.get(k);
            }
            v
        }
        E::Not(a) => V::Bool(!eval(a, env).truthy()),
        E::Neg(a) => match eval(a, env).num() {
            Some(x) => V::Num(-x),
            None => V::Absent,
        },
        E::If(c, a, b) => {
            if eval(c, env).truthy() {
                eval(a, env)
            } else {
                eval(b, env)
            }
        }
        E::Bin(op, a, b) => {
            match op.as_str() {
                "&&" => return V::Bool(eval(a, env).truthy() && eval(b, env).truthy()),
                "||" => return V::Bool(eval(a, env).truthy() || eval(b, env).truthy()),
                "??" => {
                    let x = eval(a, env);
                    return if x == V::Absent { eval(b, env) } else { x };
                }
                _ => {}
            }
            let (x, y) = (eval(a, env), eval(b, env));
            if matches!(op.as_str(), "==" | "!=")
                && (matches!(x, V::Str(_) | V::List(_) | V::Obj(_))
                    || matches!(y, V::Str(_) | V::List(_) | V::Obj(_)))
            {
                let eq = x != V::Absent && y != V::Absent && x == y;
                return V::Bool(if op == "==" { eq } else { !eq && x != V::Absent && y != V::Absent });
            }
            let (Some(x), Some(y)) = (x.num(), y.num()) else {
                return match op.as_str() {
                    "==" | "!=" | "<" | "<=" | ">" | ">=" => V::Bool(false),
                    _ => V::Absent,
                };
            };
            match op.as_str() {
                "+" => V::Num(x + y),
                "-" => V::Num(x - y),
                "*" => V::Num(x * y),
                "/" => V::Num(x / y),
                "==" => V::Bool(x == y),
                "!=" => V::Bool(x != y),
                "<" => V::Bool(x < y),
                "<=" => V::Bool(x <= y),
                ">" => V::Bool(x > y),
                ">=" => V::Bool(x >= y),
                _ => V::Absent,
            }
        }
        E::Call(f, args) => {
            let a: Vec<V> = args.iter().map(|x| eval(x, env)).collect();
            let list = |v: &V| -> Vec<f64> {
                match v {
                    V::List(items) => items.iter().filter_map(|x| x.num()).collect(),
                    other => other.num().into_iter().collect(),
                }
            };
            match f.as_str() {
                "abs" => a[0].num().map(|x| V::Num(x.abs())).unwrap_or(V::Absent),
                "finite" => V::Bool(a[0].num().is_none_or(|x| x.is_finite())),
                "sum" => V::Num(list(&a[0]).iter().sum()),
                "len" => V::Num(match &a[0] {
                    V::List(v) => v.len() as f64,
                    V::Absent => 0.0,
                    _ => 1.0,
                }),
                // the value all members share, else 0 (and 0 for none)
                "same" => {
                    let v = list(&a[0]);
                    V::Num(if !v.is_empty() && v.iter().all(|x| *x == v[0]) {
                        v[0]
                    } else {
                        0.0
                    })
                }
                "in" => V::Bool(
                    a.len() == 2 && a[0] != V::Absent && items(&a[1]).iter().any(|x| same_value(x, &a[0])),
                ),
                // the number of distinct values of a list
                "distinct" => {
                    let v = items(&a[0]);
                    let mut seen: Vec<&V> = Vec::new();
                    for x in &v {
                        if !seen.iter().any(|y| same_value(y, x)) {
                            seen.push(x);
                        }
                    }
                    V::Num(seen.len() as f64)
                }
                "first" => items(&a[0]).into_iter().next().unwrap_or(V::Absent),
                // a member of an object by key (a number is its decimal), or an item of a list
                "at" => match (&a[0], a.get(1)) {
                    (V::Obj(_), Some(k)) => key_of(k).map(|k| a[0].get(&k)).unwrap_or(V::Absent),
                    (V::List(v), Some(k)) => k
                        .num()
                        .filter(|i| *i >= 0.0 && i.fract() == 0.0)
                        .and_then(|i| v.get(i as usize).cloned())
                        .unwrap_or(V::Absent),
                    _ => V::Absent,
                },
                "min" | "max" => {
                    let v: Vec<f64> = if a.len() == 1 { list(&a[0]) } else { a.iter().filter_map(|x| x.num()).collect() };
                    if v.is_empty() {
                        V::Absent
                    } else if f == "min" {
                        V::Num(v.iter().cloned().fold(f64::INFINITY, f64::min))
                    } else {
                        V::Num(v.iter().cloned().fold(f64::NEG_INFINITY, f64::max))
                    }
                }
                "present" => V::Bool(a.first().is_some_and(|x| *x != V::Absent)),
                _ => V::Absent,
            }
        }
    }
}
