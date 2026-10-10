//! References to elements of an IR, checked against the IR (design/ARCHITECTURE.md §3.3,
//! "the end of the amplification class"): every reference is charged to the run's
//! budget, and so is every repeated one, a reference to bytes a reference already
//! used, which the IR can only reach through an alias (it claims no byte twice),
//! whatever the format.

use crate::ir::Ir;
use crate::out::{payload_size, Out, Part, MAX_PAYLOAD};
use serde_json::Value as J;
use std::collections::HashSet;

/// A part of a form's recipe, relative to its element's first byte.
enum Step {
    /// bytes from an offset: to the element's end (None), or of a length
    Src(u64, Option<u64>),
    /// a shared data source of the IR
    Shared(usize),
}

/// The references of an output, charged to a budget: at most 2^22 + 4 · size
/// references, and at most 2^20 repeats of an (element start, length, piece).
pub struct Refs<'a> {
    pub ir: &'a Ir,
    pub out: Out,
    /// each form's recipe (None for a form without one)
    recipes: Vec<Option<Vec<Step>>>,
    seen: HashSet<(u64, u64, u64)>,
    limits: [u64; 2],
    spent: [u64; 2],
}

const WHAT: [&str; 2] = ["refs", "repeats"];

fn recipe(form: &str) -> Option<Vec<Step>> {
    let v: J = serde_json::from_str(form).ok()?;
    v.get("recipe")?
        .as_array()?
        .iter()
        .map(|p| match p[0].as_str()? {
            "src" => Some(Step::Src(p[1].as_u64()?, p[2].as_u64())),
            _ => Some(Step::Shared(p[1].as_u64()? as usize)),
        })
        .collect()
}

impl<'a> Refs<'a> {
    pub fn new(ir: &'a Ir, out: Out) -> Refs<'a> {
        Refs {
            ir,
            out,
            recipes: ir.forms.list.iter().map(|f| recipe(f)).collect(),
            seen: HashSet::new(),
            limits: [(1 << 22) + 4 * ir.size, 1 << 20],
            spent: [0, 0],
        }
    }

    fn charge(&mut self, k: usize) -> Result<(), String> {
        self.spent[k] += 1;
        if self.spent[k] > self.limits[k] {
            return Err(format!("budget: more than {} {}", self.limits[k], WHAT[k]));
        }
        Ok(())
    }

    /// An element's recipe (relative to its first byte) as output parts.
    pub fn parts(&mut self, start: u64, length: u64, form: u32) -> Result<Vec<Part>, String> {
        let steps = self
            .recipes
            .get(form as usize)
            .and_then(|r| r.as_ref())
            .ok_or_else(|| format!("internal: form {form} has no recipe"))?;
        let mut out = Vec::with_capacity(steps.len());
        for s in steps {
            out.push(match *s {
                Step::Src(o, n) => Part::Src(start + o, n.unwrap_or(length.wrapping_sub(o))),
                Step::Shared(k) => {
                    let b = self.ir.shared.get(k).ok_or("internal: no shared source")?;
                    self.out.shared(b)
                }
            });
        }
        Ok(out)
    }

    /// The chunk `key` is the element at [start, start + length): its recipe (form
    /// `form`), or `parts` of it, piece `piece`.
    pub fn chunk(&mut self, key: String, start: u64, length: u64, form: u32, parts: Option<Vec<Part>>, piece: u64) -> Result<(), String> {
        self.charge(0)?;
        if !self.seen.insert((start, length, piece)) {
            self.charge(1)?;
        }
        let parts = match parts {
            Some(p) => p,
            None => self.parts(start, length, form)?,
        };
        if payload_size(&parts) > MAX_PAYLOAD {
            return Err(format!("{key}: a reference payload of more than {MAX_PAYLOAD} bytes"));
        }
        self.out.refs(&key, parts);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An IR of `size` bytes with one form (the element's bytes from 4, then shared source 0).
    fn ir(size: u64) -> Ir {
        let mut ir = Ir::new(size);
        ir.forms.id(r#"{"recipe":[["src",4,null],["src",0,2],["shared",0]]}"#);
        ir.shared.push(b"tail".to_vec());
        ir
    }

    #[test]
    fn references_name_recipes_and_charge_repeats() {
        let ir = ir(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        refs.chunk("a".into(), 10, 20, 0, None, 0).unwrap();
        refs.chunk("b".into(), 10, 20, 0, Some(vec![Part::Src(10, 5)]), 1).unwrap();
        refs.chunk("c".into(), 10, 20, 0, None, 0).unwrap();
        assert_eq!(refs.spent, [3, 1]);
        let whole = vec![Part::Src(14, 16), Part::Src(10, 2), Part::Data(1, 0, 4)];
        assert_eq!(refs.out.refs, vec![("a".into(), whole.clone()), ("b".into(), vec![Part::Src(10, 5)]), ("c".into(), whole)]);
        assert_eq!(refs.out.data, vec![b"tail".to_vec()]);
    }

    #[test]
    fn rejects_references_past_the_budget() {
        let ir = ir(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        assert_eq!(refs.limits[0], (1 << 22) + 400);
        refs.limits[0] = 2;
        refs.chunk("a".into(), 0, 8, 0, None, 0).unwrap();
        refs.chunk("b".into(), 8, 8, 0, None, 0).unwrap();
        assert_eq!(refs.chunk("c".into(), 16, 8, 0, None, 0), Err("budget: more than 2 refs".into()));
    }

    #[test]
    fn rejects_repeats_past_the_budget() {
        let ir = ir(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        assert_eq!(refs.limits[1], 1 << 20);
        refs.limits[1] = 1;
        refs.chunk("a".into(), 0, 8, 0, None, 0).unwrap();
        refs.chunk("b".into(), 0, 8, 0, None, 0).unwrap();
        assert_eq!(refs.chunk("c".into(), 0, 8, 0, None, 0), Err("budget: more than 1 repeats".into()));
    }

    #[test]
    fn rejects_a_payload_past_the_limit() {
        let ir = ir(1 << 40);
        let mut refs = Refs::new(&ir, Out::new("u"));
        let parts = (0..8192).map(|k| Part::Src(k << 30, 1 << 20)).collect();
        assert_eq!(refs.chunk("k".into(), 0, 8, 0, Some(parts), 0), Err("k: a reference payload of more than 65519 bytes".into()));
    }
}
