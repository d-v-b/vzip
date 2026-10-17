//! Standalone phkv stores, written and read back (spec/bindings/phkv.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>).

use refkv_phkv::format::{self, Superblock};
use refkv_phkv::{Class, Codec, EntryInfo, Kind, NoUrls, Options, Prop, Request, Resolver, Store, Writer};
use refkv_vzip::pb::{Range, Source};
use std::collections::HashMap;

fn write(opts: Options, f: impl FnOnce(&mut Writer<Vec<u8>>)) -> Vec<u8> {
    let mut w = Writer::new(Vec::new(), opts);
    f(&mut w);
    w.close().expect("close")
}

fn whole(s: &Store<Vec<u8>>, key: &str) -> Option<Vec<u8>> {
    s.get(key, Request::Whole, &NoUrls).expect("get")
}

/// A resolver over in-memory objects, which checks the size pin.
struct Objects(HashMap<String, Vec<u8>>);

impl Resolver for Objects {
    fn read(&self, src: &Source, offset: u64, length: u64) -> Result<Vec<u8>, String> {
        let o = self.0.get(src.url_str().unwrap()).ok_or("no such object")?;
        if src.size.is_some_and(|n| n != o.len() as u64) {
            return Err("size pin fails".into());
        }
        o.get(offset as usize..(offset + length) as usize).map(|b| b.to_vec()).ok_or_else(|| "short object".into())
    }
}

#[test]
fn empty_store() {
    let b = write(Options::default(), |_| {});
    let s = Store::open(b).unwrap();
    assert_eq!(s.key_count(), 0);
    assert_eq!(s.classify("a").unwrap(), None);
    assert_eq!(whole(&s, "a"), None);
    assert!(s.list("").unwrap().is_empty());
    assert_eq!(s.revision(), Some(refkv_vzip::SPEC_REVISION));
}

#[test]
fn one_key() {
    let b = write(Options::default(), |w| w.add_bytes("zarr.json", b"{}").unwrap());
    let s = Store::open(b).unwrap();
    assert_eq!(s.classify("zarr.json").unwrap(), Some(Kind::Bytes));
    assert_eq!(whole(&s, "zarr.json").unwrap(), b"{}");
    assert_eq!(s.classify("zarr.jso").unwrap(), None);
    assert_eq!(s.list("").unwrap(), ["zarr.json"]);
}

