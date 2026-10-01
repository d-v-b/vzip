mod common;
use common::*;
use vzip::proto::{self, CdIndex, Page, Pinned};
use vzip::{Kind, Request};

fn std_entries() -> Vec<E> {
    vec![
        e_stored("a", b"hello"),
        e_deflate("b", b"world world world"),
        e_concat("c", vec![lit(b"xy"), src_range(0, 1, 3), lit(b"")]),
        e_range("d", src_range(1, 0, 2)),
    ]
}

fn std_sources() -> Vec<u8> {
    table(vec![data_src(b"0123456"), key_src("a")])
}

#[test]
fn reads_crafted_archive_unpaged_paged_and_zip64() {
    let mk = |paged: bool, z64: bool| {
        let f = one_page_per_record;
        build_full(&std_entries(), &std_sources(), if paged { Some(&f) } else { None }, z64).bytes
    };
    for (paged, z64) in [(false, false), (true, false), (false, true), (true, true)] {
        let ar = open_bytes(&mk(paged, z64)).unwrap();
        assert_eq!(ar.classify("a").unwrap(), Kind::Bytes);
        assert_eq!(ar.classify("c").unwrap(), Kind::Reference);
        assert_eq!(ar.classify("zz").unwrap(), Kind::Missing);
        assert_eq!(ar.classify("0").unwrap(), Kind::Missing);
        assert_eq!(ar.get("a", Request::Whole).unwrap().unwrap(), b"hello");
        assert_eq!(ar.get("b", Request::Range(6, 11)).unwrap().unwrap(), b"world");
        assert_eq!(ar.get("c", Request::Whole).unwrap().unwrap(), b"xy123");
        assert_eq!(ar.get("c", Request::Range(1, 3)).unwrap().unwrap(), b"y1");
        assert_eq!(ar.get("c", Request::Suffix(100)).unwrap().unwrap(), b"xy123");
        assert_eq!(ar.get("c", Request::Offset(100)).unwrap().unwrap(), b"");
        assert_eq!(ar.get("c", Request::Range(50, 60)).unwrap().unwrap(), b"");
        assert_eq!(ar.get("d", Request::Whole).unwrap().unwrap(), b"he");
        assert_eq!(ar.list("").unwrap(), vec!["a", "b", "c", "d"]);
        assert_eq!(ar.list("c").unwrap(), vec!["c"]);
        assert_eq!(ar.get("zz", Request::Whole).unwrap(), None);
        assert_eq!(ar.raw("__vz__/sources").unwrap().unwrap(), std_sources());
        assert_eq!(ar.raw("__vz__/index").unwrap().is_some(), paged);
        assert_eq!(ar.raw("c").unwrap().unwrap(), proto::encode_concat(&proto::Concat {
            parts: vec![lit(b"xy"), src_range(0, 1, 3), lit(b"")]
        }));
    }
}

// ---------------------------------------------------------------- archive errors

#[test]
fn archive_error_not_zip() {
    assert_eq!(class(open_bytes(b"this is not a zip file at all, not even close to one....")), "archive");
}

#[test]
fn archive_error_empty_file() {
    assert_eq!(class(open_bytes(b"")), "archive");
}

#[test]
fn archive_error_missing_file() {
    assert_eq!(class(vzip::Archive::open(std::path::Path::new("/nonexistent/x.vzip"))), "archive");
}

#[test]
fn archive_error_unsupported_version() {
    let mut b = build(&std_entries(), &std_sources());
    let n = b.len();
    b[n - 22 + 5] = b'1';
    let e = open_bytes(&b).err().unwrap();
    assert_eq!(e.class(), "archive");
    assert!(e.message().contains("unsupported"));
}

#[test]
fn archive_error_bad_magic() {
    let mut b = build(&std_entries(), &std_sources());
    let n = b.len();
    b[n - 22] = b'x';
    assert_eq!(class(open_bytes(&b)), "archive");
}

