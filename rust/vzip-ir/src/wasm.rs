//! A C-ABI surface for wasm32 hosts (a browser, Node): each parser's
//! step/feed/finish over linear memory. The host reads the ranges each step asks
//! for, writes their bytes into buffers it allocates here, and feeds them.
//!
//! `vz_new(format, size)` makes a parser (0 ND2, 1 TIFF, 2 CZI); `vz_step`,
//! `vz_feed` and `vz_finish` drive it. The `nd2_*` names are round 2's, kept.

use crate::czi::Czi;
use crate::nd2::{Nd2, Step};
use crate::tiff::Tiff;
use crate::Parser;
use std::cell::RefCell;

thread_local! {
    static OUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

fn set_out(b: Vec<u8>) -> usize {
    OUT.with(|o| *o.borrow_mut() = b);
    OUT.with(|o| o.borrow().len())
}

#[unsafe(no_mangle)]
pub extern "C" fn vz_alloc(n: usize) -> *mut u8 {
    let mut v = vec![0u8; n];
    let p = v.as_mut_ptr();
    std::mem::forget(v);
    p
}

/// # Safety
/// `p` and `n` must come from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_free(p: *mut u8, n: usize) {
    drop(unsafe { Vec::from_raw_parts(p, n, n) });
}

/// The bytes of the last result (ranges as little-endian u64 pairs, a message, or the facts).
#[unsafe(no_mangle)]
pub extern "C" fn vz_out() -> *const u8 {
    OUT.with(|o| o.borrow().as_ptr())
}

pub enum Any {
    Nd2(Nd2),
    Tiff(Tiff),
    Czi(Czi),
}

fn step_out(r: Result<Step, String>) -> i64 {
    match r {
        Ok(Step::Read(r)) => 1 + set_out(r.iter().flat_map(|(o, n)| [o.to_le_bytes(), n.to_le_bytes()]).flatten().collect()) as i64,
        Ok(Step::Done) => 0,
        Err(e) => -(set_out(e.into_bytes()) as i64),
    }
}

fn finish_out(r: Result<(crate::ir::Ir, serde_json::Value), String>) -> i64 {
    match r {
        Ok((ir, facts)) => {
            let out = serde_json::json!({
                "elements": ir.len(),
                "runs": ir.runs.len(),
                "check": crate::check::check(&ir).is_ok(),
                "digest": format!("{:016x}", ir.digest()),
                "facts": facts,
            });
            set_out(out.to_string().into_bytes()) as i64
        }
        Err(e) => -(set_out(e.into_bytes()) as i64),
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn vz_new(format: u32, size: u64) -> *mut Any {
    Box::into_raw(Box::new(match format {
        0 => Any::Nd2(Nd2::new(size)),
        1 => Any::Tiff(Tiff::new(size)),
        _ => Any::Czi(Czi::new(size)),
    }))
}

/// The next batch: 1 + its byte length at `vz_out()` (16 per range; a batch may
/// be empty), 0 when done, or minus the length of the rejection's message.
///
/// # Safety
/// `p` must come from `vz_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_step(p: *mut Any) -> i64 {
    step_out(match unsafe { &mut *p } {
        Any::Nd2(x) => x.step(),
        Any::Tiff(x) => x.step(),
        Any::Czi(x) => x.step(),
    })
}

/// # Safety
/// `p` from `vz_new`; `data` and `n` from `vz_alloc(n)` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_feed(p: *mut Any, offset: u64, data: *mut u8, n: usize) {
    let v = unsafe { Vec::from_raw_parts(data, n, n) };
    match unsafe { &mut *p } {
        Any::Nd2(x) => x.feed(offset, v),
        Any::Tiff(x) => x.feed(offset, v),
        Any::Czi(x) => x.feed(offset, v),
    }
}

/// Finishes: JSON of the element and run counts, the checker's verdict, the IR's
/// digest and the facts at `vz_out()`, its length, or minus the length of a
/// rejection's message.
///
/// # Safety
/// `p` from `vz_new` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_finish(p: *mut Any) -> i64 {
    finish_out(match *unsafe { Box::from_raw(p) } {
        Any::Nd2(x) => Parser::finish(x),
        Any::Tiff(x) => Parser::finish(x),
        Any::Czi(x) => Parser::finish(x),
    })
}

#[unsafe(no_mangle)]
pub extern "C" fn nd2_new(size: u64) -> *mut Nd2 {
    Box::into_raw(Box::new(Nd2::new(size)))
}