#[test]
fn values_references_and_listing() {
    let big: Vec<u8> = (0..100_000u32).flat_map(|i| (i % 97).to_le_bytes()).collect();
    let object: Vec<u8> = (0..=255u8).cycle().take(5000).collect();
    let opts = Options { inline_max: 64, ..Options::default() };
    let b = write(opts, |w| {
        w.add_bytes("a/zarr.json", b"{\"zarr_format\":3}").unwrap();
        w.add_bytes_with("a/c/0", &big, Codec::None).unwrap();
        w.add_bytes_with("a/c/1", &big, Codec::Deflate).unwrap();
        w.add_bytes_with("a/c/2", &big, Codec::Zstd).unwrap();
        w.add_bytes_with("a/c/3", b"tiny", Codec::Zstd).unwrap(); // inline and compressed
        w.add_bytes("empty", b"").unwrap();
        let header = w.blob(b"HEADER").unwrap();
        let shared = w.internal("a/c/2").unwrap();
        let url = w.source(Source { size: Some(object.len() as u64), ..Source::url("https://example.org/o.bin") }).unwrap();
        w.add_ranges("r/one", &[Range::of(url, 10, 20)]).unwrap();
        w.add_ranges("r/concat", &[Range::of(header, 0, 6), Range::literal(b"--".to_vec()), Range::of(shared, 4, 8)]).unwrap();
        let crc = format::crc32c(&object[100..200]);
        w.add_ranges("r/crc", &[Range { crc32c: Some(crc), ..Range::of(url, 100, 100) }]).unwrap();
        w.set_property("a/c/0", "mtime", Prop::I64(-5)).unwrap();
        w.set_property("a/c/2", "mtime", Prop::I64(1_700_000_000_000_000_000)).unwrap();
        w.set_property("a/zarr.json", "media_type", Prop::Str("application/json".into())).unwrap();
        w.set_property("r/one", "org.example:tag", Prop::Bytes(vec![1, 2, 3])).unwrap();
        w.set_metadata("writer", b"tests").unwrap();
    });
    let s = Store::open(b).unwrap();
    let objects = Objects(HashMap::from([("https://example.org/o.bin".to_string(), object.clone())]));

    for k in ["a/c/0", "a/c/1", "a/c/2"] {
        assert_eq!(whole(&s, k).unwrap(), big, "{k}");
        assert_eq!(s.get(k, Request::Range(10, 20), &NoUrls).unwrap().unwrap(), big[10..20]);
        assert_eq!(s.get(k, Request::Suffix(3), &NoUrls).unwrap().unwrap(), big[big.len() - 3..]);
        assert_eq!(s.get(k, Request::Offset(big.len() as u64 + 9), &NoUrls).unwrap().unwrap(), b"");
    }
    assert_eq!(whole(&s, "a/c/3").unwrap(), b"tiny");
    assert_eq!(whole(&s, "empty").unwrap(), b"");
    assert_eq!(s.get("r/one", Request::Whole, &objects).unwrap().unwrap(), object[10..30]);
    let mut concat = b"HEADER--".to_vec();
    concat.extend_from_slice(&big[4..12]);
    assert_eq!(whole(&s, "r/concat").unwrap(), concat);
    assert_eq!(s.get("r/concat", Request::Range(5, 9), &NoUrls).unwrap().unwrap(), concat[5..9]);
    assert_eq!(s.get("r/crc", Request::Range(1, 2), &objects).unwrap().unwrap(), object[101..102]);
    // a url source without a resolver is a resolution error, for the requests that need it
    assert_eq!(s.get("r/one", Request::Whole, &NoUrls).unwrap_err().class, Class::Resolution);
    assert_eq!(s.get("a/c/0", Request::Range(5, 4), &NoUrls).unwrap_err().class, Class::Request);

    assert_eq!(s.classify("r/one").unwrap(), Some(Kind::Reference));
    assert_eq!(s.list("").unwrap(), ["a/c/0", "a/c/1", "a/c/2", "a/c/3", "a/zarr.json", "empty", "r/concat", "r/crc", "r/one"]);
    assert_eq!(s.list("a/c/").unwrap(), ["a/c/0", "a/c/1", "a/c/2", "a/c/3"]);
    assert_eq!(s.list("r/c").unwrap(), ["r/concat", "r/crc"]);
    assert!(s.list("zzz").unwrap().is_empty());

    // raw gives the stored bytes, and entry_info says how to read them
    let raw = s.raw("a/c/2").unwrap().unwrap();
    let Some(EntryInfo::Bytes { codec: Codec::Zstd, stored_length, size, .. }) = s.entry_info("a/c/2").unwrap() else { panic!() };
    assert_eq!((raw.len() as u64, size), (stored_length, big.len() as u64));
    assert_eq!(format::decode(Codec::Zstd, &raw, size).unwrap(), big);
    assert_eq!(s.raw("a/c/0").unwrap().unwrap(), big);
    assert_eq!(s.raw("r/one").unwrap().unwrap(), Range::of(2, 10, 20).encode());

    assert_eq!(s.property("a/c/0", "mtime").unwrap(), Some(Some(Prop::I64(-5))));
    assert_eq!(s.property("a/c/2", "mtime").unwrap(), Some(Some(Prop::I64(1_700_000_000_000_000_000))));
    assert_eq!(s.property("a/c/1", "mtime").unwrap(), Some(None));
    assert_eq!(s.property("a/zarr.json", "media_type").unwrap(), Some(Some(Prop::Str("application/json".into()))));
    assert_eq!(s.property("r/one", "org.example:tag").unwrap(), Some(Some(Prop::Bytes(vec![1, 2, 3]))));
    assert_eq!(s.property("r/concat", "org.example:tag").unwrap(), Some(None));
    assert_eq!(s.property("nope", "mtime").unwrap(), None);
    assert_eq!(s.property("a/c/0", "org.example:none").unwrap(), Some(None));
    assert_eq!(s.metadata(), [("writer".to_string(), b"tests".to_vec())]);
}

