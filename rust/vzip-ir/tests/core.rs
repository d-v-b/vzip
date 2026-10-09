//! The compact IR's invariants with runs: folding, finish (gap runs, unfolding,
//! aliases), check and the rebuild's leaves; the root and the names (conventions
//! §8.1), one test for the IRs that keep them and one per way to break them.

use vzip_ir::check::{check, finish, leaves, names};
use vzip_ir::ir::*;

/// `n` segments of a 32-byte header and `body` bytes of data, `stride` apart from `at`.
fn segments(ir: &mut Ir, at: u64, n: u64, body: u64, stride: u64, fold: bool) {
    let mut prev = None;
    for k in 0..n {
        let o = at + k * stride;
        let s = ir
            .struct_(0, "segments/", k, 0, Some((o, 32 + body)))
            .unwrap();
        ir.value(
            s,
            "header",
            NO_INDEX,
            "bytes[32]",
            0,
            (o, 32),
            Some(&[7u8; 32]),
        )
        .unwrap();
        if body > 0 {
            ir.value(
                s,
                "data",
                NO_INDEX,
                &format!("bytes[{body}]"),
                0,
                (o + 32, body),
                None,
            )
            .unwrap();
        }
        if fold {
            match prev {
                Some(p) if ir.fold(p, s) => {}
                _ => prev = Some(s),
            }
        }
    }
}

#[test]
fn runs_fold_finish_check_and_rebuild() {
    // (start, members, body, stride, extra single, expected rows after finish, expected leaves)
    type Case = (
        u64,
        u64,
        u64,
        u64,
        Option<(u64, u64)>,
        usize,
        Vec<(u64, u64)>,
    );
    let cases: Vec<Case> = vec![
        // contiguous members: one run, one block, nothing else
        (0, 1000, 0, 32, None, 1 + 2, vec![(0, 32000)]),
        (0, 1000, 16, 48, None, 1 + 3, vec![(0, 48000)]),
        // holes between members: a gap run (count - 1) fills them
        (0, 1000, 16, 64, None, 1 + 3 + 1, vec![(0, 63984)]),
        // a single element after the run
        (0, 10, 0, 32, Some((320, 8)), 1 + 2 + 1, vec![(0, 328)]),
        // a single element inside member 5, claimed after it: the run is unfolded
        // and the single is an alias of member 5's header (the element that claims it)
        (0, 10, 0, 32, Some((170, 4)), 1 + 2 * 10 + 1, vec![(0, 320)]),
        // a single element that member 0 overlaps, claimed before it: the run is
        // unfolded and member 0's header is an alias (which claims nothing, so the
        // bytes of it past the single are a gap), and a gap precedes the single
        (
            32,
            10,
            0,
            32,
            Some((20, 20)),
            1 + 2 * 10 + 1 + 2,
            vec![(0, 352)],
        ),
    ];
    for (at, n, body, stride, single, rows, want) in cases {
        let end = at + (n - 1) * stride + 32 + body;
        let size = single.map(|(o, l)| end.max(o + l)).unwrap_or(end);
        let mut ir = Ir::new(size);
        if at > 0 && single.is_none() {
            ir.gap(0, "lead", (0, at)).unwrap();
        }
        segments(&mut ir, at, n, body, stride, true);
        assert_eq!(ir.runs.len(), 1, "one run of {n}");
        if let Some((o, l)) = single {
            ir.value(
                0,
                "extra",
                NO_INDEX,
                &format!("bytes[{l}]"),
                0,
                (o, l),
                None,
            )
            .unwrap();
        }
        finish(&mut ir).unwrap();
        check(&ir).unwrap();
        assert_eq!(
            ir.len(),
            rows,
            "rows for n={n} body={body} stride={stride} single={single:?}"
        );
        assert_eq!(leaves(&ir).unwrap(), want);
    }
}

#[test]
fn fold_refuses_different_values() {
    let mut ir = Ir::new(64);
    let a = ir.struct_(0, "s/", 0, 0, Some((0, 32))).unwrap();
    ir.value(a, "h", NO_INDEX, "bytes[32]", 0, (0, 32), Some(&[1u8; 32]))
        .unwrap();
    let b = ir.struct_(0, "s/", 1, 0, Some((32, 32))).unwrap();
    ir.value(b, "h", NO_INDEX, "bytes[32]", 0, (32, 32), Some(&[2u8; 32]))
        .unwrap();
    assert!(!ir.fold(a, b));
}

