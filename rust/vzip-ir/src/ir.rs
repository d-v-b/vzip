//! The intermediate representation (ARCHITECTURE.md §3), held as columns.
//!
//! An element is one row of dense columns (kind, parent, name, name index, type,
//! space, extent) plus sparse side tables, sorted by element id, for what few
//! elements carry: decoded value bytes, a form (the geometry, codec, recipe or
//! transform of a `data` or `derived` element), an alias target, and runs.
//!
//! **Runs.** A run is one subtree that stands for `count` identically shaped
//! subtrees, member `j` being member 0 with every extent shifted by `j × stride`
//! and its root's name index increased by `j`. Only the run's root carries the
//! run; its descendants belong to it. A parser does not build runs: it emits
//! subtrees one by one and calls [`Ir::fold`] after each, which folds the new
//! subtree into its previous sibling when the two have the same shape (kinds,
//! names, types, lengths, values and forms) at a constant stride, so a run costs
//! one subtree whatever its count. Runs do not nest, and hold no derived content.
//!
//! **Spaces.** An element's extent is in the source (space 0) or, under a
//! `derived` element `d`, in `d`'s derived bytes (space `d`).

use std::collections::HashMap;

pub const STRUCT: u8 = 0;
pub const VALUE: u8 = 1;
pub const DATA: u8 = 2;
pub const DERIVED: u8 = 3;
pub const ALIAS: u8 = 4;
pub const GAP: u8 = 5;
pub const KINDS: [&str; 6] = ["struct", "value", "data", "derived", "alias", "gap"];
pub const NONE: u32 = u32::MAX;
pub const NO_INDEX: u64 = u64::MAX;

pub fn is_leaf(kind: u8) -> bool {
    matches!(kind, VALUE | DATA | DERIVED | GAP)
}

/// `%`, `/` and `~` of a source text, percent-encoded (`%25`, `%2F`, `%7E`).
fn escape_name(t: &str) -> String {
    let mut s = String::with_capacity(t.len());
    for c in t.chars() {
        match c {
            '%' => s.push_str("%25"),
            '/' => s.push_str("%2F"),
            '~' => s.push_str("%7E"),
            c => s.push(c),
        }
    }
    s
}

/// The (name, name index) of siblings named by texts the source gives (an ND2
/// chunk's, LV record's or XML element's name; conventions/nd2 §5.3), in order:
/// a text that is not empty, holds no `/` or `~`, is none of the parent's `fixed`
/// names and is the first of its siblings' with that text is the name itself, with
/// no index; any other is the text with `%`, `/` and `~` percent-encoded, then `~`,
/// with the number of earlier siblings of that text as its index. So no full name
/// is empty, none repeats, and none holds `/`.
pub fn source_names<S: AsRef<str>>(texts: &[S], fixed: &[&str]) -> Vec<(String, u64)> {
    let mut seen: HashMap<&str, u64> = HashMap::new();
    texts
        .iter()
        .map(|t| {
            let t = t.as_ref();
            let k = seen.entry(t).or_insert(0);
            let earlier = *k;
            *k += 1;
            let plain = !t.is_empty() && !t.contains(['/', '~']) && !fixed.contains(&t);
            if plain && earlier == 0 {
                (t.to_string(), NO_INDEX)
            } else {
                (format!("{}~", escape_name(t)), earlier)
            }
        })
        .collect()
}

/// The source text of a name [`source_names`] gave: the inverse of its rule.
pub fn source_text(name: &str, nidx: u64) -> String {
    let Some(e) = name.strip_suffix('~').filter(|_| nidx != NO_INDEX) else {
        return name.to_string();
    };
    let mut out = Vec::with_capacity(e.len());
    let b = e.as_bytes();
    let mut k = 0;
    while k < b.len() {
        let code = if b[k] == b'%' { b.get(k + 1..k + 3) } else { None };
        match code {
            Some(b"25") => out.push(b'%'),
            Some(b"2F") => out.push(b'/'),
            Some(b"7E") => out.push(b'~'),
            _ => {
                out.push(b[k]);
                k += 1;
                continue;
            }
        }
        k += 3;
    }
    String::from_utf8(out).unwrap_or_else(|_| e.to_string())
}

/// Strings stored once: names, types, forms.
#[derive(Default, Clone)]
pub struct Interner {
    pub map: HashMap<String, u32>,
    pub list: Vec<String>,
}

