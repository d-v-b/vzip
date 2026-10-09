//! The compact IR's invariants with runs: folding, finish (gap runs, unfolding,
//! aliases), check and the rebuild's leaves.

use vzip_ir::check::{check, finish, leaves};
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
