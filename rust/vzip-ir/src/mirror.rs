//! The source mirror (design/ARCHITECTURE.md §3.4) of an IR: one projection for every
//! format, written under `vzip_source`. It never reads the source.
//!
//! The mirror is canonical (spec/conventions.md §8.8): a function of the IR alone,
//! so two producers write the same entries for one source. [`canonical`] expands the
//! IR's runs, leaves out the derived spaces past the budget, numbers the elements in
//! canonical order (`canon.rs`) and sorts the interned strings; [`table`] folds the
//! result into the stored table (column runs, their columns' encodings); [`write`]
//! stores a table as arrays; the view shows the canonical IR as JSON.
//! [`canonical_problem`] is the validator's check: it rebuilds the table from a
//! loaded mirror and the source, and names the first way the stored one differs.
//!
//! | array | type | holds |
//! |---|---|---|
//! | `rows` | int64 [8, m] | a column a row: kind, up (the row's id minus its parent's), name, name index (−1: none), type, space, start (for a row with a length, its distance from the end of the last earlier row with one), length |
//! | `tables` | int64 [k, 7] | (code, ...): 0 a run (root, count, stride), never written; 1 an alias (alias, target); 2 a form (element, form); 3 a column run (first row, count, size); 4 a column (column run, row in the member, field, encoding, x, y); 5 six values of the stored columns |
//! | `shared` | family | the shared data sources recipes name (when there are any) |
//!
//! Rebuilding reads the table, expands its column runs (within the record budget),
//! checks the invariants with the checker, and concatenates the source's bytes for
//! its leaves in order. Element ids the facts and the projections use stay those
//! of the parser's rows: only the mirror renumbers.

use crate::ir::*;
use crate::out::{declare, json_size, metadata_array, metadata_group, Chunk, Out, SOURCE_NODE};
use crate::types::{self, Ty, Val};
use serde_json::{json, Map, Value as J};
use std::collections::{HashMap, HashSet};

pub const VERSION: u64 = 2;
/// Rows of derived spaces the table keeps together (conventions §8.2).
const DERIVED_ROWS: u64 = 1 << 20;
const MIN_COUNT: usize = 3;
/// A column run's member is up to this many consecutive siblings (a CZI directory
/// entry and its dimensions are two).
const MAX_PERIOD: usize = 4;
/// The fewest members of a column run whose columns may read an array (a reader
/// reads the array from the source: not worth it for a few).
const MIN_ARRAY_COLUMN: usize = 16;

pub const F_NIDX: i64 = 0;
pub const F_TYPE: i64 = 1;
pub const F_START: i64 = 2;
pub const F_LEN: i64 = 3;
pub const F_FORM: i64 = 4;
pub const F_TARGET: i64 = 5;
pub const F_REL: i64 = 6;

pub const T_RUN: i64 = 0;
pub const T_TARGET: i64 = 1;
pub const T_FORM: i64 = 2;
pub const T_CRUN: i64 = 3;
pub const T_COLUMN: i64 = 4;
pub const T_VALUES: i64 = 5;

/// What the table folded: rows of the IR, rows stored, column runs and their columns by encoding.
#[derive(Default, Debug, Clone)]
pub struct Folded {
    pub rows: u64,
    pub stored: u64,
    pub cruns: u64,
    pub arithmetic: u64,
    pub stored_columns: u64,
    pub element_columns: u64,
}

impl Folded {
    pub fn json(&self) -> J {
        json!({"rows": self.rows, "stored": self.stored, "cruns": self.cruns, "arithmetic": self.arithmetic,
               "stored_columns": self.stored_columns, "element_columns": self.element_columns})
    }
}

/// The rows a table may expand to: the loader's budget (conventions §8.3).
pub fn row_limit(size: u64) -> u64 {
    (1 << 22) + size / 4
}

/// The `["shared", k]` parts of a form, in order: (byte offset of k, its length, k).
fn shared_refs(form: &str) -> Vec<(usize, usize, usize)> {
    const PAT: &str = "[\"shared\",";
    let mut v = Vec::new();
    let mut from = 0;
    while let Some(p) = form[from..].find(PAT) {
        let a = from + p + PAT.len();
        let n = form[a..].bytes().take_while(u8::is_ascii_digit).count();
        if n > 0 && form[a + n..].starts_with(']') {
            if let Ok(k) = form[a..a + n].parse() {
                v.push((a, n, k));
            }
        }
        from = a;
    }
    v
}

/// The canonical IR of `ir` (conventions §8.8), or why it has none (the root and the
/// names of §8.1, `check::names`, do not hold): every run expanded; the elements in
/// canonical order, numbered by it (a parent before its children, siblings by first
/// byte, name, name index); the descendants of a derived element left out when they
/// would bring the derived rows kept past 2^20 (derived elements in canonical order,
/// those inside a kept derived space counted with it); names and types the ones used,
/// sorted by their UTF-8 bytes; shared sources numbered by first use (rows in order,
/// a form's parts in order) and the forms, so renumbered, sorted.
pub fn canonical(ir: &Ir) -> Result<Ir, String> {
    let mut x = ir.clone();
    x.budget = None;
    if x.runs.iter().any(|r| (r.0 as usize) >= x.len() || x.nidx[r.0 as usize] == NO_INDEX) {
        return Err("a run whose root has no name index".into());
    }
    let roots: Vec<u32> = x.runs.iter().map(|r| r.0).collect();
    for r in roots {
        x.unfold(r)?;
    }
    // the root and the names (§8.1): a table that breaks them is invalid
    crate::check::names(&x)?;
    x.build_children();
    let (ord, _) = crate::canon::order(&x);
    let n = x.len();
    if ord.len() != n {
        return Err("an element outside the tree".into());
    }
    let mut size = vec![1u64; n];
    for &i in ord.iter().rev() {
        let p = x.parent[i as usize];
        if p != NONE {
            size[p as usize] += size[i as usize];
        }
    }
    let mut keep = vec![true; n];
    let mut drop_kids = vec![false; n];
    let mut inside = vec![false; n];
    let mut spent = 0u64;
    let mut kept: Vec<u32> = Vec::with_capacity(n);
    for &i in &ord {
        let iu = i as usize;
        let p = x.parent[iu];
        if p != NONE {
            let pu = p as usize;
            if !keep[pu] || drop_kids[pu] {
                keep[iu] = false;
                continue;
            }
            inside[iu] = inside[pu] || x.kind[pu] == DERIVED;
        }
        kept.push(i);
        if x.kind[iu] == DERIVED && !inside[iu] {
            let d = size[iu] - 1;
            if spent + d <= DERIVED_ROWS {
                spent += d;
            } else {
                drop_kids[iu] = true;
            }
        }
    }
    let mut at = vec![NONE; n];
    for (k, &i) in kept.iter().enumerate() {
        at[i as usize] = k as u32;
    }
    // interned strings: the used ones, sorted by bytes
    let sorted = |used: &mut Vec<u32>, s: &Interner| -> (Interner, HashMap<u32, u32>) {
        used.sort_unstable();
        used.dedup();
        let mut list: Vec<(String, u32)> = used.iter().map(|&u| (s.get(u).to_string(), u)).collect();
        list.sort();
        let map: HashMap<u32, u32> = list.iter().enumerate().map(|(k, (_, u))| (*u, k as u32)).collect();
        let mut out = Interner::default();
        for (t, _) in list {
            out.id(&t);
        }
        (out, map)
    };
    let (names, name_map) = sorted(&mut kept.iter().map(|&i| x.name[i as usize]).collect(), &x.names);
    let (types, type_map) = sorted(&mut kept.iter().map(|&i| x.ty[i as usize]).collect(), &x.types);
    // shared sources by first use; forms rewritten with the new numbers, then sorted
    let mut shared_map: HashMap<usize, usize> = HashMap::new();
    let mut shared = Vec::new();
    let mut used_forms: Vec<u32> = Vec::new();
    for &i in &kept {
        if let Some(f) = x.form_id(i) {
            used_forms.push(f);
            for (_, _, k) in shared_refs(x.forms.get(f)) {
                if let std::collections::hash_map::Entry::Vacant(e) = shared_map.entry(k) {
                    let b = x.shared.get(k).ok_or(format!("a form names shared source {k}, which the IR has not"))?;
                    e.insert(shared.len());
                    shared.push(b.clone());
                }
            }
        }
    }
    let mut renamed = Interner::default();
    let mut form_old: HashMap<u32, String> = HashMap::new();
    for &f in &used_forms {
        if form_old.contains_key(&f) {
            continue;
        }
        let t = x.forms.get(f);
        let mut s = String::with_capacity(t.len());
        let mut last = 0;
        for (a, len, k) in shared_refs(t) {
            s.push_str(&t[last..a]);
            s.push_str(&shared_map[&k].to_string());
            last = a + len;
        }
        s.push_str(&t[last..]);
        form_old.insert(f, s.clone());
        renamed.id(&s);
    }
    let mut form_ids: Vec<u32> = (0..renamed.list.len() as u32).collect();
    let (forms, renamed_map) = sorted(&mut form_ids, &renamed);
    let form_map: HashMap<u32, u32> = form_old.iter().map(|(&f, s)| (f, renamed_map[&renamed.map[s]])).collect();
    let targets: HashMap<u32, u32> = x.targets.iter().copied().collect();
    let m = kept.len();
    let mut y = Ir { size: x.size, budget: None, names, types, forms, ..Default::default() };
    y.kind.reserve(m);
    for &i in &kept {
        let iu = i as usize;
        y.kind.push(x.kind[iu]);
        y.parent.push(if x.parent[iu] == NONE { NONE } else { at[x.parent[iu] as usize] });
        y.name.push(name_map[&x.name[iu]]);
        y.nidx.push(x.nidx[iu]);
        y.ty.push(type_map[&x.ty[iu]]);
        y.space.push(if x.space[iu] == 0 { 0 } else { at[x.space[iu] as usize] });
        y.start.push(x.start[iu]);
        y.len.push(x.len[iu]);
        if let Some(b) = x.value_bytes(i) {
            let k = y.len() as u32 - 1;
            y.values.push((k, y.vbytes.len() as u64, b.len() as u32));
            y.vbytes.extend_from_slice(b);
        }
        if let Some(f) = x.form_id(i) {
            y.form_of.push((y.len() as u32 - 1, form_map[&f]));
        }
        if let Some(&t) = targets.get(&i) {
            y.targets.push((y.len() as u32 - 1, if t == NONE { NONE } else { at[t as usize] }));
        }
    }
    y.shared = shared;
    for (k, b) in y.shared.iter().enumerate() {
        y.shared_index.insert(b.clone(), k as u32);
    }
    y.finished = true;
    y.build_children();
    Ok(y)
}

