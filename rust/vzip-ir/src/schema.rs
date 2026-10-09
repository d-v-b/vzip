//! The checker of a source model schema (`schema/nd2.json`): it reads the LV
//! members the schema lists, with their kinds, defaults and limits; checks every
//! experiment node; flattens the loops; evaluates the derived quantities and the
//! constraints. A file that fails is rejected here, once, with the schema's message.

use crate::expr::{self, Env, V};
use crate::lv::{Node, Read, Rec, scalar_flag, scalar_number};
use serde_json::Value as J;
use std::sync::OnceLock;

pub static ND2_SCHEMA: &str = include_str!("../schema/nd2.json");

pub fn nd2() -> &'static J {
    static S: OnceLock<J> = OnceLock::new();
    S.get_or_init(|| serde_json::from_str(ND2_SCHEMA).expect("the ND2 schema is JSON"))
}

pub fn limit(name: &str) -> f64 {
    nd2()["limits"][name].as_f64().unwrap_or(f64::NAN)
}

pub type Res<T> = Result<T, String>;

#[derive(Clone, Copy)]
enum Item<'a> {
    N(Node<'a>),
    Byte(u8),
}

impl<'a> Item<'a> {
    fn read(&self) -> Read<'a> {
        match self {
            Item::N(n) => n.read(),
            Item::Byte(b) => Read::Scalar(3, &BYTES[*b as usize..*b as usize + 1]),
        }
    }
}

const MAX_SAFE: f64 = 9007199254740991.0;

static BYTES: [u8; 256] = {
    let mut t = [0u8; 256];
    let mut i = 0;
    while i < 256 {
        t[i] = i as u8;
        i += 1;
    }
    t
};

fn default_of(spec: &J) -> V {
    V::from_json(spec.get("default").unwrap_or(&J::Null))
}

fn missing(spec: &J, what: &str) -> Res<V> {
    match spec.get("required") {
        Some(J::Bool(true)) => Err(format!("missing {what}")),
        Some(J::String(m)) => Err(m.clone()),
        _ => Ok(default_of(spec)),
    }
}

fn members_of<'a>(item: Item<'a>, what: &str) -> Res<Vec<Item<'a>>> {
    Ok(match item.read() {
        Read::Object(m) => m.into_iter().map(|(_, n)| Item::N(n)).collect(),
        Read::List(v) => v.into_iter().map(Item::N).collect(),
        Read::Bytes(b) => b.iter().map(|&x| Item::Byte(x)).collect(),
        _ => return Err(format!("{what} is not an object or a list")),
    })
}

fn object_members<'a>(item: Item<'a>, what: &str) -> Res<Vec<(String, Node<'a>)>> {
    match item.read() {
        Read::Object(m) => Ok(m),
        _ => Err(format!("{what} is not an object")),
    }
}