#[test]
fn archive_error_sources_truncated() {
    let mut b = build(&std_entries(), &std_sources());
    let n = b.len();
    let sz = u64::from_le_bytes(b[n - 8..].try_into().unwrap());
    b[n - 8..].copy_from_slice(&(sz - 1).to_le_bytes());
    assert_eq!(class(open_bytes(&b)), "archive");
}

#[test]
fn archive_error_sources_trailing_bytes() {
    let mut b = build(&std_entries(), &std_sources());
    let n = b.len();
    let sz = u64::from_le_bytes(b[n - 8..].try_into().unwrap());
    b[n - 8..].copy_from_slice(&(sz + 1).to_le_bytes());
    assert_eq!(class(open_bytes(&b)), "archive");
}

#[test]
fn archive_error_sources_outside_file() {
    let mut b = build(&std_entries(), &std_sources());
    let n = b.len();
    b[n - 8..].copy_from_slice(&(1u64 << 40).to_le_bytes());
    assert_eq!(class(open_bytes(&b)), "archive");
}

#[test]
fn archive_error_source_table_malformed() {
    assert_eq!(class(open_bytes(&build(&[], &[0x0b]))), "archive");
}

#[test]
fn archive_error_source_without_kind() {
    assert_eq!(class(open_bytes(&build(&[], &table(vec![Default::default()])))), "archive");
}

#[test]
fn archive_error_empty_url() {
    assert_eq!(class(open_bytes(&build(&[], &table(vec![url_src("")])))), "archive");
}

#[test]
fn archive_error_pin_on_data_source() {
    let mut s = data_src(b"x");
    s.size = Some(1);
    assert_eq!(class(open_bytes(&build(&[], &table(vec![s])))), "archive");
}

#[test]
fn archive_error_weak_etag() {
    let mut s = url_src("x");
    s.etag = Some("W/\"a\"".into());
    assert_eq!(class(open_bytes(&build(&[], &table(vec![s])))), "archive");
}

#[test]
fn archive_error_cd_outside_file() {
    let mut b = build(&std_entries(), &std_sources());
    let n = b.len();
    let off = n - 44 + 16;
    b[off..off + 4].copy_from_slice(&0x7fff_0000u32.to_le_bytes());
    assert_eq!(class(open_bytes(&b)), "archive");
}

#[test]
fn archive_error_cd_bad_signature() {
    let bl = build_full(&std_entries(), &std_sources(), None, false);
    let mut b = bl.bytes;
    b[bl.cd_offset as usize] = b'Q';
    assert_eq!(class(open_bytes(&b)), "archive");
}

#[test]
fn archive_error_index_record_without_page_index() {
    let mut es = std_entries();
    es.push(e_stored("__vz__/index", b""));
    assert_eq!(class(open_bytes(&build(&es, &std_sources()))), "archive");
}

#[test]
fn archive_error_zip64_locator_missing() {
    let bl = build_full(&std_entries(), &std_sources(), None, true);
    let mut b = bl.bytes;
    let loc = bl.eocd_offset as usize - 20;
    b[loc] = 0;
    assert_eq!(class(open_bytes(&b)), "archive");
}

#[test]
fn archive_error_zip64_record_bad_size() {
    let bl = build_full(&std_entries(), &std_sources(), None, true);
    let mut b = bl.bytes;
    let z = bl.eocd_offset as usize - 20 - 56;
    b[z + 4] = 45;
    assert_eq!(class(open_bytes(&b)), "archive");
}

fn paged_with(f: &dyn Fn(&[RecInfo]) -> Vec<u8>) -> Result<vzip::Archive, vzip::Error> {
    open_bytes(&build_full(&std_entries(), &std_sources(), Some(f), false).bytes)
}

fn pages_of(infos: &[RecInfo]) -> Vec<Page> {
    infos.iter().map(|i| Page { first_key: i.0.clone(), offset: i.1, length: i.2 }).collect()
}

fn ix(pages: Vec<Page>, pinned: Vec<Pinned>) -> Vec<u8> {
    proto::encode_cd_index(&CdIndex { pages, pinned })
}