/// The fields of row `i` (of a canonical IR) a column may hold.
fn field(y: &Ir, i: usize, f: i64, root: usize) -> i64 {
    match f {
        F_NIDX => y.nidx[i] as i64,
        F_TYPE => y.ty[i] as i64,
        F_START => y.start[i] as i64,
        F_LEN => y.len[i] as i64,
        F_FORM => y.form_id(i as u32).map(|x| x as i64).unwrap_or(-1),
        F_TARGET => {
            let t = y.target(i as u32);
            if t == NONE { -1 } else { t as i64 }
        }
        _ => y.start[i].wrapping_sub(y.start[root]) as i64,
    }
}

/// Whether the subtrees at `a` and `b` (of `size` rows each, in a canonical IR) have
/// one shape: row by row the same kind, name, presence of a length and parent
/// (relative to the subtree's first row).
fn same_shape(y: &Ir, a: usize, b: usize, size: usize) -> bool {
    (0..size).all(|k| {
        let (i, j) = (a + k, b + k);
        y.kind[i] == y.kind[j]
            && y.name[i] == y.name[j]
            && (y.len[i] > 0) == (y.len[j] > 0)
            && (k == 0 || y.parent[i] as usize - a == y.parent[j] as usize - b)
    })
}

/// The child of `p` (in canonical order) named `name` with index `nidx`.
fn child(y: &Ir, p: u32, name: &str, nidx: u64) -> Option<u32> {
    y.children(p).iter().copied().find(|&c| y.names.get(y.name[c as usize]) == name && y.nidx[c as usize] == nidx)
}

/// The array an encoding-2 column reads (conventions §8.8, the named array rules):
/// for a column run whose member 0 is row `r`, its field `f`, by the profile's rules.
/// Returns (row, item size, big-endian). TIFF has one rule; ND2 and CZI none.
fn array_rule(profile: &str, y: &Ir, r: usize, f: i64) -> Option<(u32, usize, bool)> {
    match profile {
        "tiff" => tiff_layout(y, r, f),
        _ => None,
    }
}

/// TIFF's rule, "layout tables": for a column run of single data rows in a struct
/// `tiles` (`strips`) whose parent has `tags/324` (`tags/273`) for starts and
/// `tags/325` (`tags/279`) for lengths, that struct's child `value`, when it is a
/// value in the source of an unsigned integer array type.
fn tiff_layout(y: &Ir, r: usize, f: i64) -> Option<(u32, usize, bool)> {
    let p = y.parent[r];
    if y.kind[r] != DATA || p == NONE || y.nidx[p as usize] != NO_INDEX {
        return None;
    }
    let tag = match (y.names.get(y.name[p as usize]), f) {
        ("tiles", F_START) => 324,
        ("tiles", F_LEN) => 325,
        ("strips", F_START) => 273,
        ("strips", F_LEN) => 279,
        _ => return None,
    };
    let ifd = y.parent[p as usize];
    if ifd == NONE {
        return None;
    }
    let s = child(y, ifd, "tags/", tag)?;
    if y.kind[s as usize] != STRUCT {
        return None;
    }
    let v = child(y, s, "value", NO_INDEX)?;
    let vu = v as usize;
    if y.kind[vu] != VALUE || y.space[vu] != 0 || y.len[vu] == 0 {
        return None;
    }
    let (z, big) = unsigned_array(y.types.get(y.ty[vu]))?;
    Some((v, z, big))
}

/// The stored table of a canonical IR (conventions §8.8): its column runs found
/// outermost first, left to right, each at the period that folds the most siblings
/// (the smaller on a tie); each column at the first encoding that holds it (none
/// when constant, 3, 0, 2, 1). `array(row)` gives the bytes of a value an encoding-2
/// column would read (the IR's, or the source's at its extent).
pub fn table(y: &Ir, profile: &str, array: &dyn Fn(u32) -> Option<Vec<u8>>) -> (Table, Folded) {
    let n = y.len();
    let mut size = vec![1usize; n];
    let mut bad = vec![false; n]; // a subtree with a derived row or a row in a derived space
    for k in (0..n).rev() {
        bad[k] |= y.kind[k] == DERIVED || y.space[k] != 0;
        if k > 0 {
            let p = y.parent[k] as usize;
            size[p] += size[k];
            bad[p] |= bad[k];
        }
    }
    let mut cruns: Vec<(usize, usize, usize)> = Vec::new(); // (first row, count, size)
    let mut covered = vec![false; n]; // rows of a column run's members
    for p in 0..n {
        if covered[p] || size[p] == 1 {
            continue;
        }
        let kids = y.children(p as u32);
        let k = kids.len();
        let at = |x: usize| kids[x] as usize;
        let mut a = 0;
        while a < k {
            let mut best: Option<(usize, usize)> = None;
            for per in 1..=MAX_PERIOD {
                if a + per > k || bad[at(a + per - 1)] {
                    break;
                }
                let mut reps = 1;
                while a + (reps + 1) * per <= k
                    && (0..per).all(|t| {
                        let (u, v) = (at(a + t), at(a + reps * per + t));
                        size[u] == size[v] && !bad[v] && same_shape(y, u, v, size[u])
                    })
                {
                    reps += 1;
                }
                if reps >= MIN_COUNT && best.is_none_or(|(bp, br)| reps * per > bp * br) {
                    best = Some((per, reps));
                }
            }
            let Some((per, reps)) = best else {
                a += 1;
                continue;
            };
            let r = at(a);
            let msize: usize = (a..a + per).map(|x| size[at(x)]).sum();
            let last = at(a + reps * per - 1);
            for c in covered.iter_mut().take(last + size[last]).skip(r) {
                *c = true;
            }
            cruns.push((r, reps, msize));
            a += reps * per;
        }
    }
    cruns.sort_unstable();
    let mut t = Table { size: y.size, ..Default::default() };
    let mut folded = Folded { rows: n as u64, ..Default::default() };
    let mut skip = vec![false; n]; // rows of members 1..
    for (q, &(r, count, sz)) in cruns.iter().enumerate() {
        for s in skip.iter_mut().take(r + sz * count).skip(r + sz) {
            *s = true;
        }
        t.cruns.push([r as i64, count as i64, sz as i64]);
        for off in 0..sz {
            let rows: Vec<usize> = (0..count).map(|j| r + j * sz + off).collect();
            let fields: &[i64] = if off > 0 && y.len[r + off] > 0 && y.len[r] > 0 {
                &[F_NIDX, F_TYPE, F_REL, F_LEN, F_FORM, F_TARGET]
            } else {
                &[F_NIDX, F_TYPE, F_START, F_LEN, F_FORM, F_TARGET]
            };
            for &f in fields {
                let v: Vec<i64> = rows.iter().enumerate().map(|(j, &i)| field(y, i, f, r + j * sz)).collect();
                if v.iter().all(|&x| x == v[0]) {
                    continue; // member 0's row holds it
                }
                let col = |enc: i64, x: i64, yy: i64| [q as i64, off as i64, f, enc, x, yy];
                if f == F_NIDX && rows.iter().zip(&v).all(|(&i, &x)| y.len[i] > 0 && x == y.start[i] as i64) {
                    t.columns.push(col(3, 0, 0));
                    folded.arithmetic += 1;
                    continue;
                }
                let d = v[1].wrapping_sub(v[0]);
                if v.windows(2).all(|w| w[1].wrapping_sub(w[0]) == d) {
                    t.columns.push(col(0, v[0], d));
                    folded.arithmetic += 1;
                    continue;
                }
                if sz == 1 && count >= MIN_ARRAY_COLUMN && (f == F_START || f == F_LEN) && y.nidx[r] != NO_INDEX {
                    if let Some((e, z, big)) = array_rule(profile, y, r, f) {
                        let first = y.nidx[r] as usize;
                        let ok = array(e).filter(|b| b.len() as u64 == y.len[e as usize] && b.len() % z == 0).is_some_and(|b| {
                            let a = uints(&b, z, big);
                            first.checked_add(count).is_some_and(|end| end <= a.len())
                                && v.iter().zip(&a[first..]).all(|(&x, &u)| x >= 0 && x as u64 == u)
                        });
                        if ok {
                            t.columns.push(col(2, e as i64, first as i64));
                            folded.element_columns += 1;
                            continue;
                        }
                    }
                }
                t.columns.push(col(1, t.column_values.len() as i64, 0));
                let mut prev = 0i64;
                for x in v {
                    t.column_values.push(x.wrapping_sub(prev));
                    prev = x;
                }
                folded.stored_columns += 1;
            }
        }
    }
    folded.cruns = cruns.len() as u64;
    let mut end = 0u64;
    for k in (0..n).filter(|&k| !skip[k]) {
        let (s, l) = (y.start[k], y.len[k]);
        let d = if l > 0 { s.wrapping_sub(end) as i64 } else { s as i64 };
        if l > 0 {
            end = s.wrapping_add(l);
        }
        t.kind.push(y.kind[k]);
        t.up.push(if k == 0 { 0 } else { k as u32 - y.parent[k] });
        t.name.push(y.name[k] as i32);
        t.nidx.push(y.nidx[k]);
        t.ty.push(y.ty[k] as i32);
        t.space.push(y.space[k]);
        t.start.push(d);
        t.length.push(l);
        let tg = y.target(k as u32);
        if y.targets.binary_search_by_key(&(k as u32), |e| e.0).is_ok() {
            t.targets.push([k as i64, if tg == NONE { -1 } else { tg as i64 }]);
        }
        if let Some(f) = y.form_id(k as u32) {
            t.forms.push([k as i64, f as i64]);
        }
    }
    folded.stored = t.kind.len() as u64;
    t.names = y.names.list.clone();
    t.types = y.types.list.clone();
    t.form_text = y.forms.list.clone();
    t.shared = y.shared.clone();
    (t, folded)
}

