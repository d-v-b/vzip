//! The read planner's policy (ARCHITECTURE.md §3.5), sans-IO: which requests to
//! make for a parser's batch of ranges. The host performs the requests (HTTP,
//! a local file, the browser's `fetch`), reports each one's bytes and time,
//! and gets the batch's bytes back; the planner never reads.
//!
//! - **Coalescing by cost.** A request costs a latency `L` and a byte costs
//!   `1 / B` (per connection), and every byte fetched also costs `byte_cost`
//!   seconds (metered or polite access: by default 0.1 s per MB, set by the host).
//!   Merging two ranges saves a request and fetches the gap `g`, so with `c`
//!   connections it pays when `g < L / (1 / B + c × byte_cost)` (`paying_gap`). `L` is the median time of small
//!   requests, `B` the median rate of large ones, calibrated from the requests
//!   made (the host also reports the requests it makes itself, such as the one
//!   that opens the source).
//! - **The amplification cap depends on the server.** A batch fetches at most
//!   `cap` times the bytes it asks for, plus 64 KiB, the smallest gaps merged
//!   first. Where the server packs several ranges in one request
//!   (`multipart/byteranges`), a request carries ranges, not gaps: the gap
//!   threshold is 512 bytes and the cap 4. Where it serves one range a request,
//!   the cap is the cost model's: when the batch's requests cost at least half a second
//!   (`n × L / concurrency`), gaps up to `paying_gap` merge, within a hard ceiling
//!   of 16 (`CEILING`); when they cost less, merging would save little and read
//!   more, so gaps up to `min(paying_gap, 4 KiB)` merge within a cap of 4. With the
//!   ceiling, a batch can fetch a whole file only when it asks for at least a
//!   sixteenth of it, so header reads (a few hundred bytes per tile, frame or
//!   subblock of tens of kilobytes or more) never fetch the file; and the gap is
//!   capped at 8 MiB, so one slow sample cannot merge everything.
//! - **Conservative until calibrated.** Until the model has the server's latency
//!   and rate, a remote batch is planned with a gap threshold of 4 KiB and a cap
//!   of 4, and a large batch first sends `concurrency` exact requests to
//!   calibrate it (the first one 256 KiB long when no request has measured the
//!   rate yet and the batch's requests would cost at least a second). A local
//!   file has fixed costs.
//! - **Multi-range requests.** Whether the server answers several ranges with
//!   `multipart/byteranges` is probed once, with two ranges; then a batch's
//!   requests are packed, as few to a pack as keeps every connection busy, at
//!   most 100 ranges each.
//! - **Balance.** Joins stop at about the batch's bytes over `concurrency` (from
//!   1 MiB to 32 MiB), so that a batch that reads much keeps every connection
//!   busy; a range asked is never cut.
//! - **Small sources.** A remote source of at most `whole_below` bytes (1 MiB) is
//!   read whole: the first batch fetches what the opening request did not, and
//!   every batch is served from it. Larger sources are unaffected.
//! - **Sequential read-ahead.** A batch of one range that starts where the
//!   previous one ended is a walk: the planner reads ahead a window that doubles
//!   (64 KiB to 8 MiB) and serves the walk from it.
//! - **Budget.** Every request and byte is charged: requests at most
//!   2^20 + size / 64, bytes at most 2^24 + 2 × size.

use serde_json::json;
use std::collections::HashMap;

pub const MAX_RANGES: usize = 100;
/// The hard ceiling of amplification on a server of single ranges.
pub const CEILING: f64 = 16.0;
const SLACK: u64 = 1 << 16;
const PACKED_GAP: u64 = 512;
const PACKED_CAP: f64 = 4.0;
const COLD_GAP: u64 = 4 << 10;
const COLD_CAP: f64 = 4.0;
const MIN_GAP: u64 = 4 << 10;
const MAX_GAP: u64 = 8 << 20;
const WARM_GAP: u64 = 4 << 10;
/// The default cost of a byte fetched: 0.1 s of wall time per MB (10^6 bytes), so a
/// megabyte read beyond what is asked must save at least a tenth of a second. It
/// stands for metered or polite access (egress paid for, a shared server); a host
/// with free bytes may set 0, which gives the time-optimal plan.
pub const BYTE_COST: f64 = 0.1e-6;
/// Remote sources of at most this many bytes are read whole.
pub const WHOLE_BELOW: u64 = 1 << 20;
/// Seconds of request latency a batch must cost before gaps are fetched to save requests.
const DOMINATE: f64 = 0.5;
const MIN_PIECE: u64 = 1 << 20;
const MAX_PIECE: u64 = 32 << 20;
/// Requests of at most this many bytes time the latency; of at least, the rate.
const SMALL: u64 = 16 << 10;
const LARGE: u64 = 64 << 10;
/// The request that measures a server's rate, when no request has.
const RATE_PROBE: u64 = 256 << 10;

