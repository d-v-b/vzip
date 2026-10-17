//! Times the writer's perfect hash on synthetic Zarr-like keys: `cargo run --release --example tune -- 1000000`.
use refkv_phkv::ptrhash;
use std::time::Instant;

fn main() {
    let n: usize = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(1_000_000);
    let keys: Vec<Vec<u8>> = (0..n).map(|i| format!("arr/c/{}/{}/{}", i / 10000, (i / 100) % 100, i % 100).into_bytes()).collect();
    let refs: Vec<&[u8]> = keys.iter().map(|k| k.as_slice()).collect();
    let t = Instant::now();
    let b = ptrhash::build(&refs).expect("built");
    let p = b.params;
    println!(
        "n={n} seed={} parts={} slots/part={} buckets/part={} build={:.2?} pilot bits/key={:.2} slot overhead={:.2}%",
        p.seed, p.parts, p.slots, p.buckets, t.elapsed(),
        8.0 * b.pilots.len() as f64 / n as f64,
        100.0 * (p.total_slots() as f64 / n as f64 - 1.0)
    );
}
