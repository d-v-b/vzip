//! `vzip convert`: a legacy image file or store virtualized into a vzip archive, the archive
//! `python -m refkv.virtualize` writes, byte for byte (python/src/refkv/virtualize/__main__.py).
//!
//! ```text
//! vzip convert <url or path> <out.vzip> [--url <source url>] [--checksums] [--allow-private-hosts]
//!     [--connections N] [--byte-cost SECONDS_PER_MB] [--workers N] [--format vzip|phkv|hybrid]
//! ```
//!
//! `--format` chooses the container: a vzip archive (the default), a standalone phkv store, or a
//! vzip archive that also carries a phkv container (spec/bindings/phkv.md §16.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#162-profiles>). Only vzip
//! archives match the Python virtualizer's output byte for byte.
//!
//! TIFF (NDPI included), ND2, CZI, DICOM, NIfTI, IMS and SAFE zip files, and Zarr v2, OME-Zarr, N5
//! and SAFE stores (an http(s) URL ending in `/`, listed by S3 `ListObjectsV2`, or a local
//! directory, which needs `--url`, the store URL it is served from), go through the Rust core
//! (rust/refkv-ir). An input at an http(s) URL is read under the reader policy (spec/refkv.md
//! §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>): a host that is,
//! or resolves to, a loopback, private, link-local or special address is refused unless
//! `--allow-private-hosts` (UNSAFE) is given. The archive's sources are checked against the same
//! policy, and an archive a reader under it could not read is not written.
//!
//! Every url source pins its size, and the input file's source its ETag when every response gave
//! the same strong one. `--checksums` records the CRC-32C of every range of a url source
//! (spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), which
//! reads every byte the archive references.
//!
//! Exits with status 3 if the input is rejected, 2 on a usage error, and 1 if the reader policy
//! refuses the input or a source the archive would name, or on any other failure. On success, the
//! summary (`{"format": ..., ...}`) is printed as Python's `json.dumps` prints it.

mod http;
mod output;
mod policy;
mod pyjson;
mod run;
mod store;
mod transport;
mod xml;

use std::process::ExitCode;
use std::time::Duration;

/// How a conversion fails, and so its exit status.
#[derive(Debug)]
pub enum Fail {
    /// the input is not one the virtualizers accept (status 3)
    Rejected(String),
    /// the reader policy refuses the input, or a source the archive would name (status 1)
    Refused(String),
    /// the command line (status 2)
    Usage(String),
    /// anything else, such as a network error or a missing file (status 1)
    Other(String),
}

const USAGE: &str = "usage: vzip convert <url or path> <out.vzip> [--url <source url>] [--checksums] \
[--allow-private-hosts] [--connections N] [--byte-cost SECONDS_PER_MB] [--workers N] [--format vzip|phkv|hybrid]\n\
(a URL ending in / or a directory is an N5, Zarr v2 or SAFE store; a directory needs --url;\n\
a store's objects are read by up to N requests at a time, default 16)";

/// The options of `convert`.
#[derive(Debug, Default, PartialEq)]
struct Args {
    input: String,
    out: String,
    url: Option<String>,
    checksums: bool,
    allow_private_hosts: bool,
    connections: Option<usize>,
    byte_cost: Option<f64>,
    workers: usize,
    format: output::Container,
}

/// Removes the first `flag` and its value from `args`. A flag without a value, like a value that
/// is not a number, fails with status 1, as Python's command does; status 2 is for the number of
/// arguments.
fn take(args: &mut Vec<String>, flag: &str) -> Result<Option<String>, Fail> {
    let Some(i) = args.iter().position(|a| a == flag) else { return Ok(None) };
    if i + 1 >= args.len() {
        return Err(Fail::Other(format!("{flag} needs a value")));
    }
    let v = args.remove(i + 1);
    args.remove(i);
    Ok(Some(v))
}

fn flag(args: &mut Vec<String>, flag: &str) -> bool {
    let i = args.iter().position(|a| a == flag);
    if let Some(i) = i {
        args.remove(i);
    }
    i.is_some()
}

fn number<T: std::str::FromStr>(flag: &str, v: &str) -> Result<T, Fail> {
    v.trim().parse().map_err(|_| Fail::Other(format!("{flag} {v:?} is not a number")))
}

/// `convert`'s arguments, the flags anywhere, as python -m refkv.virtualize reads them.
fn parse(argv: &[String]) -> Result<Args, Fail> {
    let mut args = argv.to_vec();
    let checksums = flag(&mut args, "--checksums");
    let allow_private_hosts = flag(&mut args, "--allow-private-hosts");
    // requests at a time to a remote file (default 8); 0 is the default
    let connections = match take(&mut args, "--connections")? {
        None => None,
        Some(v) => match number::<i64>("--connections", &v)? {
            0 => None,
            n if n < 0 => return Err(Fail::Other(format!("--connections {n} is not a number of connections"))),
            n => Some(n as usize),
        },
    };
    // seconds per MB the read planner charges for bytes fetched (default 0.1)
    let byte_cost = take(&mut args, "--byte-cost")?.map(|v| number::<f64>("--byte-cost", &v)).transpose()?.map(|c| c * 1e-6);
    let url = take(&mut args, "--url")?;
    let workers = take(&mut args, "--workers")?.map(|v| number::<i64>("--workers", &v)).transpose()?.unwrap_or(store::WORKERS as i64);
    let format = match take(&mut args, "--format")? {
        None => output::Container::default(),
        Some(f) => output::Container::parse(&f).ok_or_else(|| Fail::Usage(format!("--format {f:?} is not vzip, phkv or hybrid")))?,
    };
    let [input, out] = <[String; 2]>::try_from(args).map_err(|_| Fail::Usage(USAGE.into()))?;
    Ok(Args { input, out, url, checksums, allow_private_hosts, connections, byte_cost, workers: workers.max(1) as usize, format })
}