/// A request: one span (a plain range request), or several (a multi-range request).
#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    pub spans: Vec<(u64, u64)>, // (start, end)
}

#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub batches: u64,
    pub ranges: u64,
    pub asked: u64,
    pub requests: u64,
    pub packed: u64,
    pub bytes: u64,
    pub served_ahead: u64,
    pub held_max: u64,
    pub threshold: u64,
    pub cap: f64,
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Stage {
    Probe,
    Calibrate,
    Main,
    Done,
}

/// The policy's view of the server, the batch in progress, and the counts.
pub struct Planner {
    pub size: u64,
    pub remote: bool,
    pub concurrency: usize,
    /// Whether the server answers multi-range requests (None: not probed yet).
    pub multirange: Option<bool>,
    /// A fixed cap, for experiments (VZIP_AMPLIFICATION).
    pub cap_override: Option<f64>,
    /// The cost of a byte fetched, as seconds of wall time (the host may set it; see
    /// BYTE_COST): a gap is fetched to save a request only when the time saved pays
    /// for its bytes at this rate.
    pub byte_cost: f64,
    /// A remote source of at most this many bytes is read whole, in one request (or
    /// two: the one that opened it, and the rest).
    pub whole_below: u64,
    pub latency: f64,
    pub bandwidth: f64,
    lat: Vec<f64>,
    rates: Vec<f64>,
    /// the fastest request so far (it bounds the latency)
    fastest: f64,
    ahead: (u64, Vec<u8>),
    window: u64,
    last_end: Option<u64>,
    reads_limit: u64,
    bytes_limit: u64,
    pub stats: Stats,
    stage: Stage,
    whole: bool,
    asked: Vec<(u64, u64)>,
    out: HashMap<(u64, u64), Vec<u8>>,
    responses: Vec<(u64, Vec<u8>)>,
    todo: Vec<(u64, u64)>,
    round: Vec<Request>,
    got: Vec<Option<Option<Vec<Vec<u8>>>>>,
    /// a walk: the range asked (its window is the round's one request)
    walk: Option<(u64, u64)>,
    /// a small source read whole: where the round's one request starts
    fill: Option<u64>,
}

fn median(v: &[f64]) -> f64 {
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = s.len();
    if n % 2 == 1 { s[n / 2] } else { (s[n / 2 - 1] + s[n / 2]) / 2.0 }
}

impl Planner {
    pub fn new(size: u64, remote: bool, concurrency: usize) -> Self {
        Planner {
            size,
            remote,
            concurrency: concurrency.max(1),
            multirange: if remote { None } else { Some(false) },
            cap_override: None,
            byte_cost: BYTE_COST,
            whole_below: WHOLE_BELOW,
            latency: if remote { 0.05 } else { 2e-5 },
            bandwidth: if remote { 2e6 } else { 2e9 },
            lat: Vec::new(),
            rates: Vec::new(),
            fastest: f64::INFINITY,
            ahead: (0, Vec::new()),
            window: 1 << 16,
            last_end: None,
            reads_limit: (1 << 20) + size / 64,
            bytes_limit: (1 << 24) + 2 * size,
            stats: Stats::default(),
            stage: Stage::Done,
            whole: false,
            asked: Vec::new(),
            out: HashMap::new(),
            responses: Vec::new(),
            todo: Vec::new(),
            round: Vec::new(),
            got: Vec::new(),
            walk: None,
            fill: None,
        }
    }

    /// Bytes the host already holds (such as those of the request that opened the
    /// source): ranges inside them are served without a request.
    pub fn seed(&mut self, offset: u64, data: Vec<u8>) {
        self.ahead = (offset, data);
    }

