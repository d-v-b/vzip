//! The read planner's policy (src/plan.rs): one test over the servers it plans
//! for, and one per error.

use vzip_ir::plan::{plan_spans, Planner, Request, CEILING};

/// Serves a planner's rounds from `data`, `seconds` per request (a multi-range
/// request refused when `packs` is false), and gives the batch's bytes.
fn serve(p: &mut Planner, data: &[u8], packs: bool, seconds: impl Fn(u64) -> f64) -> Vec<(u64, Vec<u8>)> {
    loop {
        let reqs = p.requests();
        if reqs.is_empty() {
            return p.take().unwrap();
        }
        for (k, q) in reqs.iter().enumerate() {
            if q.spans.len() > 1 && !packs {
                p.complete(k, None, 0.01).unwrap();
                continue;
            }
            let n: u64 = q.spans.iter().map(|s| s.1 - s.0).sum();
            let parts = q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect();
            p.complete(k, Some(parts), seconds(n)).unwrap();
        }
    }
}

#[test]
fn plans_by_the_server() {
    let data: Vec<u8> = (0..(64u64 << 20)).map(|i| (i % 251) as u8).collect();
    let size = data.len() as u64;
    // headers of 512 bytes every 64 KiB: 1/128 of the file
    let heads: Vec<(u64, u64)> = (0..1000u64).map(|k| (k << 16, 512)).collect();
    let asked: u64 = heads.iter().map(|r| r.1).sum();
    // EBI's profile: 65 ms a request, 2.5 MB/s a connection
    let ebi = |n: u64| 0.065 + n as f64 / 2.5e6;

    // (server: remote, multi-range, calibrate first) -> what the plan must do
    for (remote, packs, warm) in [(false, false, false), (true, false, false), (true, false, true), (true, true, false)] {
        let mut p = Planner::new(size, remote, 6);
        if warm {
            for _ in 0..4 {
                p.observe(4096, 0.065);
            }
            p.observe(1 << 20, ebi(1 << 20));
            p.multirange = Some(false);
            p.byte_cost = 0.0; // bytes free: the time-optimal plan, which the ceiling bounds
        }
        p.begin(heads.clone(), false);
        let got = serve(&mut p, &data, packs, ebi);
        // every range's bytes, in the order asked
        assert_eq!(got.len(), heads.len());
        for ((o, b), (ro, rn)) in got.iter().zip(&heads) {
            assert_eq!((o, b.len() as u64), (ro, *rn));
            assert_eq!(b[..], data[*o as usize..(*o + *rn) as usize]);
        }
        let st = &p.stats;
        let (threshold, cap) = p.policy(heads.len());
        match (remote, packs, warm) {
            (false, ..) => assert!(st.bytes <= 4 * asked + (1 << 16)),
            (true, false, false) => {
                // cold: a probe of two ranges (refused), a calibration slice, then the cap of 4
                assert!(p.calibrated());
                assert_eq!(p.multirange, Some(false));
                assert!(st.bytes <= CEILING as u64 * asked + (1 << 16));
            }
            (true, false, true) => {
                // calibrated on a single-range server, bytes free: gaps under L × B (162 KB) merge, within 16x
                assert_eq!(cap, CEILING);
                assert!((threshold as f64 / (0.065 * 2.5e6) - 1.0).abs() < 0.01, "{threshold}");
                assert!(st.bytes <= CEILING as u64 * asked + (1 << 16));
                assert!(st.bytes < size / 4, "header reads never fetch the file");
                // merging every gap would pay (65 KB < 162 KB) but read 128x: the ceiling stops it
                assert!(st.bytes > (CEILING as u64 - 2) * asked && st.requests < 1000);
                // a batch whose requests cost less than half a second: gaps to 4 KiB, the cap of 4
                assert_eq!(p.policy(10), (4 << 10, 4.0));
                // at the default cost of bytes (0.1 s per MB), a gap pays below
                // L / (1 / B + 6 × 0.1 µs) = 65 KB, not 162 KB: gaps of 130 KB, which
                // the time-optimal plan merges, do not
                p.byte_cost = vzip_ir::plan::BYTE_COST;
                let t = p.policy(heads.len()).0 as f64;
                assert!((t / 65_000.0 - 1.0).abs() < 0.01, "{t}");
                let sparse: Vec<(u64, u64)> = (0..400u64).map(|k| (k << 17, 512)).collect();
                p.begin(sparse.clone(), false);
                let before = p.stats.bytes;
                serve(&mut p, &data, false, ebi);
                assert_eq!(p.stats.bytes - before, 512 * 400);
            }
            (true, true, _) => {
                // packed: exact ranges, at most 100 to a request
                assert_eq!(p.multirange, Some(true));
                assert_eq!((threshold, cap), (512, 4.0));
                assert_eq!(st.bytes, asked);
                assert!(st.packed >= 1 && st.requests <= 1 + 6 * 2);
            }
        }
    }

    // a small source (at most 1 MiB): its first batch reads what the opening request
    // did not hold, in one request, and every batch is served from it
    let small = &data[..700_000];
    let mut p = Planner::new(small.len() as u64, true, 6);
    p.seed(0, small[..1 << 16].to_vec());
    for batch in [vec![(100_000u64, 10u64), (600_000, 50)], vec![(300_000, 7)]] {
        p.begin(batch.clone(), false);
        let got = serve(&mut p, small, false, ebi);
        for ((o, b), (ro, rn)) in got.iter().zip(&batch) {
            assert_eq!((*o, b.len() as u64), (*ro, *rn));
            assert_eq!(b[..], small[*o as usize..(*o + rn) as usize]);
        }
    }
    assert_eq!((p.stats.requests, p.stats.bytes), (1, 700_000 - (1 << 16)));

    // a walk: one-range batches, each where the last ended, are served from a window
    let mut p = Planner::new(size, true, 6);
    p.multirange = Some(false);
    let mut requests = 0;
    for k in 0..4000u64 {
        p.begin(vec![(k * 32, 32)], false);
        requests += p.requests().len();
        let got = serve(&mut p, &data, false, ebi);
        assert_eq!(got[0].1[..], data[(k * 32) as usize..(k * 32 + 32) as usize]);
    }
    assert_eq!(p.stats.requests, 3);
    assert!(requests >= 3);

    // the merge itself: (ranges, threshold, cap) -> requests
    let cases: Vec<(Vec<(u64, u64)>, u64, f64, Vec<(u64, u64)>)> = vec![
        (vec![(0, 100), (200, 100)], 10_000, 4.0, vec![(0, 300)]),
        (vec![(0, 100), (20_000, 100)], 10_000, 4.0, vec![(0, 100), (20_000, 20_100)]),
        // three gaps of 5000, 6000, 7000 bytes: within the cap of 3 x 400 + 64 KiB, all merge
        (vec![(0, 100), (5100, 100), (11_200, 100), (18_300, 100)], 10_000, 4.0, vec![(0, 18_400)]),
        // at amplification 1 only 64 KiB of gaps may be fetched: the smallest first
        (vec![(0, 10), (60_000, 10), (69_010, 10), (77_020, 10)], 100_000, 1.0, vec![(0, 10), (60_000, 77_030)]),
        // overlapping and touching ranges are one span
        (vec![(0, 10), (5, 10), (15, 5)], 10_000, 4.0, vec![(0, 20)]),
        // a large read is shared: joins stop at about total / concurrency (1 MiB at least)
        ((0..64u64).map(|k| (k << 20, (1 << 20) - 100)).collect(), 1 << 20, 16.0,
         (0..16u64).map(|k| (k << 22, ((k + 1) << 22) - 100)).collect()),
    ];
    for (ranges, t, cap, want) in cases {
        let conc = if ranges.len() == 64 { 16 } else { 1 };
        assert_eq!(plan_spans(&ranges, t, cap, conc), want, "{ranges:?}");
    }
}

