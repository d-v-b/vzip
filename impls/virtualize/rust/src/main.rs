mod common;
mod http;
mod json;
mod lv;
mod nd2;
mod tiff;
mod xml;

use common::{Error, Output, Res};

fn run(url: &str, out_path: &str) -> Res<Output> {
    let mut src = http::Source::open(url)?;
    let size = src.size;
    if size < 4 {
        rej!("file shorter than 4 bytes");
    }
    let magic = src.read(0, 4)?;
    let mut out = Output::new(size);
    match &magic[..] {
        b"II\x2a\x00" | b"MM\x00\x2a" | b"II\x2b\x00" | b"MM\x00\x2b" => tiff::virtualize(&mut src, &mut out)?,
        b"\xda\xce\xbe\x0a" => nd2::virtualize(&mut src, &mut out)?,
        _ => rej!("unrecognized file signature {:02x?}", magic),
    }
    let _ = out_path;
    Ok(out)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: virtualize <url> <out.json>");
        std::process::exit(2);
    }
    let url = args[1].clone();
    let out_path = args[2].clone();
    // Deeply nested LV levels recurse; run on a thread with a large stack.
    let h = std::thread::Builder::new()
        .stack_size(1 << 30)
        .spawn(move || run(&url, &out_path).map(|o| o.to_json(&url)))
        .expect("spawn");
    match h.join() {
        Ok(Ok(json)) => {
            if let Err(e) = std::fs::write(&args[2], json) {
                eprintln!("error: cannot write {}: {e}", args[2]);
                std::process::exit(1);
            }
            println!("ok: {}", args[1]);
        }
        Ok(Err(Error::Reject(m))) => {
            eprintln!("rejected: {m}");
            std::process::exit(3);
        }
        Ok(Err(Error::Fail(m))) => {
            eprintln!("error: {m}");
            std::process::exit(1);
        }
        Err(_) => {
            eprintln!("error: internal panic");
            std::process::exit(1);
        }
    }
}
