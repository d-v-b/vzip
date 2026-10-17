//! Prints the test vectors of spec/bindings/phkv.md Appendix C
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#appendix-c-constants-and-test-vectors>.
use refkv_phkv::ptrhash;

fn main() {
    for (k, seed) in [(&b""[..], 0), (b"abc", 0), (b"zarr.json", 1)] {
        println!("xxh64({:?}, {seed}) = {:#018X}", String::from_utf8_lossy(k), ptrhash::hash(k, seed));
    }
    for x in [0u64, 1, 0x0123_4567_89AB_CDEF] {
        println!("fmix64({x:#018X}) = {:#018X}", ptrhash::fmix64(x));
    }
    let keys: Vec<&[u8]> = vec![b"zarr.json", b"a/zarr.json", b"a/c/0", b"a/c/1", b"b"];
    let built = ptrhash::build(&keys).unwrap();
    let p = built.params;
    println!("params: seed={} P={} S={} B={} pilots={:?}", p.seed, p.parts, p.slots, p.buckets, built.pilots);
    for k in &keys {
        let h = ptrhash::hash(k, p.seed);
        let (part, b) = p.bucket(h);
        println!("{:?}: h={h:#018X} part={part} bucket={b} pilot={} slot={}", String::from_utf8_lossy(k), built.pilots[b as usize], ptrhash::slot(&p, &built.pilots, h));
    }
}