impl Interner {
    pub fn id(&mut self, s: &str) -> u32 {
        if let Some(&i) = self.map.get(s) {
            return i;
        }
        let i = self.list.len() as u32;
        self.list.push(s.to_string());
        self.map.insert(s.to_string(), i);
        i
    }
    pub fn get(&self, i: u32) -> &str {
        &self.list[i as usize]
    }
    pub fn bytes(&self) -> usize {
        self.list.iter().map(|s| s.len() + 48).sum::<usize>()
    }
}

/// The resource limits of one run (ARCHITECTURE.md §3.5): exceeding one rejects.
#[derive(Clone, Debug)]
pub struct Budget {
    pub records: u64,
    pub elements: u64,
    pub read: u64,
    pub reads: u64,
    pub spent_records: u64,
    pub spent_elements: u64,
    pub spent_read: u64,
    pub spent_reads: u64,
}

impl Budget {
    pub fn new(size: u64) -> Self {
        Budget {
            // physical rows: one per 4 bytes of the source, at most, past 2^22
            records: (1 << 22) + size / 4,
            // rows with every run expanded: the elements a source describes
            elements: (1 << 30) + size,
            read: (1 << 24) + 2 * size,
            reads: (1 << 20) + size / 64,
            spent_records: 0,
            spent_elements: 0,
            spent_read: 0,
            spent_reads: 0,
        }
    }
}

#[derive(Default, Clone)]
pub struct Ir {
    pub size: u64,
    pub kind: Vec<u8>,
    pub parent: Vec<u32>,
    pub name: Vec<u32>,
    pub nidx: Vec<u64>,
    pub ty: Vec<u32>,
    pub space: Vec<u32>,
    pub start: Vec<u64>,
    pub len: Vec<u64>,
    /// (element, offset in `vbytes`, length): decoded value bytes
    pub values: Vec<(u32, u64, u32)>,
    pub vbytes: Vec<u8>,
    /// (element, form id)
    pub form_of: Vec<(u32, u32)>,
    /// (element, target): aliases
    pub targets: Vec<(u32, u32)>,
    /// (element, kind it had): the aliases finish() made
    pub was: Vec<(u32, u8)>,
    /// (run root, count, stride)
    pub runs: Vec<(u32, u64, u64)>,
    pub names: Interner,
    pub types: Interner,
    pub forms: Interner,
    /// shared data sources a recipe names (`["shared", k]`), stored once each
    pub shared: Vec<Vec<u8>>,
    pub shared_index: HashMap<Vec<u8>, u32>,
    pub finished: bool,
    pub budget: Option<Budget>,
    /// children, built by finish(): CSR over parents
    pub kid_index: Vec<u32>,
    pub kids: Vec<u32>,
}

fn find<T: Copy>(table: &[(u32, T)], i: u32) -> Option<T> {
    table
        .binary_search_by_key(&i, |e| e.0)
        .ok()
        .map(|k| table[k].1)
}

pub type Res<T> = Result<T, String>;

impl Ir {
    pub fn new(size: u64) -> Self {
        let mut ir = Ir {
            size,
            budget: Some(Budget::new(size)),
            ..Default::default()
        };
        ir.types.id(""); // type 0: none
        // the root (conventions §8.1): a struct named "", no index, spanning the source
        ir.add(STRUCT, NONE, "", NO_INDEX, "", 0, Some((0, size)))
            .unwrap();
        ir
    }

    pub fn len(&self) -> usize {
        self.kind.len()
    }

    pub fn is_empty(&self) -> bool {
        self.kind.is_empty()
    }

    // ---- emission

    pub fn add(
        &mut self,
        kind: u8,
        parent: u32,
        name: &str,
        nidx: u64,
        ty: &str,
        space: u32,
        ext: Option<(u64, u64)>,
    ) -> Res<u32> {
        if self.finished {
            return Err("the IR is finished".into());
        }
        if let Some(b) = self.budget.as_mut() {
            b.spent_records += 1;
            b.spent_elements += 1;
            if b.spent_records > b.records {
                return Err(format!("budget: more than {} IR records", b.records));
            }
            if b.spent_elements > b.elements {
                return Err(format!("budget: more than {} IR elements", b.elements));
            }
        }
        let i = self.kind.len() as u32;
        let name = self.names.id(name);
        let ty = self.types.id(ty);
        self.kind.push(kind);
        self.parent.push(parent);
        self.name.push(name);
        self.nidx.push(nidx);
        self.ty.push(ty);
        self.space.push(space);
        let (o, n) = ext.unwrap_or((0, 0));
        self.start.push(o);
        self.len.push(n);
        Ok(i)
    }