#[test]
fn many_keys_and_misses() {
    let n = 50_000;
    let opts = Options { page_size: 1024, ..Options::default() };
    let b = write(opts, |w| {
        for i in 0..n {
            w.add_bytes(&format!("arr/c/{}/{}", i / 100, i % 100), &(i as u32).to_le_bytes()).unwrap();
        }
    });
    let s = Store::open(b).unwrap();
    for i in 0..n {
        assert_eq!(whole(&s, &format!("arr/c/{}/{}", i / 100, i % 100)).unwrap(), (i as u32).to_le_bytes());
        assert_eq!(whole(&s, &format!("arr/c/{}/{}x", i / 100, i % 100)), None);
    }
    assert_eq!(s.list("").unwrap().len(), n);
    assert_eq!(s.list("arr/c/12/").unwrap().len(), 100);
    assert_eq!(s.list("arr/c/12").unwrap().len(), 1100); // 12/, and 120/ to 129/
}

#[test]
fn every_probe_distance() {
    for d in format::probe_distances().take_while(|&d| d <= 1 << 20) {
        let b = write(Options { distance: d, ..Options::default() }, |w| w.add_bytes("k", b"v").unwrap());
        let s = Store::open(b).unwrap_or_else(|e| panic!("D = {d}: {e}"));
        assert_eq!(s.superblock().distance, d);
        assert_eq!(whole(&s, "k").unwrap(), b"v");
    }
    let r = Writer::new(Vec::new(), Options { distance: 3, ..Options::default() }).close();
    assert!(r.is_err(), "3 is not a probe distance");
}

#[test]
fn appended_bytes_hide_the_superblock() {
    // the superblock records its distance, so bytes appended after it do not leave a stale match
    let mut b = write(Options::default(), |w| w.add_bytes("k", b"v").unwrap());
    b.extend_from_slice(&[0u8; 1024]);
    assert_eq!(Store::open(b).err().unwrap().class, Class::Archive);
}

#[test]
fn deterministic() {
    let make = || {
        write(Options::default(), |w| {
            for i in (0..1000).rev() {
                w.add_bytes_with(&format!("k{i}"), format!("value {i}").repeat(i % 7).as_bytes(), if i % 2 == 0 { Codec::Zstd } else { Codec::Deflate }).unwrap();
            }
            w.set_property("k3", "mode", Prop::U64(0o644)).unwrap();
        })
    };
    assert_eq!(make(), make());
}

#[test]
fn writer_rejects() {
    let mut w = Writer::new(Vec::new(), Options::default());
    w.add_bytes("a", b"x").unwrap();
    assert!(w.add_bytes("a", b"y").is_err(), "duplicate key");
    assert!(w.add_bytes("", b"y").is_err(), "empty key");
    assert!(w.url("not a uri").is_err());
    assert!(w.add_ranges("r", &[Range::of(7, 0, 1)]).is_err(), "unregistered source");
    assert!(w.set_property("a", "mtime", Prop::U64(1)).is_err(), "registered type");
    assert!(w.set_property("a", "unregistered", Prop::U64(1)).is_err(), "unregistered name without ':'");
    let blob = w.blob(b"abc").unwrap();
    w.add_ranges("r", &[Range::of(blob, 2, 2)]).unwrap();
    assert!(w.close().is_err(), "a range past the end of a data source");

    let mut w = Writer::new(Vec::new(), Options::default());
    let k = w.internal("missing").unwrap();
    w.add_ranges("r", &[Range::of(k, 0, 1)]).unwrap();
    assert!(w.close().is_err(), "a key source naming a missing key");

    let mut w = Writer::new(Vec::new(), Options::default());
    w.set_property("ghost", "mode", Prop::U64(1)).unwrap();
    w.add_bytes("a", b"x").unwrap();
    assert!(w.close().is_err(), "a property of a missing key");
}

/// The superblock of a store written with D = 0.
fn superblock(b: &[u8]) -> Superblock {
    Superblock::matching(&b[b.len() - 64..], 0).unwrap()
}