/// The rows a table expands to: its stored rows and members 1.. of its column runs.
pub fn expanded_rows(t: &Table) -> u64 {
    t.kind.len() as u64 + t.cruns.iter().map(|c| (c[1].max(1) as u64 - 1).saturating_mul(c[2].max(0) as u64)).sum::<u64>()
}

/// Stores table `t` under `vzip_source` (conventions §8.2): its description in
/// `vzip_source`'s source metadata under `profile`'s convention, `ir/rows`, `ir/tables`
/// (codes in order: runs, aliases, forms, column runs, columns, column values) and,
/// when there are shared sources, `ir/shared`. `deflate` (for tests of what a
/// validator flags) deflates every chunk; the canonical table does not.
pub fn write(t: &Table, out: &mut Out, profile: &str, deflate: bool) {
    let own = json!({"ir": {"version": VERSION, "size": t.size, "elements": expanded_rows(t), "rows": t.kind.len(),
                            "names": t.names, "types": t.types, "forms": t.form_text}});
    metadata_group(out, SOURCE_NODE, Some(declare(Map::new(), profile, None, Some(own))));
    let m = t.kind.len();
    let mut rows: Vec<i64> = Vec::with_capacity(8 * m);
    rows.extend(t.kind.iter().map(|&x| x as i64));
    rows.extend(t.up.iter().map(|&x| x as i64));
    rows.extend(t.name.iter().map(|&x| x as i64));
    rows.extend(t.nidx.iter().map(|&x| x as i64));
    rows.extend(t.ty.iter().map(|&x| x as i64));
    rows.extend(t.space.iter().map(|&x| x as i64));
    rows.extend(t.start.iter().copied());
    rows.extend(t.length.iter().map(|&x| x as i64));
    let mut tables: Vec<[i64; 7]> = Vec::new();
    tables.extend(t.runs.iter().map(|r| [T_RUN, r[0], r[1], r[2], 0, 0, 0]));
    tables.extend(t.targets.iter().map(|r| [T_TARGET, r[0], r[1], 0, 0, 0, 0]));
    tables.extend(t.forms.iter().map(|r| [T_FORM, r[0], r[1], 0, 0, 0, 0]));
    tables.extend(t.cruns.iter().map(|c| [T_CRUN, c[0], c[1], c[2], 0, 0, 0]));
    tables.extend(t.columns.iter().map(|c| [T_COLUMN, c[0], c[1], c[2], c[3], c[4], c[5]]));
    for v in t.column_values.chunks(6) {
        let mut r = [T_VALUES, 0, 0, 0, 0, 0, 0];
        r[1..1 + v.len()].copy_from_slice(v);
        tables.push(r);
    }
    let flat = |v: &[i64]| -> Vec<u8> { v.iter().flat_map(|x| x.to_le_bytes()).collect() };
    copied_as(out, "ir/rows", "int64", 8, &[8, m as u64], &flat(&rows), deflate);
    let tt: Vec<i64> = tables.iter().flatten().copied().collect();
    copied_as(out, "ir/tables", "int64", 8, &[tables.len() as u64, 7], &flat(&tt), deflate);
    if !t.shared.is_empty() {
        let mut starts = vec![0i64];
        for b in &t.shared {
            starts.push(starts.last().unwrap() + b.len() as i64);
        }
        metadata_group(out, &format!("{SOURCE_NODE}/ir/shared"), None);
        copied_as(out, "ir/shared/offsets", "int64", 8, &[starts.len() as u64], &flat(&starts), deflate);
        let data: Vec<u8> = t.shared.concat();
        if !data.is_empty() {
            copied_as(out, "ir/shared/data", "uint8", 1, &[data.len() as u64], &data, deflate);
        }
    }
}

/// Writes the mirror of `ir` under `vzip_source`: the canonical IR's table and view.
pub fn mirror(ir: &Ir, out: &mut Out, profile: &str) -> Result<Folded, String> {
    let limit = row_limit(ir.size);
    let over = || format!("budget: the mirror would have more than {limit} rows");
    // at least the rows outside derived spaces, every run expanded: rejected before expanding
    let extra: u64 = ir.runs.iter()
        .map(|&(r, c, _)| c.saturating_sub(1).saturating_mul((ir.subtree_end(r) - r) as u64))
        .fold(0u64, |a, b| a.saturating_add(b));
    let outside = ir.space.iter().filter(|&&d| d == 0).count() as u64;
    if outside.saturating_add(extra) > limit {
        return Err(over());
    }
    let y = canonical(ir)?;
    if y.len() as u64 > limit {
        return Err(over());
    }
    // a derived space holds shown values only when a validator can derive it (§8.8)
    for i in 0..y.len() as u32 {
        let d = y.space[i as usize];
        if d != 0 && shown(&y, profile, i) {
            let t = y.form(d).and_then(crate::transform::Transform::of_form);
            if !t.is_some_and(|t| t.may_hold_shown()) {
                return Err(format!("internal: a value the view shows in a derived space of form {:?}", y.form(d)));
            }
        }
    }
    let (t, folded) = table(&y, profile, &|e| y.value_bytes(e).map(|b| b.to_vec()));
    write(&t, out, profile, false);
    View::new(&y, profile).tree(out);
    Ok(folded)
}

/// (item size, big-endian) of a type that is a 1-D array of unsigned integers.
pub fn unsigned_array(t: &str) -> Option<(usize, bool)> {
    match types::parse(t).ok()? {
        Ty::Num { code, big, size, dims } if dims.len() == 1 && code.starts_with('u') => Some((size, big)),
        _ => None,
    }
}

pub fn uints(raw: &[u8], z: usize, big: bool) -> Vec<u64> {
    raw.chunks(z)
        .map(|c| {
            let mut b = [0u8; 8];
            if big {
                b[8 - z..].copy_from_slice(c);
                u64::from_be_bytes(b)
            } else {
                b[..z].copy_from_slice(c);
                u64::from_le_bytes(b)
            }
        })
        .collect()
}

/// The most values of a table chunk (1 MiB of int64).
const CHUNK_VALUES: u64 = 1 << 17;

