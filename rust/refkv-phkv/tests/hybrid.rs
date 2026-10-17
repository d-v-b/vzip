//! Hybrid stores: vzip archives with a phkv container in `__vz__/phkv` (spec/bindings/phkv.md §21
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#21-hybrid-profile>).

use refkv_phkv::format::{self, Superblock};
use refkv_phkv::{Codec, EntryInfo, HybridWriter, Kind, NoUrls, Options, Prop, Request, Store};
use refkv_vzip::pb::Range;

fn build(page_size: Option<u64>, n: usize) -> Vec<u8> {
    let zip = refkv_vzip::Options { mirror_refs: true, page_size };
    let mut w = HybridWriter::new(Vec::new(), zip, Options::default()).unwrap();
    let big: Vec<u8> = (0..50_000u32).flat_map(|i| (i % 13).to_le_bytes()).collect();
    w.add_bytes("a/c/0", &big, false, false).unwrap();
    w.add_bytes("a/c/1", &big, true, false).unwrap();
    w.add_bytes("a/zarr.json", b"{\"zarr_format\":3}", true, true).unwrap();
    w.add_bytes("empty", b"", false, false).unwrap();
    let hidden = w.add_hidden("extra", b"hidden bytes", false, false).unwrap();
    assert_eq!(hidden, "__vz__/extra");
    let header = w.blob(b"HDR").unwrap();
    let own = w.internal("a/c/0").unwrap();
    w.add_ranges("r/one", vec![Range::of(own, 4, 8)]).unwrap();
    w.add_ranges("r/concat", vec![Range::of(header, 0, 3), Range::literal(b"!".to_vec()), Range::of(own, 0, 4)]).unwrap();
    for i in 0..n {
        w.add_bytes(&format!("m/{i:06}"), &(i as u64).to_le_bytes(), false, false).unwrap();
    }
    w.set_property("a/c/0", "mtime", Prop::I64(42)).unwrap();
    w.set_property("__vz__/extra", "org.example:note", Prop::Str("hidden".into())).unwrap();
    w.set_metadata("writer", b"hybrid test").unwrap();
    w.close().unwrap()
}

fn check(b: Vec<u8>, n: usize) {
    // still a vzip archive: its end of central directory record and comment are last
    let len = b.len();
    let comment_at = if &b[len - 38..len - 32] == b"vzip/0" { len - 38 } else { len - 22 };
    assert_eq!(&b[comment_at..comment_at + 6], b"vzip/0");
    assert_eq!(&b[comment_at - 22..comment_at - 18], &0x0605_4b50u32.to_le_bytes());

    let file_size = b.len() as u64;
    let s = Store::open(b).unwrap();
    let sb: Superblock = *s.superblock();
    assert!(sb.hybrid());
    assert!(sb.distance > 0, "the central directory follows the container");
    let big: Vec<u8> = (0..50_000u32).flat_map(|i| (i % 13).to_le_bytes()).collect();
    let get = |k: &str| s.get(k, Request::Whole, &NoUrls).unwrap();

    assert_eq!(get("a/c/0").unwrap(), big);
    assert_eq!(get("a/c/1").unwrap(), big);
    assert_eq!(s.get("a/c/1", Request::Range(8, 12), &NoUrls).unwrap().unwrap(), big[8..12]);
    assert_eq!(get("a/zarr.json").unwrap(), b"{\"zarr_format\":3}");
    assert_eq!(get("empty").unwrap(), b"");
    assert_eq!(get("r/one").unwrap(), big[4..12]);
    let mut concat = b"HDR!".to_vec();
    concat.extend_from_slice(&big[..4]);
    assert_eq!(get("r/concat").unwrap(), concat);
    for i in (0..n).step_by(97) {
        assert_eq!(get(&format!("m/{i:06}")).unwrap(), (i as u64).to_le_bytes());
    }

    // hidden entries are outside the mapping, but in the raw view
    for hidden in ["__vz__/extra", "__vz__/sources", format::HYBRID_KEY] {
        assert_eq!(s.classify(hidden).unwrap(), None, "{hidden}");
        assert_eq!(get(hidden), None, "{hidden}");
    }
    assert_eq!(s.raw("__vz__/extra").unwrap().unwrap(), b"hidden bytes");
    let region = s.raw(format::HYBRID_KEY).unwrap().unwrap();
    assert_eq!(region.len() as u64, sb.region_length);
    // the region holds the superblock, then zero bytes up to the central directory, which ends the
    // file `distance` bytes after the superblock
    let sb_at = file_size - sb.distance - format::SUPERBLOCK_SIZE;
    assert!(sb.region_offset <= sb_at && sb_at + format::SUPERBLOCK_SIZE <= sb.region_offset + sb.region_length);
    let after = (sb_at + format::SUPERBLOCK_SIZE - sb.region_offset) as usize;
    assert!(region[after..].iter().all(|&b| b == 0));
    assert_eq!(&region[after - 64..after - 60], format::MAGIC);
    assert!(s.raw("__vz__/sources").unwrap().is_some(), "the source table, inflated");

    // raw follows vzip: decoded values, and a reference's body (its payload)
    assert_eq!(s.raw("a/c/1").unwrap().unwrap(), big);
    assert_eq!(s.raw("r/one").unwrap().unwrap(), Range::of(1, 4, 8).encode());
    assert!(matches!(s.entry_info("a/c/1").unwrap(), Some(EntryInfo::Bytes { codec: Codec::Deflate, .. })));
    assert!(matches!(s.entry_info("__vz__/extra").unwrap(), Some(EntryInfo::Bytes { hidden: true, .. })));
    assert_eq!(s.classify("r/concat").unwrap(), Some(Kind::Reference));

    let mut want = vec!["a/c/0", "a/c/1", "a/zarr.json", "empty", "r/concat", "r/one"].into_iter().map(String::from).collect::<Vec<_>>();
    want.splice(4..4, (0..n).map(|i| format!("m/{i:06}")));
    assert_eq!(s.list("").unwrap(), want);
    assert_eq!(s.list("__vz__/").unwrap(), Vec::<String>::new());
    assert_eq!(s.list("a/").unwrap().len(), 3);

    assert_eq!(s.property("a/c/0", "mtime").unwrap(), Some(Some(Prop::I64(42))));
    assert_eq!(s.property("__vz__/extra", "org.example:note").unwrap(), Some(Some(Prop::Str("hidden".into()))));
    assert_eq!(s.metadata(), [("writer".to_string(), b"hybrid test".to_vec())]);
}

#[test]
fn unpaged() {
    check(build(None, 10), 10);
}

#[test]
fn paged() {
    check(build(Some(512), 10), 10);
}

#[test]
fn many_entries_push_the_superblock_further() {
    let b = build(Some(4096), 20_000);
    let s = Store::open(b.clone()).unwrap();
    assert!(s.superblock().distance >= 1 << 20, "a central directory of 20,000 records is over 1 MiB");
    check(b, 20_000);
}

#[test]
fn deterministic() {
    assert_eq!(build(Some(1024), 100), build(Some(1024), 100));
}

#[test]
fn refuses() {
    let zip = refkv_vzip::Options { mirror_refs: false, page_size: None };
    assert!(HybridWriter::new(Vec::new(), zip, Options::default()).is_err(), "mirror_refs");
    let zip = refkv_vzip::Options::default();
    let mut w = HybridWriter::new(Vec::new(), zip, Options::default()).unwrap();
    assert!(w.add_hidden("phkv", b"x", false, false).is_err());
    w.set_property("ghost", "mode", Prop::U64(1)).unwrap();
    assert!(w.close().is_err(), "a property of a missing key");
}
