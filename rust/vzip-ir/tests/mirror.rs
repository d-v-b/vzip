//! The mirror's table (src/mirror.rs): written from an IR, read back with its column
//! runs expanded, it is the canonical IR, its invariants hold and its leaves rebuild
//! the source; rebuilt from what it loads, it is the same table entry for entry (the
//! canonical form is a fixed point); one test per way a table can be invalid, and one
//! per way a valid table can be non-canonical, which the validator flags.

use std::path::Path;
use vzip_ir::check::{check, leaves};
use vzip_ir::ir::{Ir, NO_INDEX, STRUCT, VALUE};
use vzip_ir::mirror::{text_json, canonical, canonical_problem, load, mirror, ordered, shown, table, table_from_out, view_problem, write, Table};
use vzip_ir::out::Out;
use vzip_ir::run::{run_bytes, Format};

fn fixtures() -> Vec<(Format, std::path::PathBuf)> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../web/test/fixtures");
    let mut v = Vec::new();
    for (f, dir, ext) in [(Format::Tiff, "tiff", "tif"), (Format::Czi, "czi", "czi"), (Format::Nd2, "nd2", "nd2")] {
        let mut ps: Vec<_> = std::fs::read_dir(root.join(dir)).unwrap().map(|e| e.unwrap().path())
            .filter(|p| p.extension().is_some_and(|e| e == ext)).collect();
        ps.sort();
        v.extend(ps.into_iter().map(|p| (f, p)));
    }
    v
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

#[test]
fn the_table_is_the_canonical_ir_rebuilds_the_source_and_is_a_fixed_point() {
    let mut folded_somewhere = false;
    let mut read_arrays = false;
    for (f, p) in fixtures() {
        let data = std::fs::read(&p).unwrap();
        let Ok((ir, _, _)) = run_bytes(f, &data) else { continue };
        let mut out = Out::new("u");
        let folded = mirror(&ir, &mut out, f.name()).unwrap();
        folded_somewhere |= folded.cruns > 0;
        read_arrays |= folded.element_columns > 0;
        let t = table_from_out(&out).unwrap();
        assert!(t.runs.is_empty(), "{p:?}");
        let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
        let back = load(&t, &mut read).unwrap_or_else(|e| panic!("{p:?}: {e}"));
        assert_eq!(ordered(&back), ordered(&ir), "{p:?}");
        check(&back).unwrap();
        // the root: named "", no index, a struct in the source spanning it (§8.1)
        assert_eq!((back.kind[0], back.name_of(0), back.nidx[0], back.space[0]), (STRUCT, String::new(), NO_INDEX, 0), "{p:?}");
        assert_eq!((back.start[0], back.len[0]), (0, data.len() as u64), "{p:?}");
        let mut rebuilt = Vec::new();
        for (o, n) in leaves(&back).unwrap() {
            rebuilt.extend_from_slice(&data[o as usize..(o + n) as usize]);
        }
        assert!(rebuilt == data, "{p:?}");
        assert_eq!(folded.rows, back.len() as u64);
        // the fixed point: the table rebuilt from what it loads is the same, byte for byte
        let y = canonical(&back).unwrap();
        let (again, _) = table(&y, f.name(), &|e| {
            let e = e as usize;
            Some(data[y.start[e] as usize..(y.start[e] + y.len[e]) as usize].to_vec())
        });
        let mut out2 = Out::new("u");
        write(&again, &mut out2, f.name(), false);
        for (k, v) in &out2.entries {
            assert!(entry(&out)(k).as_ref() == Some(v), "{p:?}: {k}");
        }
        assert_eq!(canonical_problem(&entry(&out), &mut read), None, "{p:?}");
        // the view the table and the source give is the stored one
        let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
        assert_eq!(view_problem(&keys, &entry(&out), &mut read), None, "{p:?}");
    }
    assert!(folded_somewhere && read_arrays);
}