#[test]
fn archive_error_index_does_not_decode() {
    assert_eq!(class(paged_with(&|_| vec![0xff])), "archive");
}

#[test]
fn archive_error_page_length_zero() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pages_of(i);
        p.push(Page { first_key: "zz".into(), offset: p.last().map(|x| x.offset + x.length).unwrap(), length: 0 });
        ix(p, vec![])
    })), "archive");
}

#[test]
fn archive_error_page_outside_cd() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pages_of(i);
        p.last_mut().unwrap().length += 100000;
        ix(p, vec![])
    })), "archive");
}

#[test]
fn archive_error_pages_not_contiguous() {
    assert_eq!(class(paged_with(&|i| {
        let p = pages_of(i);
        ix(p[1..].to_vec(), vec![])
    })), "archive");
}

#[test]
fn archive_error_first_keys_not_increasing() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pages_of(i);
        p[1].first_key = "a".into();
        ix(p, vec![])
    })), "archive");
}

#[test]
fn archive_error_empty_first_key() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pages_of(i);
        p[0].first_key = "".into();
        ix(p, vec![])
    })), "archive");
}

fn pin(i: &RecInfo) -> Pinned {
    Pinned { key: i.0.clone(), data_offset: i.3, size: i.5, csize: i.4, method: i.6 as u32 }
}

#[test]
fn archive_error_pinned_empty_key() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pin(&i[0]);
        p.key = "".into();
        ix(pages_of(i), vec![p])
    })), "archive");
}

#[test]
fn archive_error_pinned_twice() {
    assert_eq!(class(paged_with(&|i| ix(pages_of(i), vec![pin(&i[0]), pin(&i[0])]))), "archive");
}

#[test]
fn archive_error_pinned_format_entry() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pin(&i[0]);
        p.key = "__vz__/sources".into();
        ix(pages_of(i), vec![p])
    })), "archive");
}

#[test]
fn archive_error_pinned_bad_method() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pin(&i[0]);
        p.method = 9;
        ix(pages_of(i), vec![p])
    })), "archive");
}

#[test]
fn archive_error_pinned_body_outside_file() {
    assert_eq!(class(paged_with(&|i| {
        let mut p = pin(&i[0]);
        p.data_offset = 1 << 40;
        ix(pages_of(i), vec![p])
    })), "archive");
}

#[test]
fn paged_lookup_uses_pins_and_pages() {
    let ar = paged_with(&|i| {
        // Single page holding everything; pin "b".
        let total: u64 = i.iter().map(|x| x.2).sum();
        ix(vec![Page { first_key: "a".into(), offset: 0, length: total }], vec![pin(&i[1])])
    })
    .unwrap();
    assert_eq!(ar.get("b", Request::Whole).unwrap().unwrap(), b"world world world");
    assert_eq!(ar.list("").unwrap(), vec!["a", "b", "c", "d"]);
}

#[test]
fn paged_lookup_record_outside_its_page_range_is_missing() {
    // Page 0 starts at "b" although its first record is "a": "a" sorts before
    // every first_key, so it is missing and not listed.
    let ar = paged_with(&|i| {
        let total: u64 = i.iter().map(|x| x.2).sum();
        ix(vec![Page { first_key: "b".into(), offset: 0, length: total }], vec![])
    })
    .unwrap();
    assert_eq!(ar.classify("a").unwrap(), Kind::Missing);
    assert_eq!(ar.list("").unwrap(), vec!["b", "c", "d"]);
}

#[test]
fn entry_error_unparseable_page() {
    // Page 1 ("b") points into the middle of a record.
    let ar = paged_with(&|i| {
        let mut p = pages_of(i);
        p[1].offset += 1;
        p[1].length -= 1;
        p[0].length += 1;
        ix(p, vec![])
    })
    .unwrap();
    assert_eq!(class(ar.classify("b")), "entry");
    assert_eq!(class(ar.get("b", Request::Whole)), "entry");
    assert_eq!(class(ar.raw("b")), "entry");
    assert_eq!(class(ar.list("")), "entry");
    assert_eq!(ar.list("c").unwrap(), vec!["c"]);
    assert_eq!(class(ar.classify("a")), "entry"); // page 0 is broken too
    assert_eq!(ar.classify("c").unwrap(), Kind::Reference);
}

