// `virtualize <url> <out.json>`: VIRTUALIZE.md (revision 4) TIFF and ND2 profiles.

mod common;
mod http;
mod lv;
mod nd2;
mod tiff;
mod xml;

use common::{E, R};

fn run(url: &str) -> R<common::Output> {
    let mut r = http::Reader::open(url)?;
    let head = r.read(0, 4.min(r.size))?;
    match head.as_slice() {
        [0x49, 0x49, 0x2A, 0x00] | [0x4D, 0x4D, 0x00, 0x2A] | [0x49, 0x49, 0x2B, 0x00] | [0x4D, 0x4D, 0x00, 0x2B] => {
            tiff::virtualize(&mut r)
        }
        [0xDA, 0xCE, 0xBE, 0x0A] => nd2::virtualize(&mut r),
        _ => Err(E::Reject("unrecognized file signature".into())),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: virtualize <url> <out.json>");
        std::process::exit(2);
    }
    let url = &args[1];
    match run(url) {
        Ok(out) => {
            let s = common::serialize(url, &out);
            if let Err(e) = std::fs::write(&args[2], s) {
                eprintln!("cannot write {}: {}", args[2], e);
                std::process::exit(1);
            }
            println!("{} entries", out.entries.len());
        }
        Err(E::Reject(m)) => {
            eprintln!("rejected: {}", m);
            std::process::exit(3);
        }
        Err(E::Fail(m)) => {
            eprintln!("failed: {}", m);
            std::process::exit(1);
        }
    }
}