/// A table array of int64 (or uint8) values, C order, in chunks of raw little-endian
/// bytes (the archive deflates its entries): a 1-D array in chunks of at most 2^20
/// bytes; `ir/rows` [8, m] in chunks [8, 2^14]; `ir/tables` [k, 7] in chunks of whole
/// rows, at most 2^17 values. Edge chunks are padded with zeros.
fn copied_as(out: &mut Out, path: &str, data_type: &str, item: u64, shape: &[u64], data: &[u8], deflate: bool) {
    let (chunk_shape, dims): (Vec<u64>, &[&str]) = match (path, shape) {
        (_, [n]) => (vec![(*n).clamp(1, CHUNK_VALUES * 8 / item)], &["index"]),
        ("ir/rows", [k, n]) => (vec![*k, (*n).clamp(1, CHUNK_VALUES / 8)], &["column", "index"]),
        (_, [k, w]) => (vec![(*k).clamp(1, (CHUNK_VALUES / w).max(1)), *w], &["index", "part"]),
        _ => unreachable!(),
    };
    let grid: Vec<u64> = shape.iter().zip(&chunk_shape).map(|(n, c)| n.div_ceil(*c)).collect();
    let mut chunks = Vec::new();
    let width = if shape.len() == 2 { shape[1] } else { 1 };
    for g0 in 0..grid[0] {
        for g1 in 0..*grid.get(1).unwrap_or(&1) {
            let mut raw = Vec::with_capacity((chunk_shape.iter().product::<u64>() * item) as usize);
            for r in 0..chunk_shape[0] {
                let row = g0 * chunk_shape[0] + r;
                let (c0, cw) = if shape.len() == 2 { (g1 * chunk_shape[1], chunk_shape[1]) } else { (0, 1) };
                for c in 0..cw {
                    let col = c0 + c;
                    let inside = row < shape[0] && (shape.len() == 1 || col < width);
                    let at = ((row * width + col) * item) as usize;
                    if inside {
                        raw.extend_from_slice(&data[at..at + item as usize]);
                    } else {
                        raw.extend(std::iter::repeat_n(0, item as usize));
                    }
                }
            }
            let coords = if shape.len() == 2 { vec![g0, g1] } else { vec![g0] };
            let body = if deflate { miniz_oxide::deflate::compress_to_vec_zlib(&raw, 6) } else { raw };
            chunks.push((coords, Chunk::Bytes(body)));
        }
    }
    let codec = deflate.then(|| json!({"name": "zlib", "configuration": {"level": 6}}));
    metadata_array(out, &format!("{SOURCE_NODE}/{path}"), data_type, shape, &chunk_shape, Some(dims), chunks, "little", codec);
}

// ---- the view

const DOC: usize = 1 << 16; // the most bytes of a view document
const SMALL: usize = 1 << 12; // the most bytes of a value's JSON
const MAX_GROUPS: usize = 1 << 12;
const INLINE: usize = 1 << 14; // a struct whose view is larger is a group of its own
const WORK: usize = 1 << 20; // elements the view visits
const TOTAL: usize = 1 << 24; // bytes of all view documents
/// The most bytes of a value the view shows (conventions §8.7).
pub const VIEW_VALUE: u64 = 1 << 10;
const MAX_SAFE: i128 = (1 << 53) - 1;

pub fn escape_key(k: &str) -> String {
    if k.starts_with('$') { format!("${k}") } else { k.to_string() }
}

pub fn escape_path(p: &str) -> String {
    if p.is_empty() {
        return String::new();
    }
    p.split('/')
        .map(|s| {
            if s.is_empty() {
                return "%".to_string();
            }
            s.bytes()
                .map(|b| if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") })
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("/")
}

pub fn base64(b: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut s = String::with_capacity(b.len().div_ceil(3) * 4);
    for c in b.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for k in 0..4 {
            if k <= c.len() {
                s.push(A[((n >> (18 - 6 * k)) & 63) as usize] as char);
            } else {
                s.push('=');
            }
        }
    }
    s
}

/// A decoded value as JSON, in the `$vz` vocabulary.
pub fn tag_json(v: &Val) -> J {
    match v {
        Val::Int(i) => {
            if i.abs() <= MAX_SAFE {
                json!(*i as i64)
            } else {
                json!({"$vz": "int", "v": i.to_string()})
            }
        }
        Val::Float(f) => {
            if f.is_finite() {
                json!(f)
            } else {
                json!({"$vz": "float", "v": if f.is_nan() { "NaN" } else if *f > 0.0 { "Infinity" } else { "-Infinity" }})
            }
        }
        Val::Str(s) => json!(s),
        Val::Bytes(b) => json!({"$vz": "bytes", "b64": base64(b)}),
        Val::List(items) => J::Array(items.iter().map(tag_json).collect()),
        Val::Rec(fields) => J::Object(fields.iter().map(|(k, x)| (escape_key(k), tag_json(x))).collect()),
    }
}

/// Whether the view shows value `i` (conventions §8.7): a value of a number, record,
/// `guid`, `xml` or text type (`ascii`, `cstr`, `utf16`; not `bytes`) whose extent is
/// at most 1024 bytes, other than an ND2 frame's `timestamp` (which a parser need not
/// read). Every such value is held (the run reads those its parser did not).
pub fn shown(y: &Ir, profile: &str, i: u32) -> bool {
    let iu = i as usize;
    if y.kind[iu] != VALUE || y.len[iu] > VIEW_VALUE {
        return false;
    }
    if profile == "nd2" && y.names.get(y.name[iu]) == "timestamp" {
        let p = y.parent[iu];
        let g = if p == NONE { NONE } else { y.parent[p as usize] };
        if g != NONE && y.names.get(y.name[g as usize]) == "frames" {
            return false;
        }
    }
    let t = y.types.get(y.ty[iu]);
    match types::parse(t) {
        Ok(Ty::Num { .. }) | Ok(Ty::Rec { .. }) | Ok(Ty::Xml) => true,
        Ok(Ty::Text { kind, .. }) => kind != "bytes",
        Err(_) => false,
    }
}

struct View<'a> {
    y: &'a Ir,
    profile: &'a str,
    spent: usize,
    groups: usize,
    visited: usize,
    paths: HashSet<String>,
    tys: HashMap<u32, Option<Ty>>,
}

impl<'a> View<'a> {
    fn new(y: &'a Ir, profile: &'a str) -> View<'a> {
        View { y, profile, spent: 0, groups: 0, visited: 0, paths: HashSet::new(), tys: HashMap::new() }
    }

    fn id(&self, i: u32) -> J {
        json!({"$vz": "element", "id": i})
    }

    fn tree(mut self, out: &mut Out) {
        let doc = self.render(out, 0, "tree");
        self.group(out, "tree", doc, true);
        // every group's ancestors are groups
        let prefix = format!("{SOURCE_NODE}/");
        let mut have: HashSet<String> = out.entries.iter()
            .filter_map(|(k, _)| k.strip_prefix(&prefix).and_then(|r| r.strip_suffix("/zarr.json")).map(|s| s.to_string()))
            .collect();
        let mut sorted: Vec<String> = have.iter().cloned().collect();
        sorted.sort();
        for p in sorted {
            let parts: Vec<&str> = p.split('/').collect();
            for k in 1..parts.len() {
                let q = parts[..k].join("/");
                if !have.contains(&q) {
                    have.insert(q.clone());
                    metadata_group(out, &format!("{SOURCE_NODE}/{q}"), None);
                }
            }
        }
    }

    /// Stores `doc` as the group at `path` when it fits the documents' total and the
    /// path is free (`force`: the root's, always).
    fn group(&mut self, out: &mut Out, path: &str, doc: Map<String, J>, force: bool) -> bool {
        let doc = J::Object(doc);
        let n = json_size(&doc);
        if !force && (self.spent + n > TOTAL || self.paths.contains(path)) {
            return false;
        }
        self.spent += n;
        self.paths.insert(path.to_string());
        // a view document is the group's source metadata: the group declares the convention
        metadata_group(out, &format!("{SOURCE_NODE}/{path}"), Some(declare(Map::new(), self.profile, None, Some(doc))));
        true
    }

    fn render(&mut self, out: &mut Out, s: u32, path: &str) -> Map<String, J> {
        let y = self.y;
        let mut doc = Map::new();
        if y.kind[s as usize] == DERIVED {
            doc.insert("$vz".into(), json!("derived"));
            doc.insert("$form".into(), y.form(s).map(|f| json!(f)).unwrap_or(J::Null));
        }
        let mut left = DOC;
        for &c in y.children(s) {
            self.visited += 1;
            if left < 1 << 10 || self.visited > WORK {
                doc.insert("$partial".into(), json!(true));
                break;
            }
            let cu = c as usize;
            let k = y.kind[cu];
            let name = y.name_of(c);
            let key = escape_key(&name);
            let mut v = if k == STRUCT || k == DERIVED {
                let sub = format!("{path}/{}", escape_path(&name));
                let m = self.render(out, c, &sub);
                let v = J::Object(m);
                if json_size(&v) > INLINE {
                    let J::Object(m) = v else { unreachable!() };
                    if self.groups < MAX_GROUPS && self.group(out, &sub, m, false) {
                        self.groups += 1;
                        json!({"$vz": "group", "path": sub})
                    } else {
                        self.id(c)
                    }
                } else {
                    v
                }
            } else if k == ALIAS {
                let t = y.target(c);
                if t == NONE || matches!(y.kind[t as usize], DATA | GAP) {
                    continue;
                }
                json!({"$vz": "alias", "path": y.path(t)})
            } else if k == VALUE {
                self.value(c)
            } else {
                continue;
            };
            let key_size = json_size(&J::String(key.clone()));
            let mut n = json_size(&v) + key_size + 2;
            if n > left {
                v = self.id(c);
                n = json_size(&v) + key_size + 2;
            }
            left -= n.min(left);
            if doc.contains_key(&key) {
                continue; // a repeated name: the view keeps the first, the table has both
            }
            doc.insert(key, v);
        }
        doc
    }

    /// A value's view: its JSON when shown and small, else a reference to its row.
    fn value(&mut self, i: u32) -> J {
        let y = self.y;
        if !shown(y, self.profile, i) {
            return self.id(i);
        }
        let tid = y.ty[i as usize];
        let ty = self.tys.entry(tid).or_insert_with(|| types::parse(y.types.get(tid)).ok()).clone();
        if let (Some(raw), Some(Ty::Text { kind, .. })) = (y.value_bytes(i), &ty) {
            if kind != "guid" {
                let j = text_json(kind, raw);
                return if json_size(&j) <= SMALL { j } else { self.id(i) };
            }
        }
        if let (Some(raw), Some(t)) = (y.value_bytes(i), &ty) {
            if let Ok(mut v) = types::decode(t, raw) {
                if let Val::Rec(fields) = &v {
                    if let Some((_, x)) = fields.iter().find(|(k, _)| k == "v") {
                        // a record whose field `v` is its value: the rest is framing
                        let x = x.clone();
                        if y.types.get(tid).contains(",v:utf16[") {
                            if let Val::Bytes(b) = &x {
                                let j = utf16(&b[..b.len().saturating_sub(2)]);
                                if json_size(&j) <= SMALL {
                                    return j;
                                }
                            }
                        }
                        v = x;
                    }
                }
                let j = tag_json(&v);
                if json_size(&j) <= SMALL {
                    return j;
                }
            }
        }
        self.id(i)
    }
}