    pub fn struct_(
        &mut self,
        parent: u32,
        name: &str,
        nidx: u64,
        space: u32,
        env: Option<(u64, u64)>,
    ) -> Res<u32> {
        self.add(STRUCT, parent, name, nidx, "", space, env)
    }

    pub fn value(
        &mut self,
        parent: u32,
        name: &str,
        nidx: u64,
        ty: &str,
        space: u32,
        ext: (u64, u64),
        raw: Option<&[u8]>,
    ) -> Res<u32> {
        let i = self.add(VALUE, parent, name, nidx, ty, space, Some(ext))?;
        if let Some(r) = raw {
            self.put_value(i, r);
        }
        Ok(i)
    }

    /// A shared data source for recipes: its index (the same bytes, the same index).
    pub fn share(&mut self, b: &[u8]) -> u32 {
        if let Some(&k) = self.shared_index.get(b) {
            return k;
        }
        let k = self.shared.len() as u32;
        self.shared.push(b.to_vec());
        self.shared_index.insert(b.to_vec(), k);
        k
    }

    /// Element `i` decodes the bytes element `from` holds (one copy for both).
    pub fn put_value_of(&mut self, i: u32, from: u32) -> bool {
        let Ok(k) = self.values.binary_search_by_key(&from, |e| e.0) else {
            return false;
        };
        let (_, o, n) = self.values[k];
        self.values.push((i, o, n));
        true
    }

    /// The value table sorted by element again, after values put out of order.
    pub fn sort_values(&mut self) {
        if !self.values.windows(2).all(|w| w[0].0 < w[1].0) {
            self.values.sort_by_key(|e| e.0);
            self.values.dedup_by_key(|e| e.0);
        }
    }

    /// A 64-bit FNV-1a digest of the IR's rows (kinds, parents, names, indexes,
    /// types, spaces, extents, runs, aliases, values, forms and shared sources):
    /// two builds that agree on it built the same IR.
    pub fn digest(&self) -> u64 {
        let mut h: u64 = 0xcbf29ce484222325;
        let mut eat = |b: &[u8]| {
            for &x in b {
                h ^= x as u64;
                h = h.wrapping_mul(0x100000001b3);
            }
        };
        for i in 0..self.len() {
            eat(&[self.kind[i]]);
            eat(&self.parent[i].to_le_bytes());
            eat(self.names.get(self.name[i]).as_bytes());
            eat(&self.nidx[i].to_le_bytes());
            eat(self.types.get(self.ty[i]).as_bytes());
            eat(&self.space[i].to_le_bytes());
            eat(&self.start[i].to_le_bytes());
            eat(&self.len[i].to_le_bytes());
        }
        for &(r, c, s) in &self.runs {
            eat(&r.to_le_bytes());
            eat(&c.to_le_bytes());
            eat(&s.to_le_bytes());
        }
        for &(a, t) in &self.targets {
            eat(&a.to_le_bytes());
            eat(&t.to_le_bytes());
        }
        for &(i, o, n) in &self.values {
            eat(&i.to_le_bytes());
            eat(&self.vbytes[o as usize..o as usize + n as usize]);
        }
        for &(i, f) in &self.form_of {
            eat(&i.to_le_bytes());
            eat(self.forms.get(f).as_bytes());
        }
        for b in &self.shared {
            eat(b);
        }
        h
    }

    pub fn put_value(&mut self, i: u32, raw: &[u8]) {
        self.values
            .push((i, self.vbytes.len() as u64, raw.len() as u32));
        self.vbytes.extend_from_slice(raw);
    }

    pub fn data(
        &mut self,
        parent: u32,
        name: &str,
        nidx: u64,
        space: u32,
        ext: (u64, u64),
        form: &str,
    ) -> Res<u32> {
        let i = self.add(DATA, parent, name, nidx, "", space, Some(ext))?;
        let f = self.forms.id(form);
        self.form_of.push((i, f));
        Ok(i)
    }

    pub fn derived(
        &mut self,
        parent: u32,
        name: &str,
        space: u32,
        ext: (u64, u64),
        form: &str,
    ) -> Res<u32> {
        let i = self.add(DERIVED, parent, name, NO_INDEX, "", space, Some(ext))?;
        let f = self.forms.id(form);
        self.form_of.push((i, f));
        Ok(i)
    }