// ---------------------------------------------------------------- entry errors

fn one(e: E) -> vzip::Archive {
    open_bytes(&build(&[e], &table(vec![data_src(b"0123")]))).unwrap()
}

fn assert_entry_error(e: E) {
    let ar = one(e);
    assert_eq!(class(ar.classify("k")), "entry");
    assert_eq!(class(ar.get("k", Request::Whole)), "entry");
    assert_eq!(class(ar.raw("k")), "entry");
    assert_eq!(ar.list("").unwrap(), vec!["k"]);
}

#[test]
fn entry_error_extra_does_not_parse() {
    let mut e = e_stored("k", b"x");
    e.extra = vec![1, 2, 3];
    assert_entry_error(e);
}

#[test]
fn entry_error_two_reference_blocks() {
    let mut e = e_range("k", lit(b"x"));
    e.extra.extend(block(0x7A77, &[]));
    assert_entry_error(e);
}

#[test]
fn entry_error_bad_method() {
    let mut e = e_stored("k", b"x");
    e.method = 12;
    assert_entry_error(e);
}

#[test]
fn entry_error_encrypted() {
    let mut e = e_stored("k", b"x");
    e.flags |= 1;
    assert_entry_error(e);
}

#[test]
fn entry_error_reference_with_deflate() {
    let mut e = e_range("k", lit(b"x"));
    e.method = 8;
    e.body = deflate(&e.body);
    assert_entry_error(e);
}

#[test]
fn entry_error_offset_all_ones_without_zip64() {
    let mut e = e_stored("k", b"x");
    e.lho = Some(0xFFFF_FFFF);
    assert_entry_error(e);
}

#[test]
fn entry_error_zip64_block_too_short() {
    let mut e = e_stored("k", b"x");
    e.lho = Some(0xFFFF_FFFF);
    e.extra = block(1, &[0; 4]);
    assert_entry_error(e);
}

#[test]
fn entry_error_two_zip64_blocks() {
    let mut e = e_stored("k", b"x");
    e.lho = Some(0xFFFF_FFFF);
    e.extra = block(1, &0u64.to_le_bytes());
    e.extra.extend(block(1, &0u64.to_le_bytes()));
    assert_entry_error(e);
}

#[test]
fn zip64_offset_block_is_used() {
    let mut e = e_stored("k", b"xyz");
    e.lho = Some(0xFFFF_FFFF);
    e.extra = block(1, &0u64.to_le_bytes());
    assert_eq!(one(e).get("k", Request::Whole).unwrap().unwrap(), b"xyz");
}

#[test]
fn invalid_names_are_ignored() {
    let mut e = e_stored("", b"x");
    e.name = vec![0xff, 0xfe];
    let ar = open_bytes(&build(&[e, e_stored("ok", b"1")], &table(vec![]))).unwrap();
    assert_eq!(ar.list("").unwrap(), vec!["ok"]);
}

// ---------------------------------------------------------------- body errors

#[test]
fn body_error_stored_sizes_differ() {
    let mut e = e_stored("k", b"xyz");
    e.usize_ = 2;
    let ar = one(e);
    assert_eq!(class(ar.get("k", Request::Range(0, 1))), "body");
    assert_eq!(class(ar.raw("k")), "body");
    assert_eq!(ar.classify("k").unwrap(), Kind::Bytes);
}

#[test]
fn body_error_deflate_trailing_garbage() {
    let mut e = e_deflate("k", b"hello");
    e.body.push(0);
    assert_eq!(class(one(e).get("k", Request::Range(0, 1))), "body");
}

#[test]
fn body_error_deflate_wrong_size() {
    let mut e = e_deflate("k", b"hello");
    e.usize_ = 4;
    assert_eq!(class(one(e).get("k", Request::Whole)), "body");
}