/// A member read by its spec (conventions/nd2 §2.2): `item` None is a missing member.
fn read(
    item: Option<Item<'_>>,
    spec: &J,
    what: &str,
    siblings: &[(String, V)],
    outer: &[(String, V)],
) -> Res<V> {
    let Some(item) = item else {
        return missing(spec, what);
    };
    let kind = spec["kind"].as_str().unwrap_or("");
    let v = match kind {
        "number" | "integer" | "color" => {
            let Read::Scalar(t, raw) = item.read() else {
                return Err(format!("{what} is not a number"));
            };
            let Some(x) = scalar_number(t, raw) else {
                return Err(format!("{what} is not a number"));
            };
            if !x.is_finite() {
                return Err(format!("{what} is not finite"));
            }
            match kind {
                "integer" if x != x.trunc() || !(0.0..=MAX_SAFE).contains(&x) => {
                    return Err(format!("{what} = {x} is not an integer from 0 to 2^53 - 1"));
                }
                "color" => {
                    if x != x.trunc() || !(-2147483648.0..=4294967295.0).contains(&x) {
                        return Err(format!("{what} = {x} is not a color"));
                    }
                    V::Num((x as i64).rem_euclid(1 << 32) as f64)
                }
                _ => V::Num(x),
            }
        }
        "flag" => {
            let Read::Scalar(t, raw) = item.read() else {
                return Err(format!("{what} is not a flag"));
            };
            V::Bool(scalar_flag(t, raw).ok_or_else(|| format!("{what} is not a flag"))?)
        }
        "string" => match item.read() {
            Read::Str(s) => V::Str(s),
            _ => return Err(format!("{what} is not a string")),
        },
        "object" => {
            let m = object_members(item, what)?;
            if spec.get("type").and_then(|t| t.as_str()) == Some("node") {
                return read_node(item, what);
            }
            if let Some(ms) = spec.get("members") {
                V::Obj(read_members(&m, ms, outer)?)
            } else if let Some(ix) = spec.get("indexed") {
                let below = siblings
                    .iter()
                    .find(|(n, _)| n == ix["below"].as_str().unwrap())
                    .and_then(|(_, v)| v.num())
                    .unwrap_or(0.0);
                let prefix = ix["prefix"].as_str().unwrap();
                let mut out = Vec::new();
                for (name, n) in &m {
                    let Some(digits) = name.strip_prefix(prefix) else {
                        continue;
                    };
                    let ok = !digits.is_empty()
                        && digits.bytes().all(|c| c.is_ascii_digit())
                        && (digits == "0" || !digits.starts_with('0'));
                    if !ok {
                        continue;
                    }
                    let i: f64 = if digits.len() > 18 {
                        f64::INFINITY
                    } else {
                        digits.parse::<u64>().unwrap() as f64
                    };
                    if i < below {
                        let v = read(Some(Item::N(*n)), ix, name, &[], &[])?;
                        out.push((digits.to_string(), v));
                    }
                }
                V::Obj(out)
            } else {
                V::Obj(Vec::new())
            }
        }
        "list" => {
            let items: Vec<Item> = match item.read() {
                Read::List(v) => v.into_iter().map(Item::N).collect(),
                Read::Bytes(b) => b.iter().map(|&x| Item::Byte(x)).collect(),
                _ => return Err(format!("{what} is not a list")),
            };
            match spec.get("items") {
                Some(is) => V::List(
                    items
                        .iter()
                        .map(|x| read(Some(*x), is, &format!("{what} entry"), &[], outer))
                        .collect::<Res<_>>()?,
                ),
                None => V::List(vec![V::Absent; items.len()]),
            }
        }
        "members" => {
            let items = members_of(item, what)?;
            let mut checked = Vec::new();
            if let Some(is) = spec.get("items") {
                for x in &items {
                    checked.push(read(Some(*x), is, &format!("{what} member"), &[], outer)?);
                }
            }
            if let Some(valid) = spec.get("valid") {
                let by = valid["by"].as_str().unwrap();
                let flags = match by.strip_prefix("../") {
                    Some(k) => outer.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone()),
                    None => siblings
                        .iter()
                        .find(|(n, _)| n == by)
                        .map(|(_, v)| v.clone()),
                }
                .unwrap_or(V::Absent);
                let keep: Vec<bool> = match &flags {
                    V::List(f) => (0..items.len())
                        .map(|i| f.get(i).is_some_and(|x| x.truthy()))
                        .collect(),
                    _ => vec![true; items.len()],
                };
                let mut out = Vec::new();
                for (x, k) in items.iter().zip(keep) {
                    if !k {
                        continue;
                    }
                    let vspec = if valid.get("kind").is_some() {
                        valid.clone()
                    } else {
                        let mut s = valid.clone();
                        s["kind"] = J::String("object".into());
                        s
                    };
                    out.push(read(
                        Some(*x),
                        &vspec,
                        &format!("{what} member"),
                        &[],
                        outer,
                    )?);
                }
                V::List(out)
            } else {
                V::List(checked)
            }
        }
        _ => return Err(format!("schema: unknown kind {kind:?}")),
    };
    if let Some(allowed) = spec.get("in").and_then(|a| a.as_array()) {
        let x = v.num();
        if !allowed.iter().any(|a| a.as_f64() == x) {
            let m = spec
                .get("message")
                .and_then(|m| m.as_str())
                .unwrap_or("a value outside its range");
            return Err(format!(
                "{m} {}",
                x.map(|x| x.to_string()).unwrap_or_default()
            ));
        }
    }
    Ok(v)
}

