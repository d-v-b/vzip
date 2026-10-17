//! The perfect hash of a phkv store (spec/bindings/phkv.md §18
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#18-perfect-hash>): PtrHash's
//! query (parts, buckets, one byte of pilot per bucket), with XXH64 as the key hash and a slot table
//! in place of PtrHash's remap. [`slot`] is the query the specification defines; [`build`] is one
//! way to find pilots for it, the writer's own (the specification only requires that every key
//! lands in a slot of its own).

use std::collections::BinaryHeap;

/// The multiplier that mixes a pilot into a key's hash, FxHash's.
pub const PILOT_MULT: u64 = 0x517c_c1b7_2722_0a95;

/// The hash function of the header's `hash_id` 1: XXH64.
pub const HASH_XXH64: u8 = 1;

/// The high 64 bits of the 128-bit product `a * m`: `a` scaled from [0, 2^64) to [0, m).
pub fn mulhi(a: u64, m: u64) -> u64 {
    ((a as u128 * m as u128) >> 64) as u64
}

/// MurmurHash3's 64-bit finalizer.
pub fn fmix64(mut x: u64) -> u64 {
    x ^= x >> 33;
    x = x.wrapping_mul(0xff51_afd7_ed55_8ccd);
    x ^= x >> 33;
    x = x.wrapping_mul(0xc4ce_b9fe_1a85_ec53);
    x ^ (x >> 33)
}

/// A key's hash: XXH64 of its UTF-8 bytes, with the header's seed.
pub fn hash(key: &[u8], seed: u64) -> u64 {
    xxhash_rust::xxh64::xxh64(key, seed)
}

/// The header's parameters: the seed, and the number of parts, and of slots and buckets in each.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Params {
    pub seed: u64,
    pub parts: u64,
    pub slots: u64,
    pub buckets: u64,
}

impl Params {
    /// The part of a hash, and its bucket's index among all buckets (the index of its pilot).
    pub fn bucket(&self, h: u64) -> (u64, u64) {
        let p = mulhi(h, self.parts);
        let b = mulhi(h.wrapping_mul(self.parts), self.buckets);
        (p, p * self.buckets + b)
    }

    /// The slot of a hash in its part, given the pilot of its bucket.
    pub fn slot_in_part(&self, h: u64, pilot: u8) -> u64 {
        mulhi(fmix64(h ^ (pilot as u64).wrapping_mul(PILOT_MULT)), self.slots)
    }

    pub fn total_slots(&self) -> u64 {
        self.parts * self.slots
    }

    pub fn total_buckets(&self) -> u64 {
        self.parts * self.buckets
    }
}

/// The slot of the key whose hash is `h`, among `params.total_slots()`.
pub fn slot(params: &Params, pilots: &[u8], h: u64) -> u64 {
    let (p, b) = params.bucket(h);
    p * params.slots + params.slot_in_part(h, pilots[b as usize])
}

/// The load factor (keys per slot of the fullest part) and the keys per bucket the writer aims for.
pub const ALPHA: f64 = 0.98;
pub const LAMBDA: f64 = 3.0;
/// The keys per part the writer aims for.
pub const PART_KEYS: u64 = 1 << 16;
/// How many seeds the writer tries before it gives up.
pub const MAX_SEEDS: u64 = 64;
/// Buckets placed most recently, which a bucket being placed must not evict (so that two buckets
/// do not evict each other forever).
const RECENT: usize = 16;

/// What [`build`] finds: the parameters, the pilots, and for each slot the index of its key in the
/// input, or `None`.
pub struct Built {
    pub params: Params,
    pub pilots: Vec<u8>,
    pub slots: Vec<Option<u32>>,
}

/// Finds a seed and pilots that place every key in a slot of its own: seeds 0, 1, 2, ... until one
/// works. None if no seed of the first [`MAX_SEEDS`] does (in practice, if two keys are equal).
pub fn build(keys: &[&[u8]]) -> Option<Built> {
    assert!(!keys.is_empty(), "a store with no keys has no perfect hash");
    assert!(keys.len() <= u32::MAX as usize, "more than 2^32 - 1 keys");
    (0..MAX_SEEDS).find_map(|seed| build_seed(keys, seed))
}