#[test]
fn check_finds_a_hole() {
    let mut ir = Ir::new(100);
    ir.value(0, "a", NO_INDEX, "bytes[10]", 0, (0, 10), None)
        .unwrap();
    ir.value(0, "b", NO_INDEX, "bytes[10]", 0, (20, 80), None)
        .unwrap();
    let e = check(&ir).unwrap_err();
    assert!(
        e.contains("bytes [10, 20) are claimed by no element"),
        "{e}"
    );
}

#[test]
fn check_finds_an_overlap() {
    let mut ir = Ir::new(20);
    ir.value(0, "a", NO_INDEX, "bytes[12]", 0, (0, 12), None)
        .unwrap();
    ir.value(0, "b", NO_INDEX, "bytes[10]", 0, (10, 10), None)
        .unwrap();
    let e = check(&ir).unwrap_err();
    assert!(e.contains("bytes [10, 12) are claimed by"), "{e}");
}

#[test]
fn check_finds_members_that_overlap_one_another() {
    let mut ir = Ir::new(100);
    let s = ir.struct_(0, "s/", 0, 0, Some((0, 40))).unwrap();
    ir.value(s, "h", NO_INDEX, "bytes[40]", 0, (0, 40), None)
        .unwrap();
    ir.runs.push((s, 2, 30)); // members [0, 40) and [30, 70)
    let e = check(&ir).unwrap_err();
    assert!(e.contains("overlap one another"), "{e}");
}

#[test]
fn check_finds_a_dangling_alias() {
    let mut ir = Ir::new(10);
    ir.value(0, "a", NO_INDEX, "bytes[10]", 0, (0, 10), None)
        .unwrap();
    ir.add(ALIAS, 0, "b", NO_INDEX, "", 0, None).unwrap();
    let e = check(&ir).unwrap_err();
    assert!(e.contains("names no element"), "{e}");
}

/// A 30-byte source: the root, then `a` (a value of 10 bytes) and a gap of 20.
fn named() -> Ir {
    let mut ir = Ir::new(30);
    ir.value(0, "a", NO_INDEX, "bytes[10]", 0, (0, 10), None).unwrap();
    ir.gap(0, "gaps/", (10, 20)).unwrap();
    ir.nidx[2] = 10;
    ir
}

#[test]
fn the_root_spans_the_source_and_every_path_is_its_own() {
    let mut ir = Ir::new(64);
    // the root: a struct named "", no index, (0, size), path ""
    assert_eq!((ir.kind[0], ir.name_of(0), ir.nidx[0], ir.space[0]), (STRUCT, String::new(), NO_INDEX, 0));
    assert_eq!((ir.start[0], ir.len[0]), (0, 64));
    assert_eq!(ir.path(0), "");
    // siblings that share a prefix, but not one followed by "/"
    ir.value(0, "a", NO_INDEX, "u1", 0, (0, 1), None).unwrap();
    ir.value(0, "ab", NO_INDEX, "u1", 0, (1, 1), None).unwrap();
    ir.value(0, "a~", 1, "u1", 0, (2, 1), None).unwrap();
    // names that hold "/", none of them another's followed by "/"
    let t = ir.struct_(0, "tags/", 256, 0, Some((3, 2))).unwrap();
    ir.value(t, "entry", NO_INDEX, "u1", 0, (3, 1), None).unwrap();
    ir.value(0, "tags/", 257, "u1", 0, (5, 1), None).unwrap();
    // an empty name with an index: the full name is the index
    let f = ir.struct_(0, "frames", NO_INDEX, 0, None).unwrap();
    ir.value(f, "", 0, "u1", 0, (6, 1), None).unwrap();
    ir.value(f, "", 1, "u1", 0, (7, 1), None).unwrap();
    // one name under different parents
    ir.value(t, "a", NO_INDEX, "u1", 0, (4, 1), None).unwrap();
    // a run of 3 members `tiles/0` to `tiles/2`, each with a child `h`, beside `tiles/3`
    let r = ir.struct_(0, "tiles/", 0, 0, Some((8, 4))).unwrap();
    ir.value(r, "h", NO_INDEX, "bytes[4]", 0, (8, 4), None).unwrap();
    ir.runs.push((r, 3, 4));
    ir.value(0, "tiles/", 3, "bytes[4]", 0, (20, 4), None).unwrap();
    finish(&mut ir).unwrap();
    names(&ir).unwrap();
    check(&ir).unwrap();
    assert_eq!(ir.path(5), "tags/256/entry");
    assert_eq!(ir.path(8), "frames/0");
    // the names a source gives, made unique, and read back
    let texts = ["a", "", "a", "x/y", "~z", "header", "", "50%", "a%2F"];
    let got = source_names(&texts, &["header"]);
    let full: Vec<String> = got.iter().map(|(n, k)| if *k == NO_INDEX { n.clone() } else { format!("{n}{k}") }).collect();
    assert_eq!(full, ["a", "~0", "a~1", "x%2Fy~0", "%7Ez~0", "header~0", "~1", "50%", "a%2F"]);
    for ((n, k), t) in got.iter().zip(texts) {
        assert_eq!(source_text(n, *k), t);
    }
}