    /// Whether the model has the server's latency and rate.
    pub fn calibrated(&self) -> bool {
        !self.remote || (self.lat.len() >= 4 && !self.rates.is_empty())
    }

    /// A single-range request of `n` bytes that took `seconds`, for the cost model
    /// (the planner's own, and those the host makes, such as the one that opens the source).
    pub fn observe(&mut self, n: u64, seconds: f64) {
        if !self.remote || seconds.is_nan() || seconds <= 0.0 {
            return;
        }
        self.fastest = self.fastest.min(seconds);
        if n <= SMALL {
            self.lat.push(seconds);
            if self.lat.len() > 256 {
                self.lat.remove(0);
            }
            if self.lat.len() >= 4 {
                self.latency = median(&self.lat);
            }
        }
        if n >= LARGE {
            // the time past the latency is the transfer's (the fastest request so far
            // bounds the latency until there are enough small ones)
            let l = if self.lat.len() >= 4 { self.latency } else { self.fastest };
            let l = if l.is_finite() && l < seconds { l } else { 0.0 };
            let t = seconds - l;
            if t > 0.0 {
                self.rates.push(n as f64 / t);
                if self.rates.len() > 64 {
                    self.rates.remove(0);
                }
                self.bandwidth = median(&self.rates);
            }
        }
    }

    /// (gap threshold, amplification cap) for the next plan, of `n` ranges.
    pub fn policy(&self, n: usize) -> (u64, f64) {
        let (gap, cap) = if !self.remote {
            // a local file: a request costs about 20 µs, a byte 0.5 ns
            ((self.latency * self.bandwidth / self.concurrency as f64).max(4096.0) as u64, 4.0)
        } else if self.multirange == Some(true) {
            (PACKED_GAP, PACKED_CAP)
        } else if !self.calibrated() {
            (COLD_GAP, COLD_CAP)
        } else if !self.requests_dominate(n) {
            // the batch's requests cost little: merging more would save little and read more
            (self.paying_gap().clamp(MIN_GAP as f64, WARM_GAP as f64) as u64, COLD_CAP)
        } else {
            (self.paying_gap().clamp(MIN_GAP as f64, MAX_GAP as f64) as u64, CEILING)
        };
        (gap, self.cap_override.unwrap_or(cap))
    }

    /// The largest gap worth fetching to save a request. With c connections, a batch
    /// costs about (requests × L + bytes / B) / c of wall time, plus `byte_cost` per
    /// byte fetched; a merge saves a request and fetches the gap g, so it pays when
    /// (g / B − L) / c + byte_cost × g < 0, that is g < L / (1 / B + c × byte_cost).
    pub fn paying_gap(&self) -> f64 {
        self.latency / (1.0 / self.bandwidth + self.concurrency as f64 * self.byte_cost)
    }

    /// Whether `n` requests cost at least a second on this server (their latency,
    /// shared by the connections): then fetching gaps to save requests is worth it.
    pub fn requests_dominate(&self, n: usize) -> bool {
        // before the latency is known, the fastest request so far bounds it
        let l = if self.lat.len() >= 4 { self.latency } else { self.latency.min(self.fastest) };
        n as f64 * l / self.concurrency as f64 >= DOMINATE
    }

    /// The requests (start, end) for ranges (offset, length), sorted and merged.
    pub fn plan(&self, ranges: &[(u64, u64)]) -> Vec<(u64, u64)> {
        let (threshold, cap) = self.policy(ranges.len());
        plan_spans(ranges, threshold, cap, self.concurrency)
    }

    fn charge(&mut self, n: u64) -> Result<(), String> {
        self.stats.requests += 1;
        self.stats.bytes += n;
        if self.stats.requests > self.reads_limit {
            return Err(format!("budget: more than {} reads", self.reads_limit));
        }
        if self.stats.bytes > self.bytes_limit {
            return Err(format!("budget: more than {} read", self.bytes_limit));
        }
        Ok(())
    }

    /// The counts and the model, as JSON.
    pub fn stats_json(&self) -> serde_json::Value {
        let s = &self.stats;
        json!({"batches": s.batches, "ranges": s.ranges, "asked": s.asked, "requests": s.requests,
               "multirange": s.packed, "bytes": s.bytes, "served_ahead": s.served_ahead, "held_max": s.held_max,
               "latency": self.latency, "bandwidth": self.bandwidth, "threshold": s.threshold, "cap": s.cap,
               "byte_cost": self.byte_cost, "concurrency": self.concurrency,
               "packs": self.multirange})
    }

