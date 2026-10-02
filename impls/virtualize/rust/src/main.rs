//! `virtualize <url> <out.json>`: VIRTUALIZE.md (profiles version 0, revision 2).

mod common;
mod nd2;
mod tiff;
mod xml;

use common::{E, Output, R, Source};

fn run(url: &str) -> R<Output> {
    let src = Source::open(url)?;
    let head = src.read(0, src.size.min(12))?;
    let mut out = Output::new();
    let tiff_magics: [[u8; 4]; 4] = [
        [0x49, 0x49, 0x2A, 0x00],
        [0x4D, 0x4D, 0x00, 0x2A],
        [0x49, 0x49, 0x2B, 0x00],
        [0x4D, 0x4D, 0x00, 0x2B],
    ];
    if head.len() >= 4 && tiff_magics.iter().any(|m| head[..4] == *m) {
        tiff::virtualize(&src, &mut out)?;
    } else if head.len() >= 4 && head[..4] == [0xDA, 0xCE, 0xBE, 0x0A] {
        nd2::virtualize(&src, &mut out)?;
    } else {
        return Err(E::Reject("not a TIFF or ND2 (version 3+) file".into()));
    }
    Ok(out)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: virtualize <url> <out.json>");
        std::process::exit(2);
    }
    let (url, path) = (args[1].clone(), args[2].clone());
    // Deeply nested LV structures recurse; give the worker a large stack.
    let worker = std::thread::Builder::new()
        .stack_size(512 * 1024 * 1024)
        .spawn(move || run(&url).map(|o| o.to_json(&url)))
        .expect("spawn");
    match worker.join() {
        Ok(Ok(json)) => {
            if let Err(e) = std::fs::write(&path, json) {
                eprintln!("cannot write {path}: {e}");
                std::process::exit(2);
            }
        }
        Ok(Err(E::Reject(msg))) => {
            eprintln!("rejected: {msg}");
            std::process::exit(3);
        }
        Ok(Err(E::Fail(msg))) => {
            eprintln!("failed: {msg}");
            std::process::exit(2);
        }
        Err(_) => {
            eprintln!("failed: panic");
            std::process::exit(2);
        }
    }
}