#[test]
fn body_error_deflate_truncated() {
    let mut e = e_deflate("k", &[7u8; 1000]);
    e.body.pop();
    assert_eq!(class(one(e).get("k", Request::Whole)), "body");
}

#[test]
fn body_error_outside_file() {
    let mut e = e_stored("k", b"xyz");
    e.csize = Some(1 << 30);
    e.usize_ = 1 << 30;
    assert_eq!(class(one(e).get("k", Request::Range(0, 1))), "body");
}

#[test]
fn body_error_of_reference_in_raw_only() {
    let mut e = e_range("k", lit(b"x"));
    e.usize_ += 1; // STORED sizes differ
    let ar = one(e);
    assert_eq!(ar.get("k", Request::Whole).unwrap().unwrap(), b"x");
    assert_eq!(class(ar.raw("k")), "body");
}

#[test]
fn body_error_in_key_source_is_resolution_error() {
    let mut h = e_deflate("__vz__/h", b"hello");
    h.body.push(1);
    let ar = open_bytes(&build(&[h, e_range("k", src_range(0, 0, 2))], &table(vec![key_src("__vz__/h")]))).unwrap();
    assert_eq!(class(ar.get("k", Request::Whole)), "resolution");
}

// ---------------------------------------------------------------- payload errors

#[test]
fn payload_error_malformed_protobuf() {
    let ar = one(e_ref("k", 0x7A76, &[0x0b]));
    assert_eq!(ar.classify("k").unwrap(), Kind::Reference);
    assert_eq!(class(ar.get("k", Request::Whole)), "payload");
}

#[test]
fn payload_error_source_out_of_range_outside_window() {
    let ar = one(e_concat("k", vec![lit(b"ab"), src_range(5, 0, 0)]));
    assert_eq!(class(ar.get("k", Request::Range(0, 1))), "payload");
}

#[test]
fn payload_error_literal_with_offset() {
    let mut r = lit(b"ab");
    r.offset = 1;
    assert_eq!(class(one(e_range("k", r)).get("k", Request::Whole)), "payload");
}

#[test]
fn payload_error_offset_plus_length_overflow() {
    assert_eq!(class(one(e_range("k", src_range(0, u64::MAX, 1))).get("k", Request::Range(0, 0))), "payload");
}

#[test]
fn payload_error_total_size_overflow() {
    let ar = one(e_concat("k", vec![src_range(0, 0, u64::MAX), src_range(0, 0, 1)]));
    assert_eq!(class(ar.get("k", Request::Range(0, 0))), "payload");
}

// ---------------------------------------------------------------- request errors

#[test]
fn request_error_start_after_end_even_when_missing() {
    let ar = one(e_stored("k", b"x"));
    assert_eq!(class(ar.get("k", Request::Range(2, 1))), "request");
    assert_eq!(class(ar.get("nope", Request::Range(2, 1))), "request");
    assert_eq!(class(ar.get("__vz__/x", Request::Range(2, 1))), "request");
}

#[test]
fn hidden_keys_are_missing_except_raw() {
    let ar = one(e_stored("__vz__/h", b"x"));
    assert_eq!(ar.classify("__vz__/h").unwrap(), Kind::Missing);
    assert_eq!(ar.get("__vz__/h", Request::Whole).unwrap(), None);
    assert_eq!(ar.raw("__vz__/h").unwrap().unwrap(), b"x");
    assert!(ar.list("").unwrap().is_empty());
}

// ---------------------------------------------------------------- resolution

fn file_archive(dir: &std::path::Path, sources: Vec<vzip::proto::Source>, entries: &[E]) -> vzip::Archive {
    let p = write_tmp(dir, "a.vzip", &build(entries, &table(sources)));
    vzip::Archive::open(&p).unwrap()
}