/// A text value's view (conventions §8.7, by §6's rule): its text, the bytes up to the
/// first NUL (`ascii`, `cstr`) or NUL unit (`utf16`): for `ascii` and `cstr` a string
/// when UTF-8, else `{"latin1": s}` (each byte the code point U+00bb); for `utf16` a
/// string when UTF-16, else `{"$vz": "utf16", "b64": ...}`. When bytes from that NUL
/// on are not all zero, `{"text": <its text>, "after": {"$vz": "bytes", "b64": ...}}`
/// with those bytes (the NUL included), as a record's `cstr` field is: nothing is lost.
pub fn text_json(kind: &str, raw: &[u8]) -> J {
    let (n, text) = if kind == "utf16" {
        let n = raw.chunks(2).position(|c| c.len() == 2 && c == [0, 0]).map_or(raw.len(), |k| 2 * k);
        (n, utf16(&raw[..n]))
    } else {
        let n = raw.iter().position(|&c| c == 0).unwrap_or(raw.len());
        let b = &raw[..n];
        let t = match std::str::from_utf8(b) {
            Ok(s) => json!(s),
            Err(_) => json!({"latin1": b.iter().map(|&c| c as char).collect::<String>()}),
        };
        (n, t)
    };
    if raw[n..].iter().any(|&c| c != 0) {
        json!({"text": text, "after": {"$vz": "bytes", "b64": base64(&raw[n..])}})
    } else {
        text
    }
}

/// UTF-16LE text, or its bytes in the `$vz` vocabulary when they are not text.
fn utf16(b: &[u8]) -> J {
    let units: Vec<u16> = b.chunks(2).map(|c| u16::from_le_bytes([c[0], *c.get(1).unwrap_or(&0)])).collect();
    match (b.len() % 2 == 0).then(|| String::from_utf16(&units).ok()).flatten() {
        Some(s) => json!(s),
        None => json!({"$vz": "utf16", "b64": base64(b)}),
    }
}

// ---- reading it back

/// The stored table (`vzip_source/ir/...` decoded by the host).
#[derive(Default)]
pub struct Table {
    pub size: u64,
    pub kind: Vec<u8>,
    pub up: Vec<u32>,
    pub name: Vec<i32>,
    pub nidx: Vec<u64>,
    pub ty: Vec<i32>,
    pub space: Vec<u32>,
    pub start: Vec<i64>,
    pub length: Vec<u64>,
    pub runs: Vec<[i64; 3]>,
    pub targets: Vec<[i64; 2]>,
    pub forms: Vec<[i64; 2]>,
    pub cruns: Vec<[i64; 3]>,
    pub columns: Vec<[i64; 6]>,
    pub column_values: Vec<i64>,
    pub names: Vec<String>,
    pub types: Vec<String>,
    pub form_text: Vec<String>,
    /// the shared data sources (`ir/shared`)
    pub shared: Vec<Vec<u8>>,
}