/// Every parser keeps every value the view shows: the run's fill batch, which reads
/// those it did not, is empty on every fixture; and the IR holds them all.
#[test]
fn every_value_the_view_shows_is_held() {
    for (f, p) in fixtures() {
        let data = std::fs::read(&p).unwrap();
        let Ok((ir, _, stats)) = run_bytes(f, &data) else { continue };
        assert_eq!(stats["fill"], serde_json::json!({"ranges": 0, "requests": 0, "bytes": 0}), "{p:?}");
        let y = canonical(&ir).unwrap();
        for i in 0..y.len() as u32 {
            if y.kind[i as usize] == VALUE && shown(&y, f.name(), i) {
                assert!(y.value_bytes(i).is_some(), "{p:?}: {} ({})", y.path(i), y.types.get(y.ty[i as usize]));
            }
        }
    }
}

/// A source of 30 bytes: the root and three gaps of 10 bytes, `gaps/0` to `gaps/2`.
fn three_ir() -> Ir {
    let mut ir = Ir::new(30);
    ir.budget = None;
    for k in 0..3u64 {
        ir.gap(0, "gaps/", (10 * k, 10)).unwrap();
        ir.nidx[k as usize + 1] = k;
    }
    ir.finished = true;
    ir.build_children();
    ir
}

/// The canonical mirror of `three_ir`, with `edit` applied to its table, as entries.
fn edited(edit: impl Fn(&mut Table), deflate: bool) -> Out {
    let y = canonical(&three_ir()).unwrap();
    let (mut t, _) = table(&y, "tiff", &|_| None);
    edit(&mut t);
    let mut out = Out::new("u");
    write(&t, &mut out, "tiff", deflate);
    out
}

fn problem(out: &Out) -> String {
    let mut read = |_: u64, n: u64| Ok(vec![0u8; n as usize]);
    canonical_problem(&entry(out), &mut read).expect("a problem")
}

#[test]
fn the_canonical_mirror_of_three_gaps_is_one_column_run() {
    let mut out = Out::new("u");
    mirror(&three_ir(), &mut out, "tiff").unwrap();
    let t = table_from_out(&out).unwrap();
    assert_eq!(t.kind.len(), 2);
    assert_eq!(t.cruns, vec![[1, 3, 1]]);
    let mut read = |_: u64, n: u64| Ok(vec![0u8; n as usize]);
    assert_eq!(canonical_problem(&entry(&out), &mut read), None);
}

#[test]
fn flags_a_run() {
    let out = edited(|t| t.runs.push([1, 1, 0]), false);
    assert!(problem(&out).starts_with("a run (table code 0)"));
}

#[test]
fn flags_compressed_chunks() {
    assert!(problem(&edited(|_| {}, true)).starts_with("ir/rows: compressed chunks"));
}

#[test]
fn flags_unsorted_names() {
    let out = edited(|t| {
        t.names.reverse();
        for x in t.name.iter_mut() {
            *x = 1 - *x;
        }
    }, false);
    assert!(problem(&out).starts_with("the names are not sorted"));
}

#[test]
fn flags_siblings_out_of_order() {
    // gaps/0 and gaps/1 stored in the other order, unfolded
    let out = edited(|t| {
        t.cruns.clear();
        t.columns.clear();
        t.column_values.clear();
        t.kind = vec![0, 5, 5, 5];
        t.up = vec![0, 1, 2, 3];
        t.name = vec![0, 1, 1, 1];
        t.nidx = vec![u64::MAX, 1, 0, 2];
        t.ty = vec![0; 4];
        t.space = vec![0; 4];
        t.start = vec![0, -20, -20, 10];
        t.length = vec![30, 10, 10, 10];
    }, false);
    assert_eq!(problem(&out), "row 1 is out of canonical order");
}

#[test]
fn flags_a_foldable_run_left_unfolded() {
    let out = edited(|t| {
        t.cruns.clear();
        t.columns.clear();
        t.column_values.clear();
        t.kind = vec![0, 5, 5, 5];
        t.up = vec![0, 1, 2, 3];
        t.name = vec![0, 1, 1, 1];
        t.nidx = vec![u64::MAX, 0, 1, 2];
        t.ty = vec![0; 4];
        t.space = vec![0; 4];
        t.start = vec![0, -30, 0, 0];
        t.length = vec![30, 10, 10, 10];
    }, false);
    assert!(problem(&out).starts_with("stored row 2 is"));
}