/// The parameters for `n` keys whose parts hold at most `max_part` of them.
fn params_for(seed: u64, parts: u64, max_part: u64) -> Params {
    let slots = ((max_part as f64 / ALPHA).ceil() as u64).max(1);
    let buckets = ((max_part as f64 / LAMBDA).ceil() as u64).max(1);
    Params { seed, parts, slots, buckets }
}

fn build_seed(keys: &[&[u8]], seed: u64) -> Option<Built> {
    let n = keys.len() as u64;
    let parts = n.div_ceil(PART_KEYS).max(1);
    let hashes: Vec<u64> = keys.iter().map(|k| hash(k, seed)).collect();
    let mut counts = vec![0u64; parts as usize];
    for &h in &hashes {
        counts[mulhi(h, parts) as usize] += 1;
    }
    let params = params_for(seed, parts, counts.iter().copied().max().unwrap_or(0));
    // each part's keys, as indices into `keys`, in input order
    let mut by_part: Vec<Vec<u32>> = counts.iter().map(|&c| Vec::with_capacity(c as usize)).collect();
    for (i, &h) in hashes.iter().enumerate() {
        by_part[mulhi(h, parts) as usize].push(i as u32);
    }
    let mut pilots = vec![0u8; params.total_buckets() as usize];
    let mut slots = vec![None; params.total_slots() as usize];
    for (p, members) in by_part.iter().enumerate() {
        let base_b = p as u64 * params.buckets;
        let base_s = p as u64 * params.slots;
        let placed = build_part(&params, &hashes, members, base_b)?;
        for (b, pilot) in placed.pilots.into_iter().enumerate() {
            pilots[(base_b + b as u64) as usize] = pilot;
        }
        for (s, key) in placed.slots.into_iter().enumerate() {
            slots[(base_s + s as u64) as usize] = key;
        }
    }
    Some(Built { params, pilots, slots })
}

struct Part {
    pilots: Vec<u8>,
    slots: Vec<Option<u32>>,
}

/// Places one part's keys: buckets largest first, each with the first pilot that puts its keys in
/// free slots; when no pilot does, the pilot that evicts the fewest keys (counting a bucket of size
/// s as s²), whose evicted buckets go back in the queue.
fn build_part(params: &Params, hashes: &[u64], members: &[u32], base_b: u64) -> Option<Part> {
    let nb = params.buckets as usize;
    let ns = params.slots as usize;
    let mut buckets: Vec<Vec<u64>> = vec![Vec::new(); nb];
    let mut owners: Vec<Vec<u32>> = vec![Vec::new(); nb];
    for &i in members {
        let h = hashes[i as usize];
        let b = (params.bucket(h).1 - base_b) as usize;
        buckets[b].push(h);
        owners[b].push(i);
    }
    // largest first; among equal sizes, the lowest bucket first
    let mut queue: BinaryHeap<(usize, std::cmp::Reverse<usize>)> =
        buckets.iter().enumerate().filter(|(_, b)| !b.is_empty()).map(|(i, b)| (b.len(), std::cmp::Reverse(i))).collect();
    let mut taken: Vec<u32> = vec![u32::MAX; ns]; // the bucket in each slot
    let mut pilots = vec![0u8; nb];
    let mut recent = [usize::MAX; RECENT];
    let mut recent_at = 0usize;
    let mut evictions = 0u64;
    let limit = 64 * members.len() as u64 + 4096;
    let mut cand: Vec<usize> = Vec::new();
    while let Some((_, std::cmp::Reverse(b))) = queue.pop() {
        let hs = &buckets[b];
        let mut best: Option<(u64, u8)> = None;
        let mut placed = false;
        for pilot in 0..=255u8 {
            cand.clear();
            cand.extend(hs.iter().map(|&h| params.slot_in_part(h, pilot) as usize));
            if has_duplicate(&mut cand.clone()) {
                continue;
            }
            if cand.iter().all(|&s| taken[s] == u32::MAX) {
                assign(&mut taken, &cand, b);
                pilots[b] = pilot;
                placed = true;
                break;
            }
            // the cost of evicting what this pilot collides with
            let mut hit: Vec<usize> = cand.iter().filter(|&&s| taken[s] != u32::MAX).map(|&s| taken[s] as usize).collect();
            hit.sort_unstable();
            hit.dedup();
            if hit.iter().any(|o| recent.contains(o)) {
                continue;
            }
            let cost: u64 = hit.iter().map(|&o| (buckets[o].len() * buckets[o].len()) as u64).sum();
            if best.is_none_or(|(c, _)| cost < c) {
                best = Some((cost, pilot));
            }
        }
        if !placed {
            let (_, pilot) = best?;
            cand.clear();
            cand.extend(hs.iter().map(|&h| params.slot_in_part(h, pilot) as usize));
            let mut hit: Vec<usize> = cand.iter().filter(|&&s| taken[s] != u32::MAX).map(|&s| taken[s] as usize).collect();
            hit.sort_unstable();
            hit.dedup();
            for &o in &hit {
                for &h in &buckets[o] {
                    taken[params.slot_in_part(h, pilots[o]) as usize] = u32::MAX;
                }
                queue.push((buckets[o].len(), std::cmp::Reverse(o)));
                evictions += 1;
            }
            if evictions > limit {
                return None;
            }
            assign(&mut taken, &cand, b);
            pilots[b] = pilot;
        }
        recent[recent_at] = b;
        recent_at = (recent_at + 1) % RECENT;
    }
    let mut slots = vec![None; ns];
    for (b, hs) in buckets.iter().enumerate() {
        for (j, &h) in hs.iter().enumerate() {
            slots[params.slot_in_part(h, pilots[b]) as usize] = Some(owners[b][j]);
        }
    }
    Some(Part { pilots, slots })
}

