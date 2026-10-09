//! A run: a format's parser and the read planner together, sans-IO. The host
//! loops `poll()` (the requests to make now, concurrently), `complete()` (each
//! one's bytes and time), until `poll()` gives None; then `finish()`.

use crate::czi::Czi;
use crate::ir::Ir;
use crate::nd2::{Nd2, Step};
use crate::plan::{Planner, Request};
use crate::tiff::Tiff;
use crate::Parser;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Format {
    Nd2,
    Tiff,
    Czi,
}

impl Format {
    pub fn from_code(c: u32) -> Option<Format> {
        match c {
            0 => Some(Format::Nd2),
            1 => Some(Format::Tiff),
            2 => Some(Format::Czi),
            _ => None,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Format::Nd2 => "nd2",
            Format::Tiff => "tiff",
            Format::Czi => "czi",
        }
    }
    /// The format a file's first bytes give, if one of these.
    pub fn sniff(head: &[u8]) -> Option<Format> {
        if head.len() >= 4 && head[..4] == [0xda, 0xce, 0xbe, 0x0a] {
            return Some(Format::Nd2);
        }
        if head.len() >= 4 && [b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"].iter().any(|m| head[..4] == m[..]) {
            return Some(Format::Tiff);
        }
        if head.len() >= 16 && &head[..16] == b"ZISRAWFILE\0\0\0\0\0\0" {
            return Some(Format::Czi);
        }
        None
    }
}

pub enum AnyParser {
    Nd2(Nd2),
    Tiff(Tiff),
    Czi(Czi),
}

impl AnyParser {
    pub fn new(f: Format, size: u64) -> Self {
        match f {
            Format::Nd2 => AnyParser::Nd2(Nd2::new(size)),
            Format::Tiff => AnyParser::Tiff(Tiff::new(size)),
            Format::Czi => AnyParser::Czi(Czi::new(size)),
        }
    }
    pub fn step(&mut self) -> Result<Step, String> {
        match self {
            AnyParser::Nd2(x) => x.step(),
            AnyParser::Tiff(x) => x.step(),
            AnyParser::Czi(x) => x.step(),
        }
    }
    pub fn feed(&mut self, o: u64, b: Vec<u8>) {
        match self {
            AnyParser::Nd2(x) => x.feed(o, b),
            AnyParser::Tiff(x) => x.feed(o, b),
            AnyParser::Czi(x) => x.feed(o, b),
        }
    }
    pub fn finish(self) -> Result<(Ir, serde_json::Value), String> {
        match self {
            AnyParser::Nd2(x) => Parser::finish(x),
            AnyParser::Tiff(x) => Parser::finish(x),
            AnyParser::Czi(x) => Parser::finish(x),
        }
    }
}

pub struct Run {
    pub format: Format,
    parser: Option<AnyParser>,
    /// the parser's IR and facts, once it is done; then the values the view shows
    /// that it did not read are read (`fill`) and held
    parsed: Option<(Ir, serde_json::Value)>,
    fill: Vec<(u32, u64, u64)>,
    /// the fill batch's ranges, and the planner's (requests, bytes) before and after it
    fill_counts: (u64, (u64, u64), (u64, u64)),
    pub planner: Planner,
    /// TIFF and CZI take whole responses (they find each range in them); ND2 exact ranges.
    whole: bool,
    in_batch: bool,
    done: bool,
}

impl Run {
    pub fn new(format: Format, size: u64, remote: bool, concurrency: usize) -> Self {
        Run {
            format,
            parser: Some(AnyParser::new(format, size)),
            parsed: None,
            fill: Vec::new(),
            fill_counts: (0, (0, 0), (0, 0)),
            planner: Planner::new(size, remote, concurrency),
            whole: format != Format::Nd2,
            in_batch: false,
            done: false,
        }
    }