fn started() -> Planner {
    let mut p = Planner::new(64 << 20, true, 2);
    p.multirange = Some(false);
    p.begin(vec![(0, 10), (100_000, 10)], false);
    p
}

#[test]
fn rejects_a_response_of_the_wrong_length() {
    let mut p = started();
    let r: Vec<Request> = p.requests();
    assert_eq!(p.complete(0, Some(vec![vec![0; 3]]), 0.1).unwrap_err(), "a response of the wrong length");
    assert!(!r.is_empty());
}

#[test]
fn rejects_a_request_completed_twice() {
    let mut p = Planner::new(64 << 20, true, 2);
    p.multirange = Some(false);
    p.begin(vec![(0, 10), (100_000, 10), (200_000, 10)], false);
    let r = p.requests();
    assert!(r.len() > 1);
    let n = (r[0].spans[0].1 - r[0].spans[0].0) as usize;
    p.complete(0, Some(vec![vec![0; n]]), 0.1).unwrap();
    assert_eq!(p.complete(0, Some(vec![vec![0; n]]), 0.1).unwrap_err(), "a request completed twice");
}

#[test]
fn rejects_an_unanswered_range_request() {
    let mut p = started();
    p.requests();
    assert_eq!(p.complete(0, None, 0.1).unwrap_err(), "a range request was not answered");
}

#[test]
fn rejects_past_the_read_budget() {
    // a 64-byte file: at most 2^24 + 128 bytes may be read
    let mut p = Planner::new(64, false, 1);
    let mut err = None;
    for _ in 0..(1 << 21) {
        p.begin(vec![(0, 64)], false);
        let r = p.requests();
        if let Err(e) = p.complete(0, Some(vec![vec![0; 64]]), 0.0) {
            err = Some(e);
            break;
        }
        assert_eq!(r.len(), 1);
        p.take().unwrap();
    }
    assert!(err.unwrap().starts_with("budget: more than"));
}

#[test]
fn rejects_taking_an_incomplete_batch() {
    let mut p = started();
    p.requests();
    assert_eq!(p.take().unwrap_err(), "the batch is not complete");
}