#[test]
fn flags_a_stored_column_where_arithmetic_holds() {
    let out = edited(|t| {
        let c = t.columns.iter_mut().find(|c| c[3] == 0 && c[2] == 0).expect("an arithmetic name index column");
        c[3] = 1;
        c[4] = t.column_values.len() as i64;
        c[5] = 0;
        t.column_values.extend([0, 1, 1]);
    }, false);
    assert!(problem(&out).starts_with("table row"));
}

/// A table of the root and three gaps of 10 bytes each, folded as one column run.
fn three() -> Table {
    Table {
        size: 30,
        kind: vec![0, 5],
        up: vec![0, 1],
        name: vec![0, 1],
        nidx: vec![u64::MAX, 0],
        ty: vec![0, 0],
        space: vec![0, 0],
        start: vec![0, -30],
        length: vec![30, 10],
        cruns: vec![[1, 3, 1]],
        columns: vec![[0, 0, 2, 0, 0, 10], [0, 0, 0, 0, 0, 1]],
        names: vec!["".into(), "gaps/".into()],
        types: vec!["".into()],
        ..Default::default()
    }
}

fn no_read(_: u64, _: u64) -> Result<Vec<u8>, String> {
    Err("no reads".into())
}

fn err(t: &Table) -> String {
    match load(t, &mut no_read) {
        Ok(_) => panic!("loaded"),
        Err(e) => e,
    }
}

#[test]
fn a_column_run_expands() {
    let ir = load(&three(), &mut no_read).unwrap();
    assert_eq!(ir.len(), 4);
    assert_eq!(ir.start, vec![0, 0, 10, 20]);
    assert_eq!(ir.nidx[1..], [0, 1, 2]);
    check(&ir).unwrap();
}

#[test]
fn rejects_columns_of_different_lengths() {
    let mut t = three();
    t.up.pop();
    assert_eq!(err(&t), "columns of different lengths");
}

#[test]
fn rejects_a_column_run_past_the_budget() {
    let mut t = three();
    t.cruns = vec![[1, 1 << 40, 1]];
    assert!(err(&t).starts_with("budget: more than"));
}

#[test]
fn rejects_a_column_past_its_values() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 1, 0, 0]];
    t.column_values = vec![0, 10];
    assert_eq!(err(&t), "a column past its values");
}

#[test]
fn rejects_a_column_of_a_row_that_is_no_array() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 2, 0, 0]];
    assert_eq!(err(&t), "a column of a row that is no unsigned array");
}

#[test]
fn rejects_a_column_of_a_row_in_a_run_that_reads_columns() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 2, 1, 0]];
    assert_eq!(err(&t), "a column of a row in a column run that reads columns");
}

#[test]
fn rejects_a_column_of_no_column_run() {
    let mut t = three();
    t.columns = vec![[5, 0, 2, 0, 0, 10]];
    assert_eq!(err(&t), "a column of no column run");
}

#[test]
fn rejects_a_bad_parent() {
    let mut t = three();
    t.up[1] = 2;
    assert_eq!(err(&t), "row 1: parent -1");
}

#[test]
fn rejects_a_bad_kind() {
    let mut t = three();
    t.kind[1] = 9;
    assert_eq!(err(&t), "row 1: kind 9");
}

#[test]
fn rejects_a_column_run_that_is_not_a_subtree() {
    let mut t = three();
    t.cruns = vec![[1, 3, 2]];
    assert_eq!(err(&t), "column run 0: member 0 is not 2 rows of sibling subtrees");
}