    /// A second name for element `target` (an earlier one): it claims nothing.
    pub fn alias(&mut self, parent: u32, name: &str, nidx: u64, target: u32) -> Res<u32> {
        let i = self.add(ALIAS, parent, name, nidx, "", 0, None)?;
        let at = self.targets.partition_point(|x| x.0 < i);
        self.targets.insert(at, (i, target));
        Ok(i)
    }

    /// The data members of `p`: its `data` children (and the aliases `finish`
    /// made of data), runs expanded, as (name index, start, length, form).
    pub fn members(&self, p: u32) -> Vec<(u64, u64, u64, u32)> {
        let mut out = Vec::new();
        for &c in self.children(p) {
            let k = self.kind[c as usize];
            if !(k == DATA || (k == ALIAS && self.was_kind(c) == Some(DATA))) {
                continue;
            }
            let (count, stride) = self.run(c).unwrap_or((1, 0));
            let f = self.form_id(c).unwrap_or(u32::MAX);
            let (o, n, x) = (self.start[c as usize], self.len[c as usize], self.nidx[c as usize]);
            for j in 0..count {
                out.push((x.wrapping_add(j), o + j * stride, n, f));
            }
        }
        out
    }

    pub fn gap(&mut self, parent: u32, name: &str, ext: (u64, u64)) -> Res<u32> {
        self.add(GAP, parent, name, NO_INDEX, "", 0, Some(ext))
    }

    /// A mark to roll back to (an attempt that failed, such as a decode that does not keep).
    pub fn mark(&self) -> usize {
        self.kind.len()
    }

    pub fn rollback(&mut self, mark: usize) {
        let m = mark as u32;
        let removed = (self.kind.len() - mark) as u64;
        self.kind.truncate(mark);
        self.parent.truncate(mark);
        self.name.truncate(mark);
        self.nidx.truncate(mark);
        self.ty.truncate(mark);
        self.space.truncate(mark);
        self.start.truncate(mark);
        self.len.truncate(mark);
        while self.values.last().is_some_and(|v| v.0 >= m) {
            let (_, off, _) = self.values.pop().unwrap();
            self.vbytes.truncate(off as usize);
        }
        while self.form_of.last().is_some_and(|v| v.0 >= m) {
            self.form_of.pop();
        }
        while self.targets.last().is_some_and(|v| v.0 >= m) {
            self.targets.pop();
        }
        while self.runs.last().is_some_and(|v| v.0 >= m) {
            self.runs.pop();
        }
        if let Some(b) = self.budget.as_mut() {
            b.spent_records -= removed;
            b.spent_elements -= removed;
        }
    }

    pub fn run(&self, i: u32) -> Option<(u64, u64)> {
        self.runs
            .binary_search_by_key(&i, |e| e.0)
            .ok()
            .map(|k| (self.runs[k].1, self.runs[k].2))
    }

    pub fn value_bytes(&self, i: u32) -> Option<&[u8]> {
        self.values.binary_search_by_key(&i, |e| e.0).ok().map(|k| {
            let (_, o, n) = self.values[k];
            &self.vbytes[o as usize..o as usize + n as usize]
        })
    }

    pub fn form(&self, i: u32) -> Option<&str> {
        find(&self.form_of, i).map(|f| self.forms.get(f))
    }

    pub fn form_id(&self, i: u32) -> Option<u32> {
        find(&self.form_of, i)
    }

    pub fn target(&self, i: u32) -> u32 {
        find(&self.targets, i).unwrap_or(NONE)
    }

    pub fn was_kind(&self, i: u32) -> Option<u8> {
        find(&self.was, i)
    }

    /// The end (exclusive) of the subtree rooted at `r`, whose ids are contiguous.
    pub fn subtree_end(&self, r: u32) -> u32 {
        let n = self.len() as u32;
        let mut k = r + 1;
        while k < n
            && self.parent[k as usize] != NONE
            && self.parent[k as usize] >= r
            && self.parent[k as usize] < k
        {
            k += 1;
        }
        k
    }