#[test]
fn a_root_of_another_kind_is_invalid() {
    let mut ir = named();
    ir.kind[0] = GAP;
    assert_eq!(names(&ir).unwrap_err(), "the root (element 0) is a gap, not a struct");
}

#[test]
fn a_named_root_is_invalid() {
    let mut ir = named();
    ir.name[0] = ir.names.id("r");
    assert_eq!(names(&ir).unwrap_err(), "the root (element 0) is named \"r\", not \"\"");
    assert_eq!(check(&ir).unwrap_err(), "the root (element 0) is named \"r\", not \"\"");
}

#[test]
fn a_root_with_an_index_is_invalid() {
    let mut ir = named();
    ir.nidx[0] = 0;
    assert_eq!(check(&ir).unwrap_err(), "the root (element 0) has the name index 0");
}

#[test]
fn a_root_in_a_derived_space_is_invalid() {
    let mut ir = named();
    ir.space[0] = 1;
    assert_eq!(check(&ir).unwrap_err(), "the root (element 0) is in space 1, not 0");
}

#[test]
fn a_root_that_does_not_span_the_source_is_invalid() {
    let mut ir = named();
    ir.len[0] = 0;
    assert_eq!(check(&ir).unwrap_err(), "the root's extent is (0, 0), not (0, 30): the root spans the source");
    ir.start[0] = 10;
    ir.len[0] = 20;
    assert_eq!(check(&ir).unwrap_err(), "the root's extent is (10, 20), not (0, 30): the root spans the source");
}

#[test]
fn a_root_that_is_a_run_is_invalid() {
    let mut ir = named();
    ir.runs.push((0, 2, 30));
    assert_eq!(names(&ir).unwrap_err(), "the root (element 0) is a run");
}

#[test]
fn an_element_with_an_empty_full_name_is_invalid() {
    let mut ir = named();
    ir.name[1] = ir.names.id("");
    assert_eq!(check(&ir).unwrap_err(), "element 1 (value \"\") has an empty full name");
}

#[test]
fn siblings_of_one_path_are_invalid() {
    // one name, twice
    let mut ir = named();
    ir.name[2] = ir.names.id("a");
    ir.nidx[2] = NO_INDEX;
    assert_eq!(check(&ir).unwrap_err(), "element 1 (value \"a\") and element 2 (gap \"a\") have one path, \"a\"");
    // a name and an index that spell another's full name: `a1` and index 3, `a` and index 13
    let mut ir = Ir::new(2);
    let s = ir.struct_(0, "s", NO_INDEX, 0, None).unwrap();
    ir.value(s, "a1", 3, "u1", 0, (0, 1), None).unwrap();
    ir.value(s, "a", 13, "u1", 0, (1, 1), None).unwrap();
    assert_eq!(check(&ir).unwrap_err(), "element 2 (value \"s/a13\") and element 3 (value \"s/a13\") have one path, \"s/a13\"");
    // a member of a run and a sibling: `t2` is member 2 of the run `t0`
    let mut ir = Ir::new(4);
    let r = ir.value(0, "t", 0, "u1", 0, (0, 1), None).unwrap();
    ir.runs.push((r, 3, 1));
    ir.value(0, "t", 2, "u1", 0, (3, 1), None).unwrap();
    assert_eq!(check(&ir).unwrap_err(), "member 2 of the run element 1 (value \"t0\") and element 2 (value \"t2\") have one path, \"t2\"");
}

#[test]
fn a_full_name_that_continues_a_siblings_with_a_slash_is_invalid() {
    let mut ir = named();
    ir.name[2] = ir.names.id("a/");
    ir.nidx[2] = 0;
    assert_eq!(
        check(&ir).unwrap_err(),
        "the full name \"a/0\" of element 2 (gap \"a/0\") starts with that of its sibling element 1 (value \"a\") and \"/\""
    );
}