/// The IR a stored table describes, its column runs expanded (at most the record
/// budget of rows). `read(offset, length)` gives source bytes (`vzip_source/bytes`),
/// for the columns read from arrays in the source.
pub fn load(t: &Table, read: &mut dyn FnMut(u64, u64) -> Result<Vec<u8>, String>) -> Result<Ir, String> {
    let m = t.kind.len();
    if [t.up.len(), t.name.len(), t.nidx.len(), t.ty.len(), t.space.len(), t.start.len(), t.length.len()].iter().any(|&x| x != m) {
        return Err("columns of different lengths".into());
    }
    let limit = (1u64 << 22) + t.size / 4;
    // expanded ids: member 0 of a column run is stored; members 1.. follow it
    let mut extra: HashMap<u64, (u64, u64, usize)> = HashMap::new(); // template root -> (count, size, crun)
    let mut total = m as u64;
    for (q, c) in t.cruns.iter().enumerate() {
        let (r, count, size) = (c[0], c[1], c[2]);
        if r < 0 || count < 1 || size < 1 {
            return Err(format!("column run {q}: bad (root, count, size) {c:?}"));
        }
        total = total.checked_add((count as u64 - 1).checked_mul(size as u64).ok_or("too many rows")?).ok_or("too many rows")?;
        if total > limit {
            return Err(format!("budget: more than {limit} rows"));
        }
        if extra.insert(r as u64, (count as u64, size as u64, q)).is_some() {
            return Err(format!("two column runs at row {r}"));
        }
    }
    let n = total as usize;
    let mut ir = Ir { size: t.size, budget: None, ..Default::default() };
    for s in &t.names {
        ir.names.id(s);
    }
    for s in &t.types {
        ir.types.id(s);
    }
    for s in &t.form_text {
        ir.forms.list.push(s.clone()); // forms may repeat: kept by position
    }
    ir.kind = vec![0; n];
    ir.parent = vec![NONE; n];
    ir.name = vec![0; n];
    ir.nidx = vec![NO_INDEX; n];
    ir.ty = vec![0; n];
    ir.space = vec![0; n];
    ir.start = vec![0; n];
    ir.len = vec![0; n];
    let names = t.names.len() as i64;
    let tys = t.types.len() as i64;
    // phase 1: the stored rows at their expanded ids
    let mut id = 0usize;
    let mut stored_at = Vec::with_capacity(m);
    let mut pending: Vec<(usize, u64, u64, usize)> = Vec::new(); // (template root, count, size, crun)
    let mut end = 0u64;
    for k in 0..m {
        // members 1.. of a column run whose template ended before this row
        while let Some(&(r, count, size, _)) = pending.last() {
            if id >= r + size as usize {
                id += ((count - 1) * size) as usize;
                pending.pop();
            } else {
                break;
            }
        }
        if id >= n {
            return Err("more stored rows than the table's size".into());
        }
        stored_at.push(id);
        ir.kind[id] = t.kind[k];
        if t.kind[k] > GAP {
            return Err(format!("row {id}: kind {}", t.kind[k]));
        }
        let up = t.up[k] as usize;
        if (id == 0) != (up == 0) || up > id {
            return Err(format!("row {id}: parent {}", id as i64 - up as i64));
        }
        ir.parent[id] = if id == 0 { NONE } else { (id - up) as u32 };
        if !(0..names.max(1)).contains(&(t.name[k] as i64)) || !(0..tys.max(1)).contains(&(t.ty[k] as i64)) {
            return Err(format!("row {id}: name or type out of range"));
        }
        ir.name[id] = t.name[k] as u32;
        ir.nidx[id] = t.nidx[k];
        ir.ty[id] = t.ty[k] as u32;
        ir.space[id] = t.space[k];
        let l = t.length[k];
        let s = if l > 0 { end.wrapping_add(t.start[k] as u64) } else { t.start[k] as u64 };
        if l > 0 {
            end = s.checked_add(l).ok_or("an extent past 2^64")?;
        }
        ir.start[id] = s;
        ir.len[id] = l;
        if let Some(&(count, size, q)) = extra.get(&(id as u64)) {
            pending.push((id, count, size, q));
        }
        id += 1;
    }
    while let Some((_, count, size, _)) = pending.pop() {
        id += ((count - 1) * size) as usize;
    }
    if id != n {
        return Err(format!("the table describes {id} rows, its column runs {n}"));
    }
    for r in &t.runs {
        if r[0] < 0 || r[0] as usize >= n || r[1] < 1 || r[2] < 0 {
            return Err(format!("bad run {r:?}"));
        }
        ir.runs.push((r[0] as u32, r[1] as u64, r[2] as u64));
    }
    ir.runs.sort();
    if ir.runs.windows(2).any(|w| w[0].0 == w[1].0) {
        return Err("two runs at one row".into());
    }
    for a in &t.targets {
        ir.targets.push((a[0] as u32, if a[1] < 0 { NONE } else { a[1] as u32 }));
    }
    for f in &t.forms {
        if f[0] < 0 || f[0] as usize >= n || f[1] < 0 || f[1] as usize >= t.form_text.len() {
            return Err(format!("bad form {f:?}"));
        }
        ir.form_of.push((f[0] as u32, f[1] as u32));
    }
    // phase 2: members 1.. of each column run
    let mut by_run: Vec<Vec<[i64; 6]>> = vec![Vec::new(); t.cruns.len()];
    for c in &t.columns {
        let q = usize::try_from(c[0]).ok().filter(|&q| q < t.cruns.len()).ok_or("a column of no column run")?;
        by_run[q].push(*c);
    }
    let mut arrays: HashMap<i64, Vec<u64>> = HashMap::new();
    ir.form_of.sort();
    ir.targets.sort();
    let (mut new_forms, mut new_targets) = (Vec::new(), Vec::new());
    // the runs that read no array first, then those that do (whose arrays are then known)
    let reads: Vec<bool> = by_run.iter().map(|cs| cs.iter().any(|c| c[3] == 2)).collect();
    let mut late = vec![false; n];
    for (q, c) in t.cruns.iter().enumerate() {
        if reads[q] {
            let (r, count, size) = (c[0] as usize, c[1] as usize, c[2] as usize);
            for x in late.iter_mut().skip(r).take(count * size) {
                *x = true;
            }
        }
    }
    let mut seq: Vec<usize> = (0..t.cruns.len()).filter(|&q| !reads[q]).collect();
    seq.extend((0..t.cruns.len()).filter(|&q| reads[q]));
    for q in seq {
        let c = &t.cruns[q];
        let (r, count, size) = (c[0] as usize, c[1] as u64, c[2] as usize);
        if r + size > n || ir.parent[r] == NONE {
            return Err(format!("column run {q} at row {r}"));
        }
        let outer = ir.parent[r];
        if (1..size).any(|off| {
            let p = ir.parent[r + off];
            p != outer && (p == NONE || (p as usize) < r || p as usize >= r + off)
        }) {
            return Err(format!("column run {q}: member 0 is not {size} rows of sibling subtrees"));
        }
        // each column's values
        let mut cols: Vec<(usize, i64, Vec<i64>)> = Vec::new();
        let mut starts_index: Vec<usize> = Vec::new();
        for col in &by_run[q] {
            let (off, f, enc, x, y) = (col[1] as usize, col[2], col[3], col[4], col[5]);
            if off >= size || !(0..=6).contains(&f) {
                return Err(format!("column run {q}: bad column {col:?}"));
            }
            if enc == 3 {
                if f != F_NIDX {
                    return Err(format!("column run {q}: encoding 3 of field {f}"));
                }
                starts_index.push(off);
                continue;
            }
            let v: Vec<i64> = match enc {
                0 => (0..count as i64).map(|j| x.wrapping_add(j.wrapping_mul(y))).collect(),
                1 => {
                    let a = usize::try_from(x).map_err(|_| "a column past its values")?;
                    let vals = t.column_values.get(a..a + count as usize).ok_or("a column past its values")?;
                    let mut acc = 0i64;
                    vals.iter().map(|d| { acc = acc.wrapping_add(*d); acc }).collect()
                }
                2 => {
                    if !arrays.contains_key(&x) {
                        let e = usize::try_from(x).ok().filter(|&e| e < n).ok_or("a column of no row")?;
                        if late[e] {
                            return Err("a column of a row in a column run that reads columns".into());
                        }
                        let (z, big) = unsigned_array(ir.types.get(ir.ty[e])).ok_or("a column of a row that is no unsigned array")?;
                        if ir.kind[e] != VALUE || ir.space[e] != 0 || ir.len[e] == 0 {
                            return Err("a column of a row that is no value in the source".into());
                        }
                        let raw = read(ir.start[e], ir.len[e])?;
                        arrays.insert(x, uints(&raw, z, big));
                    }
                    let a = &arrays[&x];
                    let y = usize::try_from(y).map_err(|_| "a column before its array")?;
                    a.get(y..y + count as usize).ok_or("a column past its array")?.iter().map(|&v| v as i64).collect()
                }
                _ => return Err(format!("column run {q}: encoding {enc}")),
            };
            cols.push((off, f, v));
        }
        for j in 1..count as usize {
            let base = r + j * size;
            for off in 0..size {
                let (src, dst) = (r + off, base + off);
                ir.kind[dst] = ir.kind[src];
                let p = ir.parent[src];
                ir.parent[dst] = if p == outer { p } else { (p as usize - r + base) as u32 };
                ir.name[dst] = ir.name[src];
                ir.nidx[dst] = ir.nidx[src];
                ir.ty[dst] = ir.ty[src];
                ir.space[dst] = ir.space[src];
                ir.start[dst] = ir.start[src];
                ir.len[dst] = ir.len[src];
            }
            let mut forms = Vec::new();
            let mut targets = Vec::new();
            for off in 0..size {
                if let Some(f) = ir.form_id((r + off) as u32) {
                    forms.push((base + off, f as i64));
                }
                let tg = ir.target((r + off) as u32);
                if tg != NONE {
                    targets.push((base + off, tg as i64));
                }
            }
            let mut rel: Vec<(usize, i64)> = Vec::new();
            for (off, f, v) in &cols {
                let dst = base + off;
                let x = v[j];
                match *f {
                    F_NIDX => ir.nidx[dst] = x as u64,
                    F_TYPE => {
                        if x < 0 || x >= tys {
                            return Err("a type column out of range".into());
                        }
                        ir.ty[dst] = x as u32
                    }
                    F_START => ir.start[dst] = x as u64,
                    F_LEN => ir.len[dst] = x as u64,
                    F_FORM if x >= t.form_text.len() as i64 => return Err("a form column out of range".into()),
                    F_FORM => match forms.iter_mut().find(|e| e.0 == dst) {
                        Some(e) => e.1 = x,
                        None => forms.push((dst, x)),
                    },
                    F_TARGET => match targets.iter_mut().find(|e| e.0 == dst) {
                        Some(e) => e.1 = x,
                        None => targets.push((dst, x)),
                    },
                    _ => rel.push((dst, x)),
                }
            }
            // starts relative to the member's first row; and those not in a column
            // follow the first row's shift from member 0
            let shift = ir.start[base].wrapping_sub(ir.start[r]);
            let relative: HashSet<usize> = cols.iter().filter(|c| c.1 == F_START || c.1 == F_REL).map(|c| base + c.0).collect();
            for off in 1..size {
                let dst = base + off;
                if !relative.contains(&dst) && ir.len[dst] > 0 && ir.len[base] > 0 {
                    ir.start[dst] = ir.start[r + off].wrapping_add(shift);
                }
            }
            for (dst, x) in rel {
                ir.start[dst] = ir.start[base].wrapping_add(x as u64);
            }
            for &off in &starts_index {
                ir.nidx[base + off] = ir.start[base + off];
            }
            new_forms.extend(forms.into_iter().filter(|f| f.1 >= 0).map(|(e, f)| (e as u32, f as u32)));
            new_targets.extend(targets.into_iter().filter(|t| t.1 >= 0).map(|(e, t)| (e as u32, t as u32)));
        }
    }
    ir.shared = t.shared.clone();
    ir.form_of.extend(new_forms);
    ir.targets.extend(new_targets);
    ir.form_of.sort();
    ir.targets.sort();
    ir.finished = true;
    ir.build_children();
    Ok(ir)
}

/// The stored table of a mirror written into `out`, for tools and tests that hold
/// the output rather than an archive.
pub fn table_from_out(out: &Out) -> Result<Table, String> {
    table_from(&|k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone()))
}

