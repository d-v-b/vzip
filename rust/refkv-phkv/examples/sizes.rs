//! Bytes per key of the three containers for the same references: `cargo run --release --example sizes -- 100000`.
use refkv_vzip::pb::Range;

fn main() {
    let n: u64 = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(100_000);
    let key = |i: u64| format!("arr/c/{}/{}/{}", i / 10000, (i / 100) % 100, i % 100);
    let mut z = refkv_vzip::Writer::new(Vec::new(), refkv_vzip::Options { mirror_refs: true, page_size: Some(65536) });
    let mut p = refkv_phkv::Writer::new(Vec::new(), refkv_phkv::Options::default());
    let mut h = refkv_phkv::HybridWriter::new(Vec::new(), refkv_vzip::Options { mirror_refs: true, page_size: Some(65536) }, refkv_phkv::Options::default()).unwrap();
    let (s1, s2, s3) = (z.url("data.nc").unwrap(), p.url("data.nc").unwrap(), h.url("data.nc").unwrap());
    for i in 0..n {
        z.add_ranges(&key(i), vec![Range::of(s1, i * 4096, 4096)]).unwrap();
        p.add_ranges(&key(i), &[Range::of(s2, i * 4096, 4096)]).unwrap();
        h.add_ranges(&key(i), vec![Range::of(s3, i * 4096, 4096)]).unwrap();
    }
    let (z, p, h) = (z.close().unwrap().len(), p.close().unwrap().len(), h.close().unwrap().len());
    println!("n={n} key≈{}B vzip(paged)={:.1} B/key phkv={:.1} B/key hybrid={:.1} B/key", key(n / 2).len(), z as f64 / n as f64, p as f64 / n as f64, h as f64 / n as f64);
}
