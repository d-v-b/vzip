//! The canonical order of an IR's elements (spec/conventions.md §8.8): depth
//! first, a parent before its children; siblings by name group (the groups in order
//! of their first byte, the least start of an element with a length in their
//! subtrees), then by name index (none last), then by first byte. The mirror's table
//! is in this order, and `finish` gives a byte two leaves claim to the one first in it.

use crate::ir::*;
use std::collections::HashMap;

/// The first byte of each element's subtree (u64::MAX: none of its elements has a length).
pub fn first_bytes(ir: &Ir) -> Vec<u64> {
    let n = ir.len();
    let mut acc = vec![u64::MAX; n];
    let mut first = vec![u64::MAX; n];
    for i in (0..n).rev() {
        let own = if ir.len[i] > 0 { ir.start[i] } else { acc[i] };
        first[i] = own;
        let p = ir.parent[i];
        if p != NONE && (p as usize) < i {
            acc[p as usize] = acc[p as usize].min(own);
        }
    }
    first
}

/// Element `i`'s children in canonical order: by **name group** (the children of one
/// name), the groups in order of their first byte (the least of their members'), then
/// by name; within a group by name index (none last), then by first byte.
pub fn sorted_children(ir: &Ir, first: &[u64], i: u32) -> Vec<u32> {
    let mut kids: Vec<u32> = ir.children(i).to_vec();
    let mut group: HashMap<u32, u64> = HashMap::new();
    for &c in &kids {
        let g = group.entry(ir.name[c as usize]).or_insert(u64::MAX);
        *g = (*g).min(first[c as usize]);
    }
    kids.sort_by(|&a, &b| {
        let (a, b) = (a as usize, b as usize);
        group[&ir.name[a]]
            .cmp(&group[&ir.name[b]])
            .then_with(|| ir.names.get(ir.name[a]).as_bytes().cmp(ir.names.get(ir.name[b]).as_bytes()))
            .then_with(|| ir.nidx[a].cmp(&ir.nidx[b]))
            .then_with(|| first[a].cmp(&first[b]))
            .then_with(|| a.cmp(&b))
    });
    kids
}

/// The canonical order: `order[k]` is the element at position k, `at[e]` element
/// e's position. Needs the IR's children (`build_children`).
pub fn order(ir: &Ir) -> (Vec<u32>, Vec<u32>) {
    let n = ir.len();
    let first = first_bytes(ir);
    let mut ord = Vec::with_capacity(n);
    let mut at = vec![NONE; n];
    let mut stack = vec![0u32];
    while let Some(i) = stack.pop() {
        at[i as usize] = ord.len() as u32;
        ord.push(i);
        for c in sorted_children(ir, &first, i).into_iter().rev() {
            stack.push(c);
        }
    }
    (ord, at)
}