    // ---- a batch

    /// Starts a batch: the ranges (offset, length) a parser asks for. With `whole`, the
    /// batch gives the responses as fetched (merged ranges are not cut; the parser
    /// finds each range in them), else each range's bytes.
    pub fn begin(&mut self, ranges: Vec<(u64, u64)>, whole: bool) {
        self.stats.batches += 1;
        self.stats.ranges += ranges.len() as u64;
        self.stats.asked += ranges.iter().map(|r| r.1).sum::<u64>();
        self.whole = whole;
        self.out.clear();
        self.responses.clear();
        self.todo.clear();
        self.walk = None;
        let (a0, alen) = (self.ahead.0, self.ahead.1.len() as u64);
        for &(o, n) in &ranges {
            if n == 0 {
                self.out.insert((o, n), Vec::new());
            } else if a0 <= o && o + n <= a0 + alen {
                let b = self.ahead.1[(o - a0) as usize..(o - a0 + n) as usize].to_vec();
                self.out.insert((o, n), b);
                self.stats.served_ahead += 1;
            } else {
                self.todo.push((o, n));
            }
        }
        self.asked = ranges;
        let end = self.last_end.unwrap_or(0).max(a0 + alen);
        let walk = self.todo.len() == 1
            && self.last_end.is_some()
            && self.todo[0].0 >= end
            && self.todo[0].0 - end <= 512
            && self.todo[0].1 < self.window;
        if walk {
            let (o, n) = self.todo[0];
            let e = self.size.min(o + n.max(self.window));
            self.window = (self.window * 2).min(8 << 20);
            self.walk = Some((o, n));
            self.todo.clear();
            self.stage = Stage::Done;
            self.round = vec![Request { spans: vec![(o, e)] }];
            return;
        }
        if self.remote && !self.todo.is_empty() && self.size <= self.whole_below {
            // a small source: read the rest of it in one request, and serve every batch from it
            let from = if a0 == 0 { alen } else { 0 };
            self.fill = Some(from);
            self.stage = Stage::Done;
            self.round = vec![Request { spans: vec![(from, self.size)] }];
            return;
        }
        if !self.todo.is_empty() {
            self.ahead = (0, Vec::new());
            self.window = 1 << 16;
        }
        self.stage = Stage::Probe;
        self.round.clear();
        self.next_stage();
    }

    fn next_stage(&mut self) {
        loop {
            match self.stage {
                Stage::Probe => {
                    self.stage = Stage::Calibrate;
                    if self.todo.len() > 1 && self.multirange.is_none() {
                        // whether the server packs ranges decides how far to coalesce: ask it first
                        let mut first = self.todo.clone();
                        first.sort();
                        first.dedup();
                        if first.len() > 1 {
                            let spans: Vec<(u64, u64)> = first[..2].iter().map(|&(o, n)| (o, o + n)).collect();
                            if spans[0].1 <= spans[1].0 {
                                self.round = vec![Request { spans }];
                                return;
                            }
                        }
                    }
                }
                Stage::Calibrate => {
                    self.stage = Stage::Main;
                    let c = self.concurrency;
                    if self.remote && self.multirange != Some(true) && !self.calibrated() && self.todo.len() > 4 * c {
                        // calibrate the model on a first slice of exact requests, then plan the rest
                        let mut first = self.todo.clone();
                        first.sort();
                        first.dedup();
                        first.truncate(c);
                        self.round = first.iter().map(|&(o, n)| Request { spans: vec![(o, o + n)] }).collect();
                        if self.rates.is_empty() && self.requests_dominate(self.todo.len()) {
                            // no rate yet: the first request reads 256 KiB (the ranges in them are served)
                            let s = &mut self.round[0].spans[0];
                            s.1 = s.1.max(self.size.min(s.0 + RATE_PROBE));
                        }
                        return;
                    }
                }
                Stage::Main => {
                    self.stage = Stage::Done;
                    if self.todo.is_empty() {
                        continue;
                    }
                    let spans = self.plan(&self.todo);
                    let (t, cap) = self.policy(self.todo.len());
                    self.stats.threshold = t;
                    self.stats.cap = cap;
                    let held: u64 = spans.iter().map(|s| s.1 - s.0).sum();
                    self.stats.held_max = self.stats.held_max.max(held + self.ahead.1.len() as u64);
                    self.round = self.packs(spans);
                    return;
                }
                Stage::Done => {
                    self.round.clear();
                    return;
                }
            }
        }
    }