/// An object's members by a map of specs: those read through a validity list after the others.
fn read_members(
    m: &[(String, Node<'_>)],
    specs: &J,
    outer: &[(String, V)],
) -> Res<Vec<(String, V)>> {
    let specs = specs.as_object().unwrap();
    let mut out: Vec<(String, V)> = Vec::new();
    for pass in 0..2 {
        for (name, spec) in specs {
            let late = spec.get("valid").is_some();
            if (pass == 1) != late {
                continue;
            }
            if let Some(w) = spec.get("when_absent").and_then(|w| w.as_str()) {
                if out.iter().any(|(n, v)| n == w && *v != V::Absent) {
                    out.push((name.clone(), V::Absent));
                    continue;
                }
            }
            let child = m.iter().find(|(n, _)| n == name).map(|(_, n)| Item::N(*n));
            let v = read(child, spec, name, &out, outer)?;
            out.push((name.clone(), v));
        }
    }
    // in the schema's order
    let order: Vec<&String> = specs.keys().collect();
    out.sort_by_key(|(n, _)| order.iter().position(|k| *k == n));
    Ok(out)
}

fn env_of(members: &[(String, V)]) -> Env {
    let mut env = Env::new();
    for (k, v) in members {
        env.insert(k.clone(), v.clone());
    }
    for (k, v) in nd2()["limits"].as_object().unwrap() {
        env.insert(k.clone(), V::from_json(v));
    }
    env
}

fn eval_str(s: &str, env: &Env) -> Res<V> {
    Ok(expr::eval(&expr::parse(s)?, env))
}

/// An experiment node: its members, its loop (`loop`, absent when skipped), its children.
fn read_node(item: Item<'_>, what: &str) -> Res<V> {
    let node_spec = &nd2()["types"]["node"];
    let m = object_members(item, what)?;
    let members = read_members(&m, &node_spec["members"], &[])?;
    let get = |k: &str| {
        members
            .iter()
            .find(|(n, _)| n == k)
            .map(|(_, v)| v.clone())
            .unwrap_or(V::Absent)
    };
    let e_type = get("eType").num().unwrap();
    let pars = m.iter().find(|(n, _)| n == "uLoopPars").map(|(_, n)| *n);
    let mut lp = V::Absent;
    if let Some(pars) = pars {
        let case = &node_spec["loops"][format!("{}", e_type as i64)];
        let pm = object_members(Item::N(pars), "uLoopPars")?;
        let pv = read_members(&pm, &case["members"], &members)?;
        let mut env = env_of(&pv);
        let count = eval_str(case["count"].as_str().unwrap(), &env)?;
        env.insert("count".into(), count.clone());
        let kind = case["kind"].as_str().unwrap().to_string();
        let mut loop_ = vec![
            ("kind".to_string(), V::Str(kind.clone())),
            ("count".to_string(), count.clone()),
        ];
        if let Some(s) = case.get("scale").and_then(|s| s.as_str()) {
            let scale = eval_str(s, &env)?;
            env.insert("scale".into(), scale.clone());
            loop_.push(("scale".into(), scale));
        }
        if let Some(s) = case.get("flip").and_then(|s| s.as_str()) {
            loop_.push(("flip".into(), eval_str(s, &env)?));
        }
        if let Some(s) = case.get("stage").and_then(|s| s.as_str()) {
            let pts = env.get(s).cloned().unwrap_or(V::List(vec![]));
            let stage = match pts {
                V::List(v) => V::List(
                    v.iter()
                        .map(|q| V::List(vec![q.get("dPosX"), q.get("dPosY")]))
                        .collect(),
                ),
                _ => V::List(vec![]),
            };
            loop_.push(("stage".into(), stage));
        }
        for c in case
            .get("constraints")
            .and_then(|c| c.as_array())
            .into_iter()
            .flatten()
        {
            if !eval_str(c["require"].as_str().unwrap(), &env)?.truthy() {
                return Err(c["message"].as_str().unwrap().to_string());
            }
        }
        if count.num().unwrap_or(0.0) != 0.0 {
            lp = V::Obj(loop_);
        }
    }
    Ok(V::Obj(vec![
        ("eType".into(), V::Num(e_type)),
        ("loop".into(), lp),
        ("children".into(), get("ppNextLevelEx")),
    ]))
}

#[derive(Clone, Debug)]
pub struct Loop {
    pub kind: String,
    pub count: f64,
    pub scale: f64,
    pub depth: u32,
    pub flip: bool,
    pub stage: Vec<(Option<f64>, Option<f64>)>,
}

/// The flattening of conventions/nd2 §3 (rules 1 to 3).
pub fn flatten(root: &V) -> Vec<Loop> {
    let mut loops: Vec<Loop> = Vec::new();
    fn visit(node: &V, depth: u32, loops: &mut Vec<Loop>) {
        let lp = node.get("loop");
        if lp == V::Absent {
            return;
        }
        let kind = match lp.get("kind") {
            V::Str(s) => s,
            _ => return,
        };
        let mut child_depth = depth + 1;
        if kind == "spectral" {
            child_depth = depth;
        } else {
            let item = Loop {
                kind: kind.clone(),
                count: lp.get("count").num().unwrap_or(0.0),
                scale: lp.get("scale").num().unwrap_or(0.0),
                depth,
                flip: lp.get("flip").truthy(),
                stage: match lp.get("stage") {
                    V::List(v) => v
                        .iter()
                        .map(|q| match q {
                            V::List(x) => (x[0].num(), x[1].num()),
                            _ => (None, None),
                        })
                        .collect(),
                    _ => vec![],
                },
            };
            match loops.last() {
                None => loops.push(item),
                Some(l) if l.depth < depth => loops.push(item),
                Some(l) if l.depth == depth && l.kind == item.kind && l.count < item.count => {
                    *loops.last_mut().unwrap() = item;
                }
                _ => {}
            }
        }
        if let V::List(children) = node.get("children") {
            for c in &children {
                visit(c, child_depth, loops);
            }
        }
    }
    if *root != V::Absent {
        visit(root, 0, &mut loops);
    }
    loops
}

/// A placed frame's header as read: (offset, magic, name length, data length), or
/// None when its 16 bytes are not within the file.
#[derive(Clone, Copy, Debug)]
pub struct FrameHead {
    pub f: u64,
    pub offset: u64,
    pub head: Option<(u32, u32, u64)>,
}

pub struct Facts {
    pub json: J,
    pub loops: Vec<Loop>,
    pub frames: f64,
    pub compressed: bool,
    pub height: u64,
    pub width: u64,
    pub width_bytes: u64,
    pub row: u64,
    pub comp: u64,
    pub bpc: u64,
    pub name_length: Option<u32>,
}

fn chunk_member<'a>(chunk: Option<(&'a [Rec], &'a [u8])>, member: &str) -> Option<Item<'a>> {
    let (recs, bytes) = chunk?;
    match Node::top(recs, bytes).read() {
        Read::Object(m) => m
            .into_iter()
            .find(|(n, _)| n == member)
            .map(|(_, n)| Item::N(n)),
        _ => None,
    }
}

/// Checks the schema on the three chunks the convention reads and on the placed
/// frames' headers (`frames` lists every frame chunk ImageDataSeq|<f>! of the map;
/// the placed ones are those below N).
pub fn check_nd2(
    attributes: Option<(&[Rec], &[u8])>,
    experiment: Option<(&[Rec], &[u8])>,
    picture: Option<(&[Rec], &[u8])>,
    frames: &[FrameHead],
    size: u64,
) -> Res<Facts> {
    let s = nd2();
    let objs = &s["objects"];
    let mut env = env_of(&[]);
    env.insert("size".into(), V::Num(size as f64));
    // attributes
    let a = &objs["attributes"];
    if attributes.is_none() {
        return Err(a["required"].as_str().unwrap().to_string());
    }
    let item = chunk_member(attributes, a["member"].as_str().unwrap());
    let attrs = read(
        item,
        &serde_json::json!({"kind": "object", "required": true, "members": a["members"]}),
        a["member"].as_str().unwrap(),
        &[],
        &[],
    )?;
    let V::Obj(am) = &attrs else { unreachable!() };
    for (k, v) in am {
        env.insert(k.clone(), v.clone());
    }
    // experiment
    let e = &objs["experiment"];
    let root = match chunk_member(experiment, e["member"].as_str().unwrap()) {
        Some(item) => {
            object_members(item, e["member"].as_str().unwrap())?;
            read_node(item, e["member"].as_str().unwrap())?
        }
        None => V::Absent,
    };
    let loops = flatten(&root);
    let mut kinds: Vec<&str> = loops.iter().map(|l| l.kind.as_str()).collect();
    kinds.sort();
    let distinct = kinds.windows(2).all(|w| w[0] != w[1]);
    env.insert("loops_distinct".into(), V::Bool(distinct));
    let product: f64 = loops.iter().map(|l| l.count).product();
    env.insert("loops_product".into(), V::Num(product));
    let find = |k: &str| loops.iter().find(|l| l.kind == k);
    env.insert(
        "p_count".into(),
        find("p").map(|l| V::Num(l.count)).unwrap_or(V::Absent),
    );
    env.insert(
        "t_scale".into(),
        find("t").map(|l| V::Num(l.scale)).unwrap_or(V::Absent),
    );
    env.insert(
        "z_scale".into(),
        find("z").map(|l| V::Num(l.scale)).unwrap_or(V::Absent),
    );
    // picture metadata
    let p = &objs["picture"];
    let pic = match chunk_member(picture, p["member"].as_str().unwrap()) {
        Some(item) => read(
            Some(item),
            &serde_json::json!({"kind": "object", "members": p["members"]}),
            p["member"].as_str().unwrap(),
            &[],
            &[],
        )?,
        None => {
            // no picture metadata: every member at its default
            let empty: Vec<(String, Node)> = Vec::new();
            V::Obj(read_members(&empty, &p["members"], &[])?)
        }
    };
    let V::Obj(pm) = &pic else { unreachable!() };
    for (k, v) in pm {
        env.insert(k.clone(), v.clone());
    }
    // derived
    for (k, x) in s["derive"].as_object().unwrap() {
        let v = eval_str(x.as_str().unwrap(), &env)?;
        env.insert(k.clone(), v);
    }
    let num = |env: &Env, k: &str| env.get(k).and_then(|v| v.num()).unwrap_or(f64::NAN);
    let n_frames = num(&env, "N");
    // placed frames: their headers
    let compressed = env.get("compressed").is_some_and(|v| v.truthy());
    let (height, wb, r) = (
        num(&env, "uiHeight"),
        num(&env, "uiWidthBytes"),
        num(&env, "R"),
    );
    let mut placed = (0u64, 0u64, u32::MAX, 0u32, u64::MAX, 0u128);
    for fr in frames {
        if (fr.f as f64) >= n_frames {
            continue;
        }
        placed.0 += 1;
        match fr.head {
            Some((magic, n, d))
                if magic == limit("chunk_magic") as u32
                    && (fr.offset as f64) <= MAX_SAFE
                    && (d as f64) <= MAX_SAFE
                    && fr.offset + 16 + n as u64 <= size =>
            {
                placed.2 = placed.2.min(n);
                placed.3 = placed.3.max(n);
                placed.4 = placed.4.min(d);
                let start = fr.offset as u128 + 16 + n as u128 + 8;
                let end = if compressed {
                    start + d as u128 - 8
                } else {
                    start + (height as u128).saturating_sub(1) * wb as u128 + r as u128
                };
                placed.5 = placed.5.max(end);
            }
            _ => placed.1 += 1,
        }
    }
    let pl = V::Obj(vec![
        ("count".into(), V::Num(placed.0 as f64)),
        ("unreadable".into(), V::Num(placed.1 as f64)),
        ("min_name".into(), V::Num(placed.2 as f64)),
        ("max_name".into(), V::Num(placed.3 as f64)),
        ("min_data".into(), V::Num(placed.4 as f64)),
        ("max_pixels_end".into(), V::Num(placed.5 as f64)),
    ]);
    env.insert("placed".into(), pl);
    // stage translations (conventions/nd2 §4.3)
    let calibrated = env.get("calibrated").is_some_and(|v| v.truthy());
    let det = num(&env, "det");
    let stages: Vec<(Option<f64>, Option<f64>)> = match find("p") {
        Some(l) => l.stage.clone(),
        None => vec![(
            env.get("dXPos").and_then(|v| v.num()),
            env.get("dYPos").and_then(|v| v.num()),
        )],
    };
    let width = num(&env, "uiWidth");
    let (sx_scale, sy_scale) = (num(&env, "scale_x"), num(&env, "scale_y"));
    let mut translations = J::Null;
    let mut finite = true;
    if calibrated && det != 0.0 && stages.iter().all(|(x, y)| x.is_some() && y.is_some()) {
        let (m11, m12, m21, m22) = (
            num(&env, "dStgLgCT11"),
            num(&env, "dStgLgCT12"),
            num(&env, "dStgLgCT21"),
            num(&env, "dStgLgCT22"),
        );
        let mut out = Vec::new();
        for (sx, sy) in &stages {
            let (sx, sy) = (sx.unwrap(), sy.unwrap());
            let u = (m22 * sx - m12 * sy) / det;
            let v = (m11 * sy - m21 * sx) / det;
            let x = u - width * sx_scale / 2.0;
            let y = v - height * sy_scale / 2.0;
            finite &= x.is_finite() && y.is_finite();
            out.push(serde_json::json!({"x": x, "y": y}));
        }
        if finite {
            translations = J::Array(out);
        }
    }
    env.insert("finite_translations".into(), V::Bool(finite));
    for c in s["constraints"].as_array().unwrap() {
        if !eval_str(c["require"].as_str().unwrap(), &env)?.truthy() {
            return Err(c["message"].as_str().unwrap().to_string());
        }
    }
    let loops_json: Vec<J> = loops
        .iter()
        .map(|l| {
            let mut o =
                serde_json::json!({"kind": l.kind, "count": l.count as u64, "depth": l.depth});
            match l.kind.as_str() {
                "p" => {
                    o["stage"] = J::Array(
                        l.stage
                            .iter()
                            .map(|(x, y)| serde_json::json!([x, y]))
                            .collect(),
                    )
                }
                "z" => {
                    o["scale"] = serde_json::json!(l.scale);
                    o["flip"] = J::Bool(l.flip);
                }
                _ => o["scale"] = serde_json::json!(l.scale),
            }
            o
        })
        .collect();
    let get = |k: &str| env.get(k).map(|v| v.to_json()).unwrap_or(J::Null);
    let json = serde_json::json!({
        "attributes": {"width": get("uiWidth"), "height": get("uiHeight"), "width_bytes": get("uiWidthBytes"),
                       "components": get("uiComp"), "bits": get("uiBpcInMemory"), "significant": get("uiBpcSignificant"),
                       "compression": get("eCompression"), "row": get("R"), "compressed": compressed},
        "loops": loops_json,
        "positions": get("P"),
        "frames": get("N"),
        "picture": {"calibrated": get("calibrated"), "planes": get("sPicturePlanes"),
                    "scales": {"x": get("scale_x"), "y": get("scale_y"), "z": get("scale_z"), "t": get("scale_t"), "c": 1.0},
                    "translations": translations},
        "placed": placed.0,
    });
    Ok(Facts {
        json,
        frames: n_frames,
        compressed,
        height: height as u64,
        width: width as u64,
        width_bytes: wb as u64,
        row: r as u64,
        comp: num(&env, "uiComp") as u64,
        bpc: num(&env, "uiBpcInMemory") as u64,
        name_length: if placed.0 > 0 && placed.2 != u32::MAX {
            Some(placed.2)
        } else {
            None
        },
        loops,
    })
}