/// # Safety
/// `p` must come from `nd2_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn nd2_step(p: *mut Nd2) -> i64 {
    step_out(unsafe { &mut *p }.step())
}

/// # Safety
/// `p` from `nd2_new`; `data` and `n` from `vz_alloc(n)` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn nd2_feed(p: *mut Nd2, offset: u64, data: *mut u8, n: usize) {
    unsafe { &mut *p }.feed(offset, unsafe { Vec::from_raw_parts(data, n, n) });
}

/// Finishes: the facts' JSON (with the element count) at `vz_out()`, its length,
/// or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `nd2_new` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn nd2_finish(p: *mut Nd2) -> i64 {
    let p = unsafe { Box::from_raw(p) };
    match p.finish() {
        Ok((ir, mut facts)) => {
            facts["elements"] = serde_json::json!(ir.len());
            facts["runs"] = serde_json::json!(ir.runs.len());
            facts["check"] = serde_json::json!(crate::check::check(&ir).is_ok());
            set_out(facts.to_string().into_bytes()) as i64
        }
        Err(e) => -(set_out(e.into_bytes()) as i64),
    }
}

// ---- runs: a parser, the read planner, the projection and the mirror

use crate::run::{Format, Run};

fn err_out(e: String) -> i64 {
    -(set_out(e.into_bytes()) as i64)
}

/// A run of `format` (0 ND2, 1 TIFF, 2 CZI) over a source of `size` bytes, remote
/// or not, read by the host at most `concurrency` requests at a time.
#[unsafe(no_mangle)]
pub extern "C" fn vz_run_new(format: u32, size: u64, remote: u32, concurrency: u32) -> *mut Run {
    let f = Format::from_code(format).unwrap_or(Format::Czi);
    Box::into_raw(Box::new(Run::new(f, size, remote != 0, concurrency as usize)))
}

/// Bytes the host holds already (from `offset`; `data`/`n` from `vz_alloc`, consumed).
///
/// # Safety
/// `p` from `vz_run_new`; `data`, `n` from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_seed(p: *mut Run, offset: u64, data: *mut u8, n: usize) {
    let v = unsafe { Vec::from_raw_parts(data, n, n) };
    unsafe { &mut *p }.planner.seed(offset, v);
}

/// A request the host made itself (`n` bytes in `seconds`), for the cost model.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_observe(p: *mut Run, n: u64, seconds: f64) {
    unsafe { &mut *p }.planner.observe(n, seconds);
}

/// The planner's settings: the cost of a byte fetched (seconds; negative: the
/// default) and the size at or below which a remote source is read whole.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_settings(p: *mut Run, byte_cost: f64, whole_below: u64) {
    let r = unsafe { &mut *p };
    if byte_cost >= 0.0 {
        r.planner.byte_cost = byte_cost;
    }
    r.planner.whole_below = whole_below;
}

/// Whether the server answers multi-range requests: −1 unknown (probe), 0 no, 1 yes.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_multirange(p: *mut Run, v: i32) {
    unsafe { &mut *p }.planner.multirange = match v {
        0 => Some(false),
        1 => Some(true),
        _ => None,
    };
}

/// The requests to make now: 1 + the byte length at `vz_out()` of u32 count, then per
/// request a u32 span count and its spans as u64 (start, end) pairs (one span: a
/// plain range request; several: a multi-range one); 0 when the run is done; or
/// minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_poll(p: *mut Run) -> i64 {
    match unsafe { &mut *p }.poll() {
        Ok(None) => 0,
        Ok(Some(reqs)) => {
            let mut b = Vec::new();
            b.extend_from_slice(&(reqs.len() as u32).to_le_bytes());
            for q in reqs {
                b.extend_from_slice(&(q.spans.len() as u32).to_le_bytes());
                for (a, e) in q.spans {
                    b.extend_from_slice(&a.to_le_bytes());
                    b.extend_from_slice(&e.to_le_bytes());
                }
            }
            1 + set_out(b) as i64
        }
        Err(e) => err_out(e),
    }
}