    /// Packs requests into multi-range requests where the server answers them.
    fn packs(&self, spans: Vec<(u64, u64)>) -> Vec<Request> {
        if self.multirange == Some(true) && spans.len() > 1 {
            let size = spans.len().div_ceil(self.concurrency).clamp(1, MAX_RANGES);
            spans.chunks(size).map(|c| Request { spans: c.to_vec() }).collect()
        } else {
            spans.into_iter().map(|s| Request { spans: vec![s] }).collect()
        }
    }

    /// The spans of request `k` of the round in progress.
    pub fn round_spans(&self, k: usize) -> Vec<(u64, u64)> {
        self.round.get(k).map(|r| r.spans.clone()).unwrap_or_default()
    }

    /// The requests to make now, concurrently (empty when the batch is complete).
    pub fn requests(&mut self) -> Vec<Request> {
        self.got = vec![None; self.round.len()];
        self.round.clone()
    }

    /// Request `k` of the round completed: each span's bytes, or None for a
    /// multi-range request the server did not answer with its ranges.
    pub fn complete(&mut self, k: usize, data: Option<Vec<Vec<u8>>>, seconds: f64) -> Result<(), String> {
        let req = self.round.get(k).ok_or("no such request")?.clone();
        if self.got.get(k).is_none_or(|g| g.is_some()) {
            return Err("a request completed twice".into());
        }
        match &data {
            Some(parts) => {
                if parts.len() != req.spans.len() || parts.iter().zip(&req.spans).any(|(p, s)| p.len() as u64 != s.1 - s.0) {
                    return Err("a response of the wrong length".into());
                }
                let n: u64 = parts.iter().map(|p| p.len() as u64).sum();
                self.charge(n)?;
                if req.spans.len() > 1 {
                    self.stats.packed += 1;
                } else {
                    self.observe(n, seconds);
                }
            }
            None => {
                if req.spans.len() < 2 {
                    return Err("a range request was not answered".into());
                }
                self.charge(0)?;
            }
        }
        self.got[k] = Some(data);
        if self.got.iter().all(|g| g.is_some()) {
            self.round_done();
        }
        Ok(())
    }

    fn round_done(&mut self) {
        let round = std::mem::take(&mut self.round);
        let got = std::mem::take(&mut self.got);
        if let Some(from) = self.fill.take() {
            let buf = got.into_iter().next().flatten().flatten().and_then(|v| v.into_iter().next()).unwrap_or_default();
            let mut all = if from > 0 { std::mem::take(&mut self.ahead.1) } else { Vec::new() };
            all.extend_from_slice(&buf);
            self.ahead = (0, all);
            for (o, n) in std::mem::take(&mut self.todo) {
                let b = self.ahead.1[o as usize..(o + n) as usize].to_vec();
                if self.whole {
                    self.responses.push((o, b));
                } else {
                    self.out.insert((o, n), b);
                }
            }
            return;
        }
        if let Some((o, n)) = self.walk.take() {
            // the walk's window: hand out the range, keep the window ahead
            let buf = got.into_iter().next().flatten().flatten().and_then(|v| v.into_iter().next()).unwrap_or_default();
            if self.whole {
                self.responses.push((o, buf[..n as usize].to_vec()));
            } else {
                self.out.insert((o, n), buf[..n as usize].to_vec());
            }
            self.ahead = (o, buf);
            return;
        }
        let mut refused = false;
        for (req, g) in round.iter().zip(got) {
            match g.unwrap() {
                Some(parts) => {
                    if req.spans.len() > 1 {
                        self.multirange = Some(true);
                    }
                    for (s, b) in req.spans.iter().zip(parts) {
                        self.accept(*s, b);
                    }
                }
                None => {
                    self.multirange = Some(false);
                    refused = true;
                }
            }
        }
        if refused && self.stage == Stage::Done {
            // a pack the server refused: plan what is left as single ranges
            self.stage = Stage::Main;
        }
        self.next_stage();
    }