fn assign(taken: &mut [u32], cand: &[usize], b: usize) {
    for &s in cand {
        taken[s] = b as u32;
    }
}

fn has_duplicate(v: &mut [usize]) -> bool {
    v.sort_unstable();
    v.windows(2).any(|w| w[0] == w[1])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(keys: &[Vec<u8>]) -> Built {
        let refs: Vec<&[u8]> = keys.iter().map(|k| k.as_slice()).collect();
        let built = build(&refs).expect("built");
        let mut seen = vec![false; built.params.total_slots() as usize];
        for (i, k) in refs.iter().enumerate() {
            let s = slot(&built.params, &built.pilots, hash(k, built.params.seed)) as usize;
            assert!(!seen[s], "two keys in slot {s}");
            seen[s] = true;
            assert_eq!(built.slots[s], Some(i as u32));
        }
        built
    }

    fn keys(n: usize) -> Vec<Vec<u8>> {
        (0..n).map(|i| format!("arr/c/{}/{}/{}", i / 10000, (i / 100) % 100, i % 100).into_bytes()).collect()
    }

    #[test]
    fn one_key() {
        let b = check(&[b"zarr.json".to_vec()]);
        assert_eq!((b.params.parts, b.params.slots, b.params.buckets), (1, 2, 1));
    }

    #[test]
    fn sizes() {
        for n in [2, 3, 10, 100, 1000, 10_000, 100_000] {
            check(&keys(n));
        }
    }

    #[test]
    fn duplicate_keys_fail() {
        let refs: Vec<&[u8]> = vec![b"a", b"a"];
        assert!(build(&refs).is_none());
    }
}

/// The test vectors of spec/bindings/phkv.md Appendix C
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#appendix-c-constants-and-test-vectors>.
#[cfg(test)]
mod vectors {
    use super::*;

    #[test]
    fn xxh64() {
        assert_eq!(hash(b"", 0), 0xEF46_DB37_51D8_E999);
        assert_eq!(hash(b"abc", 0), 0x44BC_2CF5_AD77_0999);
        assert_eq!(hash(b"zarr.json", 1), 0xB7C2_3067_5AB7_BA48);
    }

    #[test]
    fn fmix() {
        assert_eq!(fmix64(0), 0);
        assert_eq!(fmix64(1), 0xB456_BCFC_34C2_CB2C);
        assert_eq!(fmix64(0x0123_4567_89AB_CDEF), 0x87CB_FBFE_8902_2CEA);
        assert_eq!(mulhi(0x8000_0000_0000_0000, 10), 5);
        assert_eq!(mulhi(u64::MAX, 7), 6);
    }

    #[test]
    fn query() {
        let params = Params { seed: 0, parts: 1, slots: 6, buckets: 2 };
        let pilots = [1u8, 3];
        for (k, s) in [(&b"zarr.json"[..], 2), (b"a/zarr.json", 3), (b"a/c/0", 4), (b"a/c/1", 1), (b"b", 5)] {
            assert_eq!(slot(&params, &pilots, hash(k, 0)), s, "{}", String::from_utf8_lossy(k));
        }
    }
}