/// The stored table of a mirror, its entries given by `get(key)` (an archive's).
pub fn table_from(get: &dyn Fn(&str) -> Option<Vec<u8>>) -> Result<Table, String> {
    let doc = |p: &str| -> Result<J, String> {
        let key = if p.is_empty() { format!("{SOURCE_NODE}/zarr.json") } else { format!("{SOURCE_NODE}/{p}/zarr.json") };
        let b = get(&key).ok_or(format!("no {key}"))?;
        serde_json::from_slice(&b).map_err(|e| e.to_string())
    };
    let array = |p: &str| -> Result<Vec<u8>, String> {
        let meta = doc(p)?;
        let nums = |v: &J| -> Vec<u64> { v.as_array().map(|a| a.iter().map(|x| x.as_u64().unwrap_or(0)).collect()).unwrap_or_default() };
        let shape = nums(&meta["shape"]);
        let cs = nums(&meta["chunk_grid"]["configuration"]["chunk_shape"]);
        let item: u64 = if meta["data_type"] == "uint8" { 1 } else { 8 };
        let codecs = meta["codecs"].as_array().cloned().unwrap_or_default();
        let deflated = codecs.iter().any(|c| c["name"] == "zlib");
        if codecs.iter().any(|c| c["name"] != "zlib" && c["name"] != "bytes") {
            return Err(format!("{p}: a codec other than bytes and zlib"));
        }
        if shape.is_empty() || shape.len() > 2 || cs.len() != shape.len() || cs.contains(&0) {
            return Err(format!("{p}: a table array of shape {shape:?}"));
        }
        let total = shape.iter().try_fold(item, |a, &b| a.checked_mul(b)).filter(|&t| t <= 1 << 34).ok_or(format!("{p}: too large"))?;
        let mut out = vec![0u8; total as usize];
        let width = if shape.len() == 2 { shape[1] } else { 1 };
        let cw = if shape.len() == 2 { cs[1] } else { 1 };
        for g0 in 0..shape[0].div_ceil(cs[0]) {
            for g1 in 0..width.div_ceil(cw) {
                let coords = if shape.len() == 2 { format!("{g0}/{g1}") } else { format!("{g0}") };
                let key = format!("{SOURCE_NODE}/{p}/c/{coords}");
                let raw = get(&key).ok_or(format!("no chunk {key}"))?;
                let raw = if deflated {
                    miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(&raw, (cs[0] * cw * item) as usize).map_err(|e| format!("{key}: {e:?}"))?
                } else {
                    raw
                };
                if raw.len() as u64 != cs[0] * cw * item {
                    return Err(format!("{key}: {} bytes", raw.len()));
                }
                for r in 0..cs[0] {
                    let row = g0 * cs[0] + r;
                    for c in 0..cw {
                        let col = g1 * cw + c;
                        if row < shape[0] && col < width {
                            let src = ((r * cw + c) * item) as usize;
                            let dst = ((row * width + col) * item) as usize;
                            out[dst..dst + item as usize].copy_from_slice(&raw[src..src + item as usize]);
                        }
                    }
                }
            }
        }
        Ok(out)
    };
    let ints = |b: Vec<u8>| -> Vec<i64> { b.chunks(8).map(|c| i64::from_le_bytes(c.try_into().unwrap())).collect() };
    let node = doc("")?;
    let attrs = node["attributes"]["vzip_virtualized"].as_object().and_then(|m| m.values().next())
        .map(|v| &v["ir"]).ok_or("vzip_source declares no mirror")?;
    let strs = |k: &str| -> Result<Vec<String>, String> {
        attrs[k].as_array().ok_or(format!("no {k}"))?.iter()
            .map(|x| x.as_str().map(|s| s.to_string()).ok_or(format!("a non-string in {k}"))).collect()
    };
    let rows = ints(array("ir/rows")?);
    let m = rows.len() / 8;
    let col = |c: usize| &rows[c * m..(c + 1) * m];
    let mut t = Table {
        size: attrs["size"].as_u64().ok_or("no size")?,
        kind: col(0).iter().map(|&x| x as u8).collect(),
        up: col(1).iter().map(|&x| x as u32).collect(),
        name: col(2).iter().map(|&x| x as i32).collect(),
        nidx: col(3).iter().map(|&x| x as u64).collect(),
        ty: col(4).iter().map(|&x| x as i32).collect(),
        space: col(5).iter().map(|&x| x as u32).collect(),
        start: col(6).to_vec(),
        length: col(7).iter().map(|&x| x as u64).collect(),
        names: strs("names")?,
        types: strs("types")?,
        form_text: strs("forms")?,
        ..Default::default()
    };
    if col(0).iter().any(|&x| !(0..=255).contains(&x)) || col(1).iter().any(|&x| !(0..=u32::MAX as i64).contains(&x)) {
        return Err("a row's kind or parent out of range".into());
    }
    if get(&format!("{SOURCE_NODE}/ir/shared/zarr.json")).is_some() {
        let offsets = ints(array("ir/shared/offsets")?);
        let data = if offsets.last().is_some_and(|&x| x > 0) { array("ir/shared/data")? } else { Vec::new() };
        for w in offsets.windows(2) {
            let (a, b) = (usize::try_from(w[0]).map_err(|_| "a bad shared offset")?, usize::try_from(w[1]).map_err(|_| "a bad shared offset")?);
            t.shared.push(data.get(a..b).ok_or("a shared source past its data")?.to_vec());
        }
    }
    for r in ints(array("ir/tables")?).chunks(7) {
        match r[0] {
            T_RUN => t.runs.push([r[1], r[2], r[3]]),
            T_TARGET => t.targets.push([r[1], r[2]]),
            T_FORM => t.forms.push([r[1], r[2]]),
            T_CRUN => t.cruns.push([r[1], r[2], r[3]]),
            T_COLUMN => t.columns.push([r[1], r[2], r[3], r[4], r[5], r[6]]),
            T_VALUES => t.column_values.extend_from_slice(&r[1..]),
            x => return Err(format!("a table row of kind {x}")),
        }
    }
    Ok(t)
}


/// What makes a stored mirror non-canonical (conventions §8.8), or None when it is
/// canonical: the first of a run (code 0), a compressed chunk, unsorted strings, rows
/// out of canonical order, and a table that differs from the one [`table`] folds the
/// loaded IR into (entry by entry). `get(key)` gives the archive's entries, `read`
/// the source's bytes (for the arrays encoding-2 columns read). A table that is not
/// valid at all is a problem too.
pub fn canonical_problem(get: &dyn Fn(&str) -> Option<Vec<u8>>, read: &mut dyn FnMut(u64, u64) -> Result<Vec<u8>, String>)
                         -> Option<String> {
    let t = match table_from(get) {
        Ok(t) => t,
        Err(e) => return Some(format!("invalid: {e}")),
    };
    let profile = stored_profile(get);
    if !t.runs.is_empty() {
        return Some("a run (table code 0): the canonical table expands every run".into());
    }
    let docs = ["ir/rows", "ir/tables", "ir/shared/offsets", "ir/shared/data"];
    for p in docs {
        if let Some(b) = get(&format!("{SOURCE_NODE}/{p}/zarr.json")) {
            let doc: J = serde_json::from_slice(&b).unwrap_or(J::Null);
            if doc["codecs"].as_array().is_none_or(|c| c.len() != 1) {
                return Some(format!("{p}: compressed chunks (the canonical table's chunks are raw)"));
            }
        }
    }
    for (what, list) in [("names", &t.names), ("types", &t.types), ("forms", &t.form_text)] {
        if list.windows(2).any(|w| w[0].as_bytes() >= w[1].as_bytes()) {
            return Some(format!("the {what} are not sorted (by their bytes, each once)"));
        }
    }
    let back = match load(&t, read) {
        Ok(ir) => ir,
        Err(e) => return Some(format!("invalid: {e}")),
    };
    let (ord, _) = crate::canon::order(&back);
    if let Some(k) = ord.iter().enumerate().position(|(k, &i)| k as u32 != i) {
        return Some(format!("row {k} is out of canonical order"));
    }
    let y = match canonical(&back) {
        Ok(y) => y,
        Err(e) => return Some(format!("invalid: {e}")),
    };
    if y.len() != back.len() {
        return Some(format!("the table keeps {} rows of derived spaces past the budget", back.len() - y.len()));
    }
    let src = std::cell::RefCell::new(read);
    let array = |e: u32| -> Option<Vec<u8>> {
        let eu = e as usize;
        (y.space[eu] == 0 && y.len[eu] > 0).then(|| (src.borrow_mut())(y.start[eu], y.len[eu]).ok()).flatten()
    };
    let (c, _) = table(&y, &profile, &array);
    if (&c.names, &c.types, &c.form_text) != (&t.names, &t.types, &t.form_text) {
        return Some("the interned strings are not the ones the rows use".into());
    }
    let rows = |t: &Table| -> Vec<[i64; 8]> {
        (0..t.kind.len()).map(|k| [t.kind[k] as i64, t.up[k] as i64, t.name[k] as i64, t.nidx[k] as i64, t.ty[k] as i64,
                                    t.space[k] as i64, t.start[k], t.length[k] as i64]).collect()
    };
    let (a, b) = (rows(&t), rows(&c));
    if let Some(k) = (0..a.len().max(b.len())).find(|&k| a.get(k) != b.get(k)) {
        return Some(format!("stored row {k} is {:?} where the canonical table has {:?}", a.get(k), b.get(k)));
    }
    let tables = |t: &Table| -> Vec<Vec<i64>> {
        let mut v: Vec<Vec<i64>> = Vec::new();
        v.extend(t.targets.iter().map(|r| vec![T_TARGET, r[0], r[1]]));
        v.extend(t.forms.iter().map(|r| vec![T_FORM, r[0], r[1]]));
        v.extend(t.cruns.iter().map(|r| vec![T_CRUN, r[0], r[1], r[2]]));
        v.extend(t.columns.iter().map(|r| [vec![T_COLUMN], r.to_vec()].concat()));
        let mut values = t.column_values.clone();
        values.resize(values.len().div_ceil(6) * 6, 0);
        v.push([vec![T_VALUES], values].concat());
        v
    };
    let (a, b) = (tables(&t), tables(&c));
    if let Some(k) = (0..a.len().max(b.len())).find(|&k| a.get(k) != b.get(k)) {
        return Some(format!("table row {k} is {:?} where the canonical table has {:?}", a.get(k), b.get(k)));
    }
    if t.shared != c.shared {
        return Some("the shared sources differ from the canonical table's".into());
    }
    // entry for entry: the description and the arrays' documents and chunks
    let mut out = Out::new("");
    write(&c, &mut out, &profile, false);
    for (k, v) in &out.entries {
        let stored = get(k);
        let same = match (k.ends_with("zarr.json"), &stored) {
            (true, Some(s)) => serde_json::from_slice::<J>(s).ok() == serde_json::from_slice::<J>(v).ok(),
            (false, Some(s)) => s == v,
            _ => false,
        };
        if !same {
            return Some(format!("{k} differs from the canonical table's"));
        }
    }
    None
}