/// A source of 4 bytes whose IR stands for 4,200,000 elements (a run of empty
/// structs): its mirror would have more rows than 2^22 + 4 / 4, so it is rejected,
/// before the run is expanded.
#[test]
fn rejects_a_mirror_past_the_row_budget() {
    let mut ir = Ir::new(4);
    ir.budget = None;
    ir.gap(0, "gaps/", (0, 4)).unwrap();
    let s = ir.struct_(0, "s", 0, 0, None).unwrap();
    ir.runs.push((s, 4_200_000, 0));
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    assert_eq!(mirror(&ir, &mut out, "tiff").unwrap_err(), "budget: the mirror would have more than 4194305 rows");
}

#[test]
fn the_mirror_of_an_ir_whose_names_break_the_rules_is_a_rejection() {
    // two empty structs `s`: one path for two elements
    let mut ir = Ir::new(10);
    ir.budget = None;
    ir.gap(0, "gaps/", (0, 10)).unwrap();
    ir.struct_(0, "s", u64::MAX, 0, None).unwrap();
    ir.struct_(0, "s", u64::MAX, 0, None).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    assert_eq!(
        mirror(&ir, &mut out, "tiff").unwrap_err(),
        "element 2 (struct \"s\") and element 3 (struct \"s\") have one path, \"s\""
    );
}

#[test]
fn flags_a_table_whose_root_is_named() {
    // `names[0]`, the root's name, is "!" (still sorted first)
    let out = edited(|t| t.names[0] = "!".into(), false);
    assert_eq!(problem(&out), "invalid: the root (element 0) is named \"!\", not \"\"");
    let t = table_from_out(&out).unwrap();
    let back = load(&t, &mut no_read).unwrap();
    assert_eq!(check(&back).unwrap_err(), "the root (element 0) is named \"!\", not \"\"");
}

/// A fixture's output, its view document at `group` edited by `edit`, and what the
/// validator's view check says of it.
fn view_edited(fixture: &str, group: &str, edit: impl Fn(&mut serde_json::Value)) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../web/test/fixtures/tiff").join(fixture);
    let data = std::fs::read(path).unwrap();
    let (ir, _, _) = run_bytes(Format::Tiff, &data).unwrap();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let key = format!("vzip_source/{group}/zarr.json");
    let e = out.entries.iter_mut().find(|e| e.0 == key).unwrap();
    let mut doc: serde_json::Value = serde_json::from_slice(&e.1).unwrap();
    edit(&mut doc["attributes"]["vzip_virtualized"]["tiff"]);
    e.1 = serde_json::to_vec(&doc).unwrap();
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    view_problem(&keys, &entry(&out), &mut read).expect("a problem")
}