#[test]
fn file_urls_resolve_relative_to_archive() {
    let d = tmpdir();
    std::fs::create_dir_all(d.join("sub dir")).unwrap();
    write_tmp(&d, "sub dir/é.bin", b"0123456789");
    let ar = file_archive(&d, vec![url_src("sub%20dir/%C3%A9.bin")], &[e_range("k", src_range(0, 2, 3))]);
    assert_eq!(ar.get("k", Request::Whole).unwrap().unwrap(), b"234");
    // Fragment-only reference: the archive itself.
    let ar = file_archive(&d, vec![url_src("#x")], &[e_range("k", src_range(0, 0, 2))]);
    assert_eq!(ar.get("k", Request::Whole).unwrap().unwrap(), b"PK");
    // Absolute file URL.
    let abs = format!("file://{}", d.join("sub dir").join("x.bin").display()).replace(' ', "%20");
    write_tmp(&d, "sub dir/x.bin", b"abc");
    let ar = file_archive(&d, vec![url_src(&abs)], &[e_range("k", src_range(0, 1, 2))]);
    assert_eq!(ar.get("k", Request::Whole).unwrap().unwrap(), b"bc");
}

#[test]
fn pins_on_file_sources() {
    let d = tmpdir();
    write_tmp(&d, "s.bin", b"0123456789");
    let mtime = {
        use std::os::unix::fs::MetadataExt;
        std::fs::metadata(d.join("s.bin")).unwrap().mtime()
    };
    let mut s = url_src("s.bin");
    s.size = Some(10);
    s.modified_not_after = Some(mtime);
    let ar = file_archive(&d, vec![s], &[e_range("k", src_range(0, 0, 2))]);
    assert_eq!(ar.get("k", Request::Whole).unwrap().unwrap(), b"01");
}

fn res_err(sources: Vec<vzip::proto::Source>, entries: &[E]) {
    let d = tmpdir();
    write_tmp(&d, "s.bin", b"0123456789");
    let ar = file_archive(&d, sources, entries);
    assert_eq!(class(ar.get("k", Request::Whole)), "resolution");
}

#[test]
fn resolution_error_missing_file() {
    res_err(vec![url_src("nope.bin")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_file_too_short() {
    res_err(vec![url_src("s.bin")], &[e_range("k", src_range(0, 5, 6))]);
}

#[test]
fn resolution_error_size_pin() {
    let mut s = url_src("s.bin");
    s.size = Some(11);
    res_err(vec![s], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_mtime_pin() {
    let mut s = url_src("s.bin");
    s.modified_not_after = Some(1000);
    res_err(vec![s], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_etag_pin_on_file() {
    let mut s = url_src("s.bin");
    s.etag = Some("\"x\"".into());
    res_err(vec![s], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_invalid_url_syntax() {
    res_err(vec![url_src("s .bin")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_unsupported_scheme() {
    res_err(vec![url_src("ftp://x/y")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_encoded_dot_segment() {
    res_err(vec![url_src("x/%2E%2E/s.bin")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_file_query() {
    res_err(vec![url_src("s.bin?")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_file_remote_host() {
    res_err(vec![url_src("file://example.com/s.bin")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_key_source_missing() {
    res_err(vec![key_src("nope")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_key_source_is_reference() {
    res_err(vec![key_src("r")], &[e_range("r", lit(b"ab")), e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_key_source_is_format_entry() {
    res_err(vec![key_src("__vz__/sources")], &[e_range("k", src_range(0, 0, 1))]);
}

#[test]
fn resolution_error_data_source_too_short() {
    res_err(vec![data_src(b"ab")], &[e_range("k", src_range(0, 1, 2))]);
}

#[test]
fn unresolved_ranges_cause_no_error() {
    let d = tmpdir();
    let ar = file_archive(
        &d,
        vec![url_src("nope.bin"), data_src(b"abc")],
        &[e_concat("k", vec![src_range(1, 0, 3), src_range(0, 0, 0), src_range(0, 0, 5)])],
    );
    assert_eq!(ar.get("k", Request::Range(0, 3)).unwrap().unwrap(), b"abc");
    assert_eq!(class(ar.get("k", Request::Range(0, 4))), "resolution");
}