/// The bytes of a derived element's space, from the source (conventions §8.5, §8.8): its
/// form's transform (`transform.rs`) applied to its extent.
fn derived_bytes(y: &Ir, d: u32, read: &mut dyn FnMut(u64, u64) -> Result<Vec<u8>, String>) -> Result<Vec<u8>, String> {
    let du = d as usize;
    let form = y.form(d).unwrap_or("");
    let t = crate::transform::Transform::of_form(form).ok_or(format!("a derived element of an unknown transform: {form}"))?;
    let raw = read(y.start[du], y.len[du])?;
    let form: J = serde_json::from_str(form).unwrap_or(J::Null);
    t.rederive(&raw, &form).unwrap_or_else(|| Err(format!("a value the view shows in a space of transform {}, which a validator cannot derive", t.name())))
}

/// Reads the ranges (offset, length) through `read`, merged when at most 64 KiB apart:
/// each range's bytes.
fn read_merged(ranges: &[(u64, u64)], read: &mut dyn FnMut(u64, u64) -> Result<Vec<u8>, String>)
               -> Result<HashMap<(u64, u64), Vec<u8>>, String> {
    let mut sorted: Vec<(u64, u64)> = ranges.to_vec();
    sorted.sort_unstable();
    sorted.dedup();
    let mut out = HashMap::new();
    let mut k = 0;
    while k < sorted.len() {
        let (a, mut end) = (sorted[k].0, sorted[k].0 + sorted[k].1);
        let mut j = k + 1;
        while j < sorted.len() && sorted[j].0 <= end + (1 << 16) {
            end = end.max(sorted[j].0 + sorted[j].1);
            j += 1;
        }
        let b = read(a, end - a)?;
        if b.len() as u64 != end - a {
            return Err(format!("the source gave {} bytes for [{a}, {end})", b.len()));
        }
        for &(o, n) in &sorted[k..j] {
            out.insert((o, n), b[(o - a) as usize..(o - a + n) as usize].to_vec());
        }
        k = j;
    }
    Ok(out)
}

/// The view a stored mirror's table gives (conventions §8.7): the canonical IR it
/// loads, every value the view shows read from the source (a derived space's from its
/// transform, an `xml` value with its references decoded), rendered.
pub fn rebuilt_view(get: &dyn Fn(&str) -> Option<Vec<u8>>, read: &mut dyn FnMut(u64, u64) -> Result<Vec<u8>, String>)
                    -> Result<Out, String> {
    let t = table_from(get)?;
    let profile = stored_profile(get);
    let back = load(&t, read)?;
    let mut y = canonical(&back)?;
    y.values.clear();
    y.vbytes.clear();
    let shown_rows: Vec<u32> = (0..y.len() as u32).filter(|&i| shown(&y, &profile, i)).collect();
    let mut want: Vec<(u64, u64)> = Vec::new();
    let mut spaces: Vec<u32> = Vec::new();
    for &i in &shown_rows {
        let iu = i as usize;
        if y.space[iu] == 0 {
            want.push((y.start[iu], y.len[iu]));
        } else {
            spaces.push(y.space[iu]);
        }
    }
    spaces.sort_unstable();
    spaces.dedup();
    let got = read_merged(&want, read)?;
    let mut derived: HashMap<u32, Vec<u8>> = HashMap::new();
    for d in spaces {
        derived.insert(d, derived_bytes(&y, d, read)?);
    }
    for &i in &shown_rows {
        let iu = i as usize;
        let (s, n) = (y.start[iu], y.len[iu]);
        let raw = if y.space[iu] == 0 {
            got[&(s, n)].clone()
        } else {
            let b = &derived[&y.space[iu]];
            let r = b.get(s as usize..(s + n) as usize).ok_or("a value past its derived space")?.to_vec();
            if matches!(types::parse(y.types.get(y.ty[iu])), Ok(Ty::Xml)) {
                crate::xml::decode_refs(&String::from_utf8_lossy(&r)).into_bytes()
            } else {
                r
            }
        };
        y.put_value(i, &raw);
    }
    y.sort_values();
    let mut out = Out::new("");
    View::new(&y, &profile).tree(&mut out);
    Ok(out)
}

/// Where a stored view differs from the one its table and source give (the
/// validator's check, conventions §8.7), or None: the first group missing or extra,
/// then, group by group, the first member (a path of keys) missing, extra or other.
/// `keys` are the archive's entry names.
pub fn view_problem(keys: &[String], get: &dyn Fn(&str) -> Option<Vec<u8>>,
                    read: &mut dyn FnMut(u64, u64) -> Result<Vec<u8>, String>) -> Option<String> {
    let out = match rebuilt_view(get, read) {
        Ok(o) => o,
        Err(e) => return Some(format!("view: cannot rebuild it: {e}")),
    };
    let prefix = format!("{SOURCE_NODE}/tree");
    let profile = stored_profile(get);
    let inside = |k: &str| k == format!("{prefix}/zarr.json") || k.starts_with(&format!("{prefix}/")) && !k.starts_with(&format!("{SOURCE_NODE}/ir/"));
    let rebuilt: HashMap<&str, &Vec<u8>> = out.entries.iter().filter(|e| inside(&e.0)).map(|e| (e.0.as_str(), &e.1)).collect();
    let mut names: Vec<&str> = rebuilt.keys().copied().collect();
    names.sort_unstable();
    for k in &names {
        let group = k.trim_end_matches("/zarr.json").trim_start_matches(&format!("{SOURCE_NODE}/"));
        let Some(stored) = get(k) else { return Some(format!("view: group {group} is missing")) };
        let (a, b): (J, J) = (serde_json::from_slice(&stored).unwrap_or(J::Null), serde_json::from_slice(rebuilt[k]).unwrap_or(J::Null));
        if a == b {
            continue;
        }
        let doc = |x: &J| x["attributes"]["vzip_virtualized"][profile.as_str()].clone();
        if let Some(d) = first_difference(&doc(&a), &doc(&b), String::new()) {
            return Some(format!("view {group}: {d}"));
        }
        return Some(format!("view {group}: the group's document differs"));
    }
    let mut extra: Vec<&String> = keys.iter().filter(|k| inside(k) && k.ends_with("zarr.json") && !rebuilt.contains_key(k.as_str())).collect();
    extra.sort();
    if let Some(k) = extra.first() {
        return Some(format!("view: group {} is not in the rebuilt view", k.trim_end_matches("/zarr.json").trim_start_matches(&format!("{SOURCE_NODE}/"))));
    }
    None
}

/// The first member (by its path of keys) where `a` (stored) and `b` (rebuilt) differ.
fn first_difference(a: &J, b: &J, path: String) -> Option<String> {
    let short = |x: &J| {
        let s = x.to_string();
        if s.len() > 200 { format!("{}…", &s[..s.char_indices().take_while(|(i, _)| *i < 200).last().map_or(0, |(i, c)| i + c.len_utf8())]) } else { s }
    };
    match (a, b) {
        (J::Object(x), J::Object(y)) if x.get("$vz").is_none() && y.get("$vz").is_none() || x.get("$vz") == y.get("$vz") && x.get("$vz") == Some(&json!("derived")) => {
            for (k, v) in y {
                let p = format!("{path}[{}]", J::String(k.clone()));
                match x.get(k) {
                    None => return Some(format!("member {p} is missing")),
                    Some(w) if w != v => return first_difference(w, v, p),
                    _ => {}
                }
            }
            x.keys().find(|k| !y.contains_key(*k)).map(|k| format!("member {path}[{}] is not in the rebuilt view", J::String(k.clone())))
        }
        _ if a == b => None,
        _ => Some(format!("member {path} is {} where the rebuilt view has {}", short(a), short(b))),
    }
}

/// The profile whose convention `vzip_source` declares.
fn stored_profile(get: &dyn Fn(&str) -> Option<Vec<u8>>) -> String {
    get(&format!("{SOURCE_NODE}/zarr.json"))
        .and_then(|b| serde_json::from_slice::<J>(&b).ok())
        .and_then(|d| d["attributes"]["vzip_virtualized"].as_object().and_then(|m| m.keys().next().cloned()))
        .unwrap_or_default()
}

/// The rows of the canonical IR of `ir` (what a mirror's table loads back to): (kind,
/// parent, name, name index, type, space, start, length) per row, the alias targets
/// and the forms (as text), for comparing a loaded table with the IR it was written from.
#[allow(clippy::type_complexity)]
pub fn ordered(ir: &Ir) -> (Vec<(u8, u32, String, u64, String, u32, u64, u64)>, Vec<(u32, u32)>, Vec<(u32, String)>) {
    let y = canonical(ir).expect("a canonical IR");
    let rows = (0..y.len()).map(|i| {
        (y.kind[i], y.parent[i], y.names.get(y.name[i]).to_string(), y.nidx[i], y.types.get(y.ty[i]).to_string(), y.space[i], y.start[i], y.len[i])
    }).collect();
    let forms = y.form_of.iter().map(|&(e, f)| (e, y.forms.get(f).to_string())).collect();
    (rows, y.targets.clone(), forms)
}