#[test]
fn corruption() {
    let opts = Options { inline_max: 0, ..Options::default() };
    let build = || write(opts, |w| {
        w.add_bytes_with("a", b"0123456789", Codec::None).unwrap();
        w.add_bytes_with("b", &[7u8; 1000], Codec::Deflate).unwrap();
        w.add_bytes_with("c", &[9u8; 1000], Codec::Zstd).unwrap();
    });

    // not a store at all
    assert_eq!(Store::open(vec![0u8; 100]).err().unwrap().class, Class::Archive);
    // the superblock's checksum
    let mut b = build();
    let n = b.len();
    b[n - 30] ^= 1;
    assert_eq!(Store::open(b).err().unwrap().class, Class::Archive);
    // the directory's checksum
    let mut b = build();
    let sb = superblock(&b);
    b[sb.directory_offset as usize + 10] ^= 1;
    assert_eq!(Store::open(b).err().unwrap().class, Class::Archive);

    // a value's bytes: a codec-0 value fails its CRC-32C when read whole, not in part
    let mut b = build();
    b[3] ^= 0xff; // "a" is written first, at offset 0
    let s = Store::open(b).unwrap();
    assert_eq!(s.get("a", Request::Whole, &NoUrls).unwrap_err().class, Class::Body);
    assert_eq!(s.get("a", Request::Range(5, 7), &NoUrls).unwrap().unwrap(), b"56");
    assert_eq!(s.raw("a").unwrap_err().class, Class::Body);

    // compressed values: a damaged stream is a body error, and so is raw's crc only for codec 0
    let b = build();
    let s = Store::open(b.clone()).unwrap();
    let Some(EntryInfo::Bytes { stored_length, .. }) = s.entry_info("b").unwrap() else { panic!() };
    let mut b2 = b.clone();
    let at = 10 + stored_length as usize - 1; // the last byte of "b"'s DEFLATE stream
    b2[at] ^= 0xff;
    let s2 = Store::open(b2).unwrap();
    assert_eq!(s2.get("b", Request::Range(0, 1), &NoUrls).unwrap_err().class, Class::Body);
    assert!(s2.raw("b").is_ok(), "raw does not decode");
    assert_eq!(whole(&s2, "c").unwrap(), vec![9u8; 1000]);
}

#[test]
fn slot_pointing_outside_records_is_an_entry_error() {
    let b = write(Options::default(), |w| w.add_bytes("k", b"v").unwrap());
    let sb = superblock(&b);
    let dir = format::DirEntry::decode_all(&b[sb.directory_offset as usize..(sb.directory_offset + sb.directory_length) as usize]).unwrap();
    let slots = dir.iter().find(|e| e.kind == format::section::SLOTS).unwrap();
    let s = Store::open(b.clone()).unwrap();
    assert!(s.classify("k").unwrap().is_some());
    // point every non-empty slot at offset 1, inside no record's start but inside the file
    let mut b2 = b.clone();
    for i in 0..slots.length / 8 {
        let at = (slots.offset + 8 * i) as usize;
        if format::le_u64(&b2, at) != format::EMPTY_SLOT {
            b2[at..at + 8].copy_from_slice(&(u64::MAX - 1).to_le_bytes());
        }
    }
    let s2 = Store::open(b2).unwrap();
    assert_eq!(s2.classify("k").unwrap_err().class, Class::Entry);
}

#[test]
fn reads_from_a_file() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.phkv");
    let src = dir.path().join("big.bin");
    std::fs::write(&src, vec![42u8; 1 << 20]).unwrap();
    let f = std::fs::File::create(&path).unwrap();
    let mut w = Writer::new(f, Options::default());
    w.add_file("big", &src).unwrap();
    w.add_bytes("small", b"s").unwrap();
    w.close().unwrap();
    let s = Store::open(std::fs::File::open(&path).unwrap()).unwrap();
    assert_eq!(s.get("big", Request::Range(1000, 1004), &NoUrls).unwrap().unwrap(), [42u8; 4]);
    assert_eq!(s.get("big", Request::Whole, &NoUrls).unwrap().unwrap().len(), 1 << 20);
    assert_eq!(s.get("small", Request::Whole, &NoUrls).unwrap().unwrap(), b"s");
}