#[test]
fn flags_a_view_missing_a_value() {
    let p = view_edited("jpeg_gray.tif", "tree", |d| {
        d["header"].as_object_mut().unwrap().remove("magic");
    });
    assert_eq!(p, r#"view tree: member ["header"]["magic"] is missing"#);
}

#[test]
fn flags_a_view_with_an_extra_value() {
    let p = view_edited("jpeg_gray.tif", "tree", |d| d["header"]["extra"] = serde_json::json!(1));
    assert_eq!(p, r#"view tree: member ["header"]["extra"] is not in the rebuilt view"#);
}

#[test]
fn flags_a_wrong_partial() {
    let p = view_edited("jpeg_gray.tif", "tree", |d| d["$partial"] = serde_json::json!(true));
    assert_eq!(p, r#"view tree: member ["$partial"] is not in the rebuilt view"#);
}

#[test]
fn flags_a_value_shown_above_the_size_limit() {
    // a source of one value of 2000 bytes, past the 1024 the view shows: a reference to its row
    let data = vec![7u8; 2000];
    let mut ir = Ir::new(2000);
    ir.value(0, "big", u64::MAX, "u1[2000]", 0, (0, 2000), None).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let e = out.entries.iter_mut().find(|e| e.0 == "vzip_source/tree/zarr.json").unwrap();
    let mut doc: serde_json::Value = serde_json::from_slice(&e.1).unwrap();
    assert_eq!(doc["attributes"]["vzip_virtualized"]["tiff"]["big"], serde_json::json!({"$vz": "element", "id": 1}));
    doc["attributes"]["vzip_virtualized"]["tiff"]["big"] = serde_json::json!(vec![7; 2000]);
    e.1 = serde_json::to_vec(&doc).unwrap();
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    let p = view_problem(&keys, &entry(&out), &mut read).unwrap();
    assert!(p.starts_with(r#"view tree: member ["big"] is [7,7,"#) && p.ends_with(r#"where the rebuilt view has {"$vz":"element","id":1}"#), "{p}");
}

/// Text values in the view (conventions §8.7): §6's text, and `{text, after}` when
/// bytes after the NUL are not all zero.
#[test]
fn text_values_show_their_text_and_what_follows_its_nul() {
    use serde_json::json;
    let cases: Vec<(&str, &[u8], serde_json::Value)> = vec![
        ("ascii", b"II", json!("II")),
        ("ascii", b"abc\0\0\0", json!("abc")),
        ("cstr", b"\xe9t\xe9\0", json!({"latin1": "\u{e9}t\u{e9}"})),
        ("cstr", b"ab\0xyz\0\0", json!({"text": "ab", "after": {"$vz": "bytes", "b64": "AHh5egAA"}})),
        ("ascii", b"\0\x01", json!({"text": "", "after": {"$vz": "bytes", "b64": "AAE="}})),
        ("utf16", b"h\0i\0\0\0", json!("hi")),
        ("utf16", b"h\0\0\0x\0", json!({"text": "h", "after": {"$vz": "bytes", "b64": "AAB4AA=="}})),
    ];
    for (kind, raw, want) in cases {
        assert_eq!(text_json(kind, raw), want, "{kind} {raw:?}");
    }
    // in a mirror: shown so, and the validator's rebuild agrees
    let data = b"Label\0hidden".to_vec();
    let mut ir = Ir::new(data.len() as u64);
    ir.value(0, "name", u64::MAX, "cstr[12]", 0, (0, 12), Some(&data)).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "czi").unwrap();
    let doc: serde_json::Value = serde_json::from_slice(&entry(&out)("vzip_source/tree/zarr.json").unwrap()).unwrap();
    assert_eq!(doc["attributes"]["vzip_virtualized"]["czi"]["name"], json!({"text": "Label", "after": {"$vz": "bytes", "b64": "AGhpZGRlbg=="}}));
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    assert_eq!(view_problem(&keys, &entry(&out), &mut read), None);
}

/// Every derived transform a parser can emit (`Transform::ALL`, which `index` keeps
/// complete) either may hold shown values and a validator can derive it again, or may
/// hold none (conventions §8.8); and on every fixture, each derived element names a
/// known transform and no space that may hold none holds a shown value.
#[test]
fn every_derived_transform_holding_shown_values_can_be_derived_again() {
    use vzip_ir::ir::DERIVED;
    use vzip_ir::transform::Transform;
    for (k, t) in Transform::ALL.into_iter().enumerate() {
        assert_eq!(t.index(), k, "{t:?} is not at its index in ALL");
        let derivable = t.rederive(&[], &serde_json::Value::Null).is_some();
        assert!(!t.may_hold_shown() || derivable, "{} may hold shown values, but a validator cannot derive it", t.name());
    }
    let mut seen = std::collections::HashSet::new();
    for (f, p) in fixtures() {
        let data = std::fs::read(&p).unwrap();
        let Ok((ir, _, _)) = run_bytes(f, &data) else { continue };
        let y = canonical(&ir).unwrap();
        for i in 0..y.len() {
            if y.kind[i] == DERIVED {
                let t = Transform::of_form(y.form(i as u32).unwrap_or("")).unwrap_or_else(|| panic!("{p:?}: {:?}", y.form(i as u32)));
                seen.insert(t);
            }
            let d = y.space[i];
            if d != 0 && shown(&y, f.name(), i as u32) {
                assert!(Transform::of_form(y.form(d).unwrap()).unwrap().may_hold_shown(), "{p:?}: {}", y.path(i as u32));
            }
        }
    }
    assert!(seen.len() >= 2, "{seen:?}");
}