/// Request `k` of the last poll completed in `seconds`: with `ok`, `data`/`n` (from
/// `vz_alloc`, consumed) are its spans' bytes one after another; without, a
/// multi-range request the server did not answer with its ranges (`data` null).
/// 0, or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_run_new`; `data`, `n` from `vz_alloc(n)` or null and 0.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_complete(p: *mut Run, k: u32, ok: u32, data: *mut u8, n: usize, seconds: f64) -> i64 {
    let r = unsafe { &mut *p };
    let parts = if ok != 0 {
        let v = if data.is_null() { Vec::new() } else { unsafe { Vec::from_raw_parts(data, n, n) } };
        let reqs = r.planner.round_spans(k as usize);
        let mut parts = Vec::with_capacity(reqs.len());
        let mut at = 0usize;
        for (a, e) in reqs {
            let len = (e - a) as usize;
            if at + len > v.len() {
                return err_out("a response of the wrong length".into());
            }
            parts.push(v[at..at + len].to_vec());
            at += len;
        }
        if at != v.len() {
            return err_out("a response of the wrong length".into());
        }
        Some(parts)
    } else {
        None
    };
    match r.complete(k as usize, parts, seconds) {
        Ok(()) => 0,
        Err(e) => err_out(e),
    }
}

/// The run's planner counts and model (JSON at `vz_out()`): its length.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_stats(p: *mut Run) -> i64 {
    set_out(unsafe { &*p }.planner.stats_json().to_string().into_bytes()) as i64
}

/// Finishes the run (consumed), projects it and, with `mirror`, mirrors it: the
/// output (`Out::encode`) at `vz_out()`, its length, or minus the length of a
/// rejection's message. `url` (UTF-8, from `vz_alloc`, consumed) is the source's.
///
/// # Safety
/// `p` from `vz_run_new`; `url`, `n` from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_output(p: *mut Run, url: *mut u8, n: usize, mirror: u32) -> i64 {
    let r = *unsafe { Box::from_raw(p) };
    let url = String::from_utf8(unsafe { Vec::from_raw_parts(url, n, n) }).unwrap_or_default();
    match r.into_output(&url, mirror != 0) {
        Ok(o) => set_out(o.encode()) as i64,
        Err(e) => err_out(e),
    }
}

/// Drops a run without finishing it.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_free(p: *mut Run) {
    drop(unsafe { Box::from_raw(p) });
}

// ---- test hooks (feature `test-hooks`, built by `wasm/build.sh test` into
// target/web-test; never in the shipped module)

/// The mirror of a crafted IR, for the browser's tests of the mirror's rejections:
/// `json` (from `vz_alloc`, consumed) is `{"size", "kind", "parent", "name", "nidx",
/// "start", "len", "runs": [[root, count, stride]]}` (a parent of -1: none; a name
/// index of -1: none), `profile` 0 ND2, 1 TIFF, 2 CZI. The output's length, or minus
/// the rejection's.
///
/// # Safety
/// `json`/`n` from `vz_alloc`.
#[cfg(feature = "test-hooks")]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_test_mirror(json: *mut u8, n: usize, profile: u32) -> i64 {
    let b = unsafe { Vec::from_raw_parts(json, n, n) };
    let built = (|| -> Result<crate::out::Out, String> {
        let v: serde_json::Value = serde_json::from_slice(&b).map_err(|e| e.to_string())?;
        let nums = |k: &str| -> Vec<i64> { v[k].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default() };
        let mut ir = crate::ir::Ir { size: v["size"].as_u64().unwrap_or(0), budget: None, ..Default::default() };
        ir.types.id("");
        let names = v["name"].as_array().cloned().unwrap_or_default();
        let (kind, parent, nidx, start, len) = (nums("kind"), nums("parent"), nums("nidx"), nums("start"), nums("len"));
        for k in 0..kind.len() {
            ir.kind.push(kind[k] as u8);
            ir.parent.push(if parent[k] < 0 { crate::ir::NONE } else { parent[k] as u32 });
            let name = names.get(k).and_then(|x| x.as_str()).unwrap_or("");
            let id = ir.names.id(name);
            ir.name.push(id);
            ir.nidx.push(nidx[k] as u64);
            ir.ty.push(0);
            ir.space.push(0);
            ir.start.push(start[k] as u64);
            ir.len.push(len[k] as u64);
        }
        for r in v["runs"].as_array().cloned().unwrap_or_default() {
            let r: Vec<u64> = r.as_array().map(|a| a.iter().filter_map(|x| x.as_u64()).collect()).unwrap_or_default();
            ir.runs.push((r[0] as u32, r[1], r[2]));
        }
        ir.finished = true;
        ir.build_children();
        let format = Format::from_code(profile).ok_or("no such profile")?;
        let mut out = crate::out::Out::new("");
        crate::mirror::mirror(&ir, &mut out, format.name())?;
        Ok(out)
    })();
    match built {
        Ok(o) => set_out(o.encode()) as i64,
        Err(e) => err_out(e),
    }
}