    /// The requests to make now, concurrently; None when the parser is done.
    pub fn poll(&mut self) -> Result<Option<Vec<Request>>, String> {
        loop {
            if self.done {
                return Ok(None);
            }
            if self.in_batch {
                let r = self.planner.requests();
                if !r.is_empty() {
                    return Ok(Some(r));
                }
                let got = self.planner.take()?;
                self.in_batch = false;
                if let Some((ir, _)) = self.parsed.as_mut() {
                    // the values the view shows, read after the parser
                    let at: std::collections::HashMap<(u64, u64), &Vec<u8>> =
                        got.iter().map(|(o, b)| ((*o, b.len() as u64), b)).collect();
                    for &(i, o, n) in &self.fill {
                        let b = at.get(&(o, n)).ok_or("internal: a value the view shows was not read")?;
                        ir.put_value(i, b);
                    }
                    ir.sort_values();
                    self.fill_counts.2 = (self.planner.stats.requests, self.planner.stats.bytes);
                    self.done = true;
                    return Ok(None);
                }
                let p = self.parser.as_mut().ok_or("internal: no parser")?;
                for (o, b) in got {
                    p.feed(o, b);
                }
            }
            let p = self.parser.as_mut().ok_or("internal: no parser")?;
            match p.step()? {
                Step::Read(r) => {
                    self.planner.begin(r, self.whole);
                    self.in_batch = true;
                }
                Step::Done => {
                    let (ir, facts) = self.parser.take().unwrap().finish()?;
                    self.fill = unread_shown(&ir, self.format.name());
                    let ranges: Vec<(u64, u64)> = {
                        let mut r: Vec<(u64, u64)> = self.fill.iter().map(|&(_, o, n)| (o, n)).collect();
                        r.sort_unstable();
                        r.dedup();
                        r
                    };
                    self.parsed = Some((ir, facts));
                    if ranges.is_empty() {
                        self.done = true;
                        return Ok(None);
                    }
                    self.fill_counts = (ranges.len() as u64, (self.planner.stats.requests, self.planner.stats.bytes), (0, 0));
                    self.planner.begin(ranges, false);
                    self.in_batch = true;
                }
            }
        }
    }

    /// Request `k` of the last poll: each span's bytes (None: a multi-range request
    /// the server did not answer with its ranges), and the seconds it took.
    pub fn complete(&mut self, k: usize, data: Option<Vec<Vec<u8>>>, seconds: f64) -> Result<(), String> {
        self.planner.complete(k, data, seconds)
    }

    pub fn finish(self) -> Result<(Ir, serde_json::Value, serde_json::Value), String> {
        if !self.done {
            return Err("the run is not done".into());
        }
        let mut stats = self.planner.stats_json();
        // the batch that read what the parser did not keep of the view's values
        let (n, a, b) = self.fill_counts;
        let (requests, bytes) = if n == 0 { (0, 0) } else { (b.0 - a.0, b.1 - a.1) };
        stats["fill"] = serde_json::json!({"ranges": n, "requests": requests, "bytes": bytes});
        let (ir, facts) = self.parsed.ok_or("internal: no IR")?;
        Ok((ir, facts, stats))
    }
}

/// The values the view shows (conventions §8.7) whose bytes the parser did not keep,
/// in the source: (element, offset, length). Each parser holds every such value it
/// reads; these are read after it.
pub fn unread_shown(ir: &Ir, profile: &str) -> Vec<(u32, u64, u64)> {
    (0..ir.len() as u32)
        .filter(|&i| {
            let iu = i as usize;
            ir.space[iu] == 0 && ir.len[iu] > 0 && ir.value_bytes(i).is_none() && crate::mirror::shown(ir, profile, i)
        })
        .map(|i| (i, ir.start[i as usize], ir.len[i as usize]))
        .collect()
}

/// The output of a parse: the image projection of `format`, the mirror under
/// `vzip_source` when `mirror`, and a summary with the planner's counts, the IR's
/// element count and what the mirror's table folded.
pub fn output(format: Format, ir: &Ir, facts: &serde_json::Value, stats: serde_json::Value, url: &str, mirror: bool)
              -> Result<crate::out::Out, String> {
    let mut out = crate::project::project(format, ir, facts, url)?;
    if let Some(m) = out.summary.as_object_mut() {
        m.insert("planner".into(), stats);
        m.insert("elements".into(), ir.len().into());
    }
    if mirror {
        let folded = crate::mirror::mirror(ir, &mut out, format.name())?;
        if let Some(m) = out.summary.as_object_mut() {
            m.insert("folded".into(), folded.json());
        }
    }
    Ok(out)
}

impl Run {
    /// Finishes, projects and mirrors: the output (`output`).
    pub fn into_output(self, url: &str, mirror: bool) -> Result<crate::out::Out, String> {
        let format = self.format;
        let (ir, facts, stats) = self.finish()?;
        output(format, &ir, &facts, stats, url, mirror)
    }
}

/// Runs a format's parser over bytes in memory through the planner (tests, tools).
pub fn run_bytes(format: Format, data: &[u8]) -> Result<(Ir, serde_json::Value, serde_json::Value), String> {
    let mut r = Run::new(format, data.len() as u64, false, 4);
    while let Some(reqs) = r.poll()? {
        for (k, q) in reqs.iter().enumerate() {
            let parts = q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect();
            r.complete(k, Some(parts), 0.0)?;
        }
    }
    r.finish()
}