    /// A span's bytes: the ranges asked that it holds are done.
    fn accept(&mut self, (s, e): (u64, u64), b: Vec<u8>) {
        let mut left = Vec::with_capacity(self.todo.len());
        let mut used = false;
        for &(o, n) in &self.todo {
            if o >= s && o + n <= e {
                used = true;
                if !self.whole {
                    self.out.insert((o, n), b[(o - s) as usize..(o - s + n) as usize].to_vec());
                }
            } else {
                left.push((o, n));
            }
        }
        self.todo = left;
        if self.whole && used {
            self.responses.push((s, b));
        }
    }

    /// The batch's bytes, once `requests()` is empty: each range asked (offset, bytes),
    /// in the order asked; with `whole`, the ranges served from the read-ahead window,
    /// then the responses as fetched.
    pub fn take(&mut self) -> Result<Vec<(u64, Vec<u8>)>, String> {
        if !self.round.is_empty() || !self.todo.is_empty() {
            return Err("the batch is not complete".into());
        }
        if let Some(m) = self.asked.iter().map(|r| r.0 + r.1).max() {
            self.last_end = Some(m);
        }
        let asked = std::mem::take(&mut self.asked);
        if self.whole {
            let mut v: Vec<(u64, Vec<u8>)> = Vec::new();
            for r in &asked {
                if let Some(b) = self.out.remove(r) {
                    v.push((r.0, b));
                }
            }
            v.append(&mut self.responses);
            return Ok(v);
        }
        let mut v = Vec::with_capacity(asked.len());
        for r in &asked {
            match self.out.get(r) {
                Some(b) => v.push((r.0, b.clone())),
                None => return Err(format!("internal: no bytes for [{}, {})", r.0, r.0 + r.1)),
            }
        }
        self.out.clear();
        Ok(v)
    }
}

/// Merges ranges (offset, length) into requests (start, end): overlapping and
/// touching ranges are one span; gaps of at most `threshold` merge, smallest
/// first, while the bytes fetched stay within `cap` times those asked plus 64 KiB;
/// joins stop at about total / `concurrency` bytes, so that a large read is shared
/// by the connections.
pub fn plan_spans(ranges: &[(u64, u64)], threshold: u64, cap: f64, concurrency: usize) -> Vec<(u64, u64)> {
    let mut spans: Vec<(u64, u64)> = ranges.iter().filter(|r| r.1 > 0).map(|&(o, n)| (o, o + n)).collect();
    spans.sort();
    let mut merged: Vec<(u64, u64)> = Vec::new();
    for (o, e) in spans {
        match merged.last_mut() {
            Some(m) if o <= m.1 => m.1 = m.1.max(e),
            _ => merged.push((o, e)),
        }
    }
    let asked: u64 = merged.iter().map(|m| m.1 - m.0).sum();
    let mut allowance = ((cap - 1.0).max(0.0) * asked as f64) as u64 + SLACK;
    let mut gaps: Vec<(u64, usize)> = (0..merged.len().saturating_sub(1)).map(|k| (merged[k + 1].0 - merged[k].1, k)).collect();
    gaps.sort();
    let mut join = vec![false; merged.len()];
    for (g, k) in gaps {
        if g > threshold || g > allowance {
            break;
        }
        allowance -= g;
        join[k] = true;
    }
    // join, cutting at the gaps so that a large read keeps every connection busy
    // (pieces of about total / concurrency, from 1 MiB to 32 MiB; a range asked is never cut)
    let join_all = |limit: u64| -> Vec<(u64, u64)> {
        let mut out: Vec<(u64, u64)> = Vec::new();
        for (k, &(o, e)) in merged.iter().enumerate() {
            match out.last_mut() {
                Some(l) if k > 0 && join[k - 1] && e - l.0 <= limit => l.1 = e,
                _ => out.push((o, e)),
            }
        }
        out
    };
    let total: u64 = join_all(u64::MAX).iter().map(|s| s.1 - s.0).sum();
    join_all(total.div_ceil(concurrency.max(1) as u64).clamp(MIN_PIECE, MAX_PIECE))
}