    /// Folds the subtree just emitted at `new` into its previous sibling `prev`
    /// (whose subtree ends where `new` starts) when the two have the same shape at
    /// a constant stride: `new` is then removed and `prev`'s run grows by one.
    pub fn fold(&mut self, prev: u32, new: u32) -> bool {
        let n = self.len() as u32;
        if prev >= new || new >= n || self.parent[prev as usize] != self.parent[new as usize] {
            return false;
        }
        let size = new - prev;
        if n - new != size || self.subtree_end(prev) != new || self.subtree_end(new) != n {
            return false;
        }
        let (count, stride) = self.run(prev).unwrap_or((1, 0));
        let (p, q) = (prev as usize, new as usize);
        if self.name[p] != self.name[q]
            || self.nidx[p] == NO_INDEX
            || self.nidx[q] != self.nidx[p].wrapping_add(count)
        {
            return false;
        }
        let mut delta: Option<u64> = None;
        let (mut lo, mut hi) = (u64::MAX, 0u64);
        // the sparse rows of the two subtrees, in order
        let vs = |t: &[(u32, u64, u32)], a: u32, b: u32| -> Vec<(u32, u64, u32)> {
            let s = t.partition_point(|e| e.0 < a);
            t[s..].iter().take_while(|e| e.0 < b).copied().collect()
        };
        let va = vs(&self.values, prev, new);
        let vb = vs(&self.values, new, n);
        if va.len() != vb.len() {
            return false;
        }
        for (x, y) in va.iter().zip(&vb) {
            if x.0 - prev != y.0 - new || x.2 != y.2 {
                return false;
            }
            let ra = &self.vbytes[x.1 as usize..x.1 as usize + x.2 as usize];
            let rb = &self.vbytes[y.1 as usize..y.1 as usize + y.2 as usize];
            if ra != rb {
                return false;
            }
        }
        let fs = |a: u32, b: u32| -> Vec<(u32, u32)> {
            let s = self.form_of.partition_point(|e| e.0 < a);
            self.form_of[s..]
                .iter()
                .take_while(|e| e.0 < b)
                .copied()
                .collect()
        };
        let fa = fs(prev, new);
        let fb = fs(new, n);
        if fa.len() != fb.len()
            || fa
                .iter()
                .zip(&fb)
                .any(|(x, y)| x.0 - prev != y.0 - new || x.1 != y.1)
        {
            return false;
        }
        for k in 0..size as usize {
            let (a, b) = (p + k, q + k);
            if self.kind[a] != self.kind[b]
                || self.ty[a] != self.ty[b]
                || self.len[a] != self.len[b]
                || self.space[a] != 0
                || self.space[b] != 0
                || self.kind[a] == DERIVED
                || self.kind[a] == ALIAS
            {
                return false;
            }
            if k > 0 {
                if self.name[a] != self.name[b]
                    || self.nidx[a] != self.nidx[b]
                    || self.parent[a] - prev != self.parent[b] - new
                    || self.run(a as u32).is_some()
                    || self.run(b as u32).is_some()
                {
                    return false;
                }
            }
            if self.len[a] > 0 {
                if self.start[b] < self.start[a] {
                    return false;
                }
                let d = self.start[b] - self.start[a];
                if delta.is_some_and(|x| x != d) {
                    return false;
                }
                delta = Some(d);
                lo = lo.min(self.start[a]);
                hi = hi.max(self.start[a] + self.len[a]);
            }
        }
        let Some(d) = delta else { return false };
        if d == 0 {
            return false;
        }
        if count == 1 {
            if hi > lo && d < hi - lo {
                return false; // members would overlap one another
            }
        } else if d != count * stride {
            return false;
        }
        let new_stride = if count == 1 { d } else { stride };
        self.rollback(new as usize);
        match self.runs.binary_search_by_key(&prev, |e| e.0) {
            Ok(k) => self.runs[k].1 = count + 1,
            Err(k) => self.runs.insert(k, (prev, 2, new_stride)),
        }
        if let Some(b) = self.budget.as_mut() {
            b.spent_elements += size as u64;
            if b.spent_elements > b.elements {
                // the caller sees the overrun at its next emission
                b.spent_records = b.records;
            }
        }
        true
    }

    // ---- reading

    /// The element's full name (conventions §8.1): its name followed by its name
    /// index in decimal, if it has one.
    pub fn name_of(&self, i: u32) -> String {
        let s = self.names.get(self.name[i as usize]);
        match self.nidx[i as usize] {
            NO_INDEX => s.to_string(),
            k => format!("{s}{k}"),
        }
    }