/// Virtualizes the input, and writes the archive; the summary.
fn convert(a: &Args) -> Result<serde_json::Value, Fail> {
    if a.url.is_none() && std::path::Path::new(&a.input).is_dir() {
        return Err(Fail::Usage("a local directory needs --url, the store URL (ending in /) it is served from".into()));
    }
    let policy = policy::Policy { allow_private_hosts: a.allow_private_hosts };
    let client = http::Client::new(policy, Duration::from_secs(60));
    let (fmt, summary, archive) = if store::is_store(&a.input) {
        let s = store::Store::open(&a.input, a.url.as_deref(), &client, a.workers)?;
        let (fmt, out) = store::virtualize_store(&s)?;
        let summary = out.summary.clone();
        let (mut archive, objects) = output::store_archive(out, &s.listing().objects)?;
        if a.checksums {
            output::checksums(&mut archive, s.listing().workers, |i, o, n| s.read_range(&objects[i], o, n))?;
        }
        (fmt, summary, archive)
    } else {
        let t = transport::Transport::open(&a.input, &client)?;
        let (fmt, out) = run::virtualize_file(&a.input, a.url.as_deref(), &t, a.connections, a.byte_cost)?;
        let summary = out.summary.clone();
        // the ETag is pinned only when the archive names the URL that was read
        let etag = if a.url.is_none() || a.url.as_deref() == Some(a.input.as_str()) { t.etag() } else { None };
        let mut archive = output::file_archive(out, t.size(), etag)?;
        if a.checksums {
            let conc = run::concurrency(&t, a.connections)?;
            output::checksums(&mut archive, conc, |_, o, n| t.get(o, o + n))?;
        }
        (fmt.name().to_string(), summary, archive)
    };
    output::save(&archive, &a.out, &policy, a.format)?;
    // {"format": fmt, **summary}: the summary's own format, if it has one, in the first place
    let mut info = serde_json::Map::new();
    info.insert("format".into(), fmt.into());
    if let serde_json::Value::Object(m) = summary {
        for (k, v) in m {
            info.insert(k, v);
        }
    }
    Ok(serde_json::Value::Object(info))
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let r = match argv.first().map(String::as_str) {
        Some("convert") => parse(&argv[1..]).and_then(|a| convert(&a)),
        _ => Err(Fail::Usage(USAGE.into())),
    };
    match r {
        Ok(info) => {
            println!("{}", pyjson::dumps(&info));
            ExitCode::SUCCESS
        }
        Err(Fail::Rejected(m)) => {
            eprintln!("rejected: {m}");
            ExitCode::from(3)
        }
        Err(Fail::Usage(m)) => {
            eprintln!("{m}");
            ExitCode::from(2)
        }
        Err(Fail::Refused(m) | Fail::Other(m)) => {
            eprintln!("error: {m}");
            ExitCode::from(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(s: &str) -> Vec<String> {
        s.split_whitespace().map(String::from).collect()
    }

    #[test]
    fn command_lines() {
        assert_eq!(
            parse(&args("in.tif out.vzip")).unwrap(),
            Args { input: "in.tif".into(), out: "out.vzip".into(), workers: 16, ..Default::default() }
        );
        assert_eq!(
            parse(&args("--checksums d/ --url http://h/s/ o.vzip --allow-private-hosts --connections 3 --byte-cost 2 --workers 0")).unwrap(),
            Args {
                input: "d/".into(),
                out: "o.vzip".into(),
                url: Some("http://h/s/".into()),
                checksums: true,
                allow_private_hosts: true,
                connections: Some(3),
                byte_cost: Some(2e-6),
                workers: 1,
                format: output::Container::Vzip,
            }
        );
        assert_eq!(parse(&args("a b --connections 0")).unwrap().connections, None);
        assert_eq!(parse(&args("a b --format hybrid")).unwrap().format, output::Container::Hybrid);
        assert!(matches!(parse(&args("a b --format tar")), Err(Fail::Usage(_))));
    }

    #[test]
    fn refuses_a_missing_output() {
        assert!(matches!(parse(&args("in.tif")), Err(Fail::Usage(_))));
    }

    #[test]
    fn refuses_an_extra_argument() {
        assert!(matches!(parse(&args("a b c")), Err(Fail::Usage(_))));
    }

    #[test]
    fn refuses_a_flag_without_its_value() {
        assert!(matches!(parse(&args("a b --url")), Err(Fail::Other(_))));
    }

    #[test]
    fn refuses_a_value_that_is_not_a_number() {
        assert!(matches!(parse(&args("a b --workers many")), Err(Fail::Other(_))));
    }

    #[test]
    fn refuses_negative_connections() {
        assert!(matches!(parse(&args("a b --connections -1")), Err(Fail::Other(_))));
    }

    #[test]
    fn refuses_a_directory_without_url() {
        let a = Args { input: env!("CARGO_MANIFEST_DIR").into(), out: "never.vzip".into(), workers: 1, ..Default::default() };
        assert!(matches!(convert(&a), Err(Fail::Usage(_))));
    }
}