    /// The element's path (conventions §8.1): the full names of its ancestors below
    /// the root and its own, joined by `/`; the root's is `""`. (At most one part per
    /// element, so that parents that loop, in a table not yet checked, still give one.)
    pub fn path(&self, mut i: u32) -> String {
        let mut parts = Vec::new();
        while i != 0 && i != NONE && (i as usize) < self.len() && parts.len() < self.len() {
            parts.push(self.name_of(i));
            i = self.parent[i as usize];
        }
        parts.reverse();
        parts.join("/")
    }

    pub fn build_children(&mut self) {
        let n = self.len();
        let mut counts = vec![0u32; n + 1];
        for i in 1..n {
            let p = self.parent[i];
            if p != NONE {
                counts[p as usize + 1] += 1;
            }
        }
        for i in 0..n {
            counts[i + 1] += counts[i];
        }
        let mut kids = vec![0u32; counts[n] as usize];
        let mut fill = counts.clone();
        for i in 1..n {
            let p = self.parent[i];
            if p != NONE {
                kids[fill[p as usize] as usize] = i as u32;
                fill[p as usize] += 1;
            }
        }
        self.kid_index = counts;
        self.kids = kids;
    }

    pub fn children(&self, i: u32) -> &[u32] {
        if self.kid_index.len() != self.len() + 1 {
            return &[];
        }
        let (a, b) = (
            self.kid_index[i as usize] as usize,
            self.kid_index[i as usize + 1] as usize,
        );
        &self.kids[a..b]
    }

    /// The run root each element belongs to (itself, an ancestor, or NONE).
    pub fn run_roots(&self) -> Vec<u32> {
        let n = self.len();
        let mut rr = vec![NONE; n];
        let mut runs = self.runs.iter().peekable();
        for i in 0..n {
            while runs.peek().is_some_and(|r| (r.0 as usize) < i) {
                runs.next();
            }
            if runs.peek().is_some_and(|r| r.0 as usize == i) {
                rr[i] = i as u32;
            } else if i > 0 && self.parent[i] != NONE && (self.parent[i] as usize) < i {
                rr[i] = rr[self.parent[i] as usize];
            }
        }
        rr
    }

    /// Bytes the columns and tables hold (an estimate of the IR's memory).
    pub fn memory(&self) -> usize {
        let n = self.len();
        n * (1 + 4 + 4 + 8 + 4 + 4 + 8 + 8)
            + self.values.len() * 16
            + self.vbytes.len()
            + self.form_of.len() * 8
            + self.targets.len() * 8
            + self.runs.len() * 24
            + self.kids.len() * 4
            + self.kid_index.len() * 4
            + self.shared.iter().map(|b| b.len() + 48).sum::<usize>()
            + self.names.bytes()
            + self.types.bytes()
            + self.forms.bytes()
    }

    /// Makes run `r` plain again: member 0 stays where it is and members 1.. are
    /// appended as subtrees of their own under the same parent.
    pub fn unfold(&mut self, r: u32) -> Res<()> {
        let Some((count, stride)) = self.run(r) else {
            return Ok(());
        };
        let end = self.subtree_end(r);
        let k = self.runs.binary_search_by_key(&r, |e| e.0).unwrap();
        self.runs.remove(k);
        let was_finished = self.finished;
        self.finished = false;
        for j in 1..count {
            let base = self.len() as u32;
            for i in r..end {
                let iu = i as usize;
                let parent = if i == r {
                    self.parent[iu]
                } else {
                    self.parent[iu] - r + base
                };
                let nidx = if i == r {
                    self.nidx[iu] + j
                } else {
                    self.nidx[iu]
                };
                let (o, n) = (self.start[iu], self.len[iu]);
                let ext = if n > 0 { (o + j * stride, n) } else { (o, 0) };
                let name = self.names.get(self.name[iu]).to_string();
                let ty = self.types.get(self.ty[iu]).to_string();
                let new = self.add(self.kind[iu], parent, &name, nidx, &ty, 0, Some(ext))?;
                if let Some(v) = self
                    .values
                    .binary_search_by_key(&i, |e| e.0)
                    .ok()
                    .map(|x| self.values[x])
                {
                    self.values.push((new, v.1, v.2));
                }
                if let Some(f) = find(&self.form_of, i) {
                    self.form_of.push((new, f));
                }
            }
        }
        self.finished = was_finished;
        Ok(())
    }
}
