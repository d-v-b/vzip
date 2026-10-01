//! Reader behaviour on invalid archives and failing references (spec §8.4).

mod common;

use common::*;
use vzip::proto::{self, CdIndex, Pinned, Range, Source, SourceKind};
use vzip::{Archive, ErrorClass, Request};

fn src(kind: SourceKind) -> Source {
    Source { kind: Some(kind), size: None, etag: None, modified_not_after: None }
}

fn open_bytes(bytes: &[u8]) -> Result<Archive, vzip::VzError> {
    let d = tmpdir();
    let p = d.join("a.vzip");
    std::fs::write(&p, bytes).unwrap();
    Archive::open_path(&p)
}

fn open_raw(spec: &RawSpec) -> Result<Archive, vzip::VzError> {
    open_bytes(&build_raw(spec))
}

fn archive_err(spec: &RawSpec) {
    match open_raw(spec) {
        Err(e) => assert_eq!(e.class, ErrorClass::Archive, "{e}"),
        Ok(_) => panic!("open should fail"),
    }
}

fn range_payload(r: Range) -> Vec<u8> {
    proto::encode_range(&r)
}

fn base_entries() -> Vec<RawEntry> {
    vec![RawEntry::stored("ok", b"fine")]
}

fn with_index(entries: Vec<RawEntry>, f: impl Fn(&[(Vec<u8>, u64, u64, u64)]) -> CdIndex + 'static) -> RawSpec {
    let mut s = RawSpec::new(entries, &[]);
    s.index = Some(Box::new(move |info| proto::encode_cd_index(&f(info))));
    s
}

fn get_err(a: &Archive, key: &str) -> ErrorClass {
    a.get(key, Request::Whole).expect_err("get should fail").class
}

// ---------------------------------------------------------------- archive

#[test]
fn archive_not_a_zip() {
    assert_eq!(open_bytes(b"hello world, this is definitely not a zip archive at all").unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_empty_file() {
    assert_eq!(open_bytes(b"").unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_missing_file() {
    assert_eq!(Archive::open_path(std::path::Path::new("/nonexistent/x.vzip")).unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_wrong_magic() {
    let mut s = RawSpec::new(base_entries(), &[]);
    s.comment_override = Some(b"zzzz/0".iter().copied().chain([0u8; 16]).collect());
    archive_err(&s);
}

#[test]
fn archive_unsupported_version() {
    let mut s = RawSpec::new(base_entries(), &[]);
    s.comment_override = Some(b"vzip/1".iter().copied().chain([0u8; 16]).collect());
    archive_err(&s);
}

#[test]
fn archive_wrong_comment_length() {
    let mut s = RawSpec::new(base_entries(), &[]);
    s.comment_override = Some(b"vzip/0".iter().copied().chain([0u8; 17]).collect());
    archive_err(&s);
}

#[test]
fn archive_sources_outside_file() {
    let mut s = RawSpec::new(base_entries(), &[]);
    let mut c = b"vzip/0".to_vec();
    c.extend_from_slice(&1_000_000u64.to_le_bytes());
    c.extend_from_slice(&10u64.to_le_bytes());
    s.comment_override = Some(c);
    archive_err(&s);
}

#[test]
fn archive_sources_trailing_bytes() {
    let mut s = RawSpec::new(base_entries(), &[]);
    s.sources_body.push(0);
    archive_err(&s);
}

#[test]
fn archive_sources_truncated() {
    let mut s = RawSpec::new(base_entries(), &[src(SourceKind::Data(vec![7; 100]))]);
    s.sources_body.pop();
    archive_err(&s);
}

#[test]
fn archive_sources_not_deflate() {
    let mut s = RawSpec::new(base_entries(), &[]);
    s.sources_body = vec![0xff, 0xff, 0xff];
    archive_err(&s);
}

#[test]
fn archive_sources_malformed() {
    let mut s = RawSpec::new(base_entries(), &[]);
    s.sources_body = deflate(&[0x0b]); // field 1, wire type 3
    archive_err(&s);
}

#[test]
fn archive_source_without_kind() {
    let s = RawSpec::new(base_entries(), &[Source { kind: None, size: None, etag: None, modified_not_after: None }]);
    archive_err(&s);
}

#[test]
fn archive_source_empty_url() {
    archive_err(&RawSpec::new(base_entries(), &[src(SourceKind::Url(String::new()))]));
}

#[test]
fn archive_pin_on_key_source() {
    let mut s = src(SourceKind::Key("ok".into()));
    s.size = Some(4);
    archive_err(&RawSpec::new(base_entries(), &[s]));
}

#[test]
fn archive_pin_on_data_source() {
    let mut s = src(SourceKind::Data(vec![]));
    s.modified_not_after = Some(0);
    archive_err(&RawSpec::new(base_entries(), &[s]));
}

#[test]
fn archive_weak_etag() {
    let mut s = src(SourceKind::Url("x".into()));
    s.etag = Some("W/\"a\"".into());
    archive_err(&RawSpec::new(base_entries(), &[s]));
}

#[test]
fn archive_invalid_url_is_not_checked_at_open() {
    let a = open_raw(&RawSpec::new(
        vec![RawEntry::reference("r", 0x7A76, &range_payload(Range { source: 0, offset: 0, length: 1, data: None }))],
        &[src(SourceKind::Url("a b".into()))],
    ))
    .unwrap();
    assert_eq!(get_err(&a, "r"), ErrorClass::Resolution);
}

fn patch_eocd(bytes: &mut [u8], off: usize, val: &[u8]) {
    let eocd = bytes.len() - 44;
    bytes[eocd + off..eocd + off + val.len()].copy_from_slice(val);
}

#[test]
fn archive_cd_outside_file() {
    let mut b = build_raw(&RawSpec::new(base_entries(), &[]));
    patch_eocd(&mut b, 16, &0x00ff_0000u32.to_le_bytes());
    assert_eq!(open_bytes(&b).unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_cd_bad_record_signature() {
    let mut b = build_raw(&RawSpec::new(base_entries(), &[]));
    let eocd = b.len() - 44;
    let cd_off = u32::from_le_bytes(b[eocd + 16..eocd + 20].try_into().unwrap()) as usize;
    b[cd_off] = 0;
    assert_eq!(open_bytes(&b).unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_cd_size_too_small() {
    let mut b = build_raw(&RawSpec::new(base_entries(), &[]));
    let eocd = b.len() - 44;
    let sz = u32::from_le_bytes(b[eocd + 12..eocd + 16].try_into().unwrap());
    patch_eocd(&mut b, 12, &(sz - 1).to_le_bytes());
    assert_eq!(open_bytes(&b).unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_index_entry_without_index() {
    archive_err(&RawSpec::new(vec![RawEntry::deflated("__vz__/index", b"")], &[]));
}

#[test]
fn archive_zip64_without_locator() {
    let mut b = build_raw(&RawSpec::new(base_entries(), &[]));
    patch_eocd(&mut b, 8, &[0xff, 0xff]);
    assert_eq!(open_bytes(&b).unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_zip64_bad_record() {
    // Writer output with 0xFFFF entries has zip64 records; corrupt the size field.
    let mut entries = Vec::new();
    for i in 0..0xFFFFu32 {
        entries.push(format!(r#"{{"key": "k{i}", "bytes": ""}}"#));
    }
    let d = format!(r#"{{"entries": [{}]}}"#, entries.join(","));
    let mut b = vzip::write_archive(&vzip::desc::parse_description(&d).unwrap()).unwrap();
    assert!(open_bytes(&b).is_ok());
    let loc = b.len() - 44 - 20;
    let z = u64::from_le_bytes(b[loc + 8..loc + 16].try_into().unwrap()) as usize;
    b[z + 4] = 45;
    assert_eq!(open_bytes(&b).unwrap_err().class, ErrorClass::Archive);
}

#[test]
fn archive_index_does_not_decode() {
    let mut s = RawSpec::new(base_entries(), &[]);
    s.index = Some(Box::new(|_| vec![0x0f]));
    archive_err(&s);
}

#[test]
fn archive_index_zero_length_page() {
    archive_err(&with_index(base_entries(), |info| {
        let mut i = one_page_per_record(info);
        i.pages.push(proto::Page { first_key: "zz".into(), offset: info[0].2, length: 0 });
        i
    }));
}

#[test]
fn archive_index_page_outside_cd() {
    archive_err(&with_index(base_entries(), |info| {
        let mut i = one_page_per_record(info);
        i.pages[0].length += 10_000;
        i
    }));
}

#[test]
fn archive_index_pages_not_contiguous() {
    archive_err(&with_index(vec![RawEntry::stored("a", b""), RawEntry::stored("b", b"")], |info| {
        let mut i = one_page_per_record(info);
        i.pages[1].offset += 1;
        i.pages[1].length -= 1;
        i
    }));
}

#[test]
fn archive_index_first_page_not_at_zero() {
    archive_err(&with_index(vec![RawEntry::stored("a", b""), RawEntry::stored("b", b"")], |info| {
        let mut i = one_page_per_record(info);
        i.pages.remove(0);
        i
    }));
}

#[test]
fn archive_index_keys_not_increasing() {
    archive_err(&with_index(vec![RawEntry::stored("a", b""), RawEntry::stored("b", b"")], |info| {
        let mut i = one_page_per_record(info);
        i.pages[1].first_key = "a".into();
        i
    }));
}

#[test]
fn archive_index_empty_first_key() {
    archive_err(&with_index(base_entries(), |info| {
        let mut i = one_page_per_record(info);
        i.pages[0].first_key = String::new();
        i
    }));
}

fn pin(key: &str, info: &[(Vec<u8>, u64, u64, u64)]) -> Pinned {
    Pinned { key: key.into(), data_offset: info[0].3, size: 4, csize: 4, method: 0 }
}

#[test]
fn archive_index_empty_pinned_key() {
    archive_err(&with_index(base_entries(), |info| CdIndex { pages: one_page_per_record(info).pages, pinned: vec![pin("", info)] }));
}

#[test]
fn archive_index_pinned_twice() {
    archive_err(&with_index(base_entries(), |info| CdIndex { pages: one_page_per_record(info).pages, pinned: vec![pin("ok", info), pin("ok", info)] }));
}

#[test]
fn archive_index_pinned_format_entry() {
    archive_err(&with_index(base_entries(), |info| CdIndex { pages: one_page_per_record(info).pages, pinned: vec![pin("__vz__/sources", info)] }));
}

#[test]
fn archive_index_pinned_bad_method() {
    archive_err(&with_index(base_entries(), |info| {
        let mut p = pin("ok", info);
        p.method = 9;
        CdIndex { pages: one_page_per_record(info).pages, pinned: vec![p] }
    }));
}

#[test]
fn archive_index_pinned_outside_file() {
    archive_err(&with_index(base_entries(), |info| {
        let mut p = pin("ok", info);
        p.csize = 1 << 40;
        CdIndex { pages: one_page_per_record(info).pages, pinned: vec![p] }
    }));
}

#[test]
fn archive_index_valid_baseline() {
    let a = open_raw(&with_index(base_entries(), |info| CdIndex { pages: one_page_per_record(info).pages, pinned: vec![pin("ok", info)] })).unwrap();
    assert_eq!(a.get("ok", Request::Whole).unwrap().unwrap(), b"fine");
    assert_eq!(a.list("").unwrap(), vec!["ok"]);
}

// ---------------------------------------------------------------- entry

fn entry_err(e: RawEntry) {
    let a = open_raw(&RawSpec::new(vec![e, RawEntry::stored("other", b"x")], &[])).unwrap();
    assert_eq!(a.classify("bad").unwrap_err().class, ErrorClass::Entry);
    assert_eq!(get_err(&a, "bad"), ErrorClass::Entry);
    assert_eq!(a.raw("bad").unwrap_err().class, ErrorClass::Entry);
    // Listed anyway; other keys unaffected.
    assert_eq!(a.list("").unwrap(), vec!["bad", "other"]);
    assert_eq!(a.get("other", Request::Whole).unwrap().unwrap(), b"x");
}

#[test]
fn entry_unparseable_extra() {
    let mut e = RawEntry::stored("bad", b"");
    e.extra = vec![1, 2, 3];
    entry_err(e);
}

#[test]
fn entry_two_reference_blocks() {
    let mut e = RawEntry::stored("bad", b"");
    e.extra = [block(0x7A76, &[]), block(0x7A77, &[])].concat();
    entry_err(e);
}

#[test]
fn entry_bad_method() {
    let mut e = RawEntry::stored("bad", b"");
    e.method = 12;
    entry_err(e);
}

#[test]
fn entry_encrypted() {
    let mut e = RawEntry::stored("bad", b"");
    e.flags |= 1;
    entry_err(e);
}

#[test]
fn entry_reference_method_8() {
    let mut e = RawEntry::reference("bad", 0x7A77, &[]);
    e.method = 8;
    e.body = deflate(b"");
    entry_err(e);
}

#[test]
fn entry_zip64_offset_without_block() {
    let mut e = RawEntry::stored("bad", b"");
    e.lho_override = Some(0xFFFF_FFFF);
    entry_err(e);
}

#[test]
fn entry_zip64_block_too_short() {
    let mut e = RawEntry::stored("bad", b"");
    e.lho_override = Some(0xFFFF_FFFF);
    e.extra = block(0x0001, &[0; 4]);
    entry_err(e);
}

#[test]
fn entry_two_zip64_blocks() {
    let mut e = RawEntry::stored("bad", b"");
    e.lho_override = Some(0xFFFF_FFFF);
    e.extra = [block(0x0001, &[0; 8]), block(0x0001, &[0; 8])].concat();
    entry_err(e);
}

#[test]
fn entry_zip64_offset_valid_block() {
    // A valid ZIP64 block holding offset 0 (the first entry) works.
    let mut e = RawEntry::stored("first", b"data");
    e.lho_override = Some(0xFFFF_FFFF);
    e.extra = block(0x0001, &0u64.to_le_bytes());
    let a = open_raw(&RawSpec::new(vec![e], &[])).unwrap();
    assert_eq!(a.get("first", Request::Whole).unwrap().unwrap(), b"data");
}

#[test]
fn entry_unparseable_page() {
    // Page 2 of 2 is shortened so it no longer holds a whole record... instead
    // corrupt a record signature inside a page by pointing the page at it with
    // a misaligned offset.
    let s = with_index(vec![RawEntry::stored("a", b"1"), RawEntry::stored("b", b"2"), RawEntry::stored("c", b"3")], |info| {
        let mut i = one_page_per_record(info);
        // Merge b and c into one page but shift the boundary by 1 byte.
        i.pages[1].length += 1;
        i.pages[2].offset += 1;
        i.pages[2].length -= 1;
        i
    });
    let a = open_raw(&s).unwrap();
    assert_eq!(a.get("a", Request::Whole).unwrap().unwrap(), b"1");
    assert_eq!(a.classify("b").unwrap_err().class, ErrorClass::Entry);
    assert_eq!(a.classify("c").unwrap_err().class, ErrorClass::Entry);
    assert_eq!(a.list("").unwrap_err().class, ErrorClass::Entry);
    assert_eq!(a.list("a").unwrap(), vec!["a"]);
}

// ---------------------------------------------------------------- body

fn body_err(e: RawEntry) {
    let a = open_raw(&RawSpec::new(vec![e], &[src(SourceKind::Key("bad".into()))])).unwrap();
    assert_eq!(a.classify("bad").unwrap(), vzip::Kind::Bytes);
    assert_eq!(a.get("bad", Request::Range(0, 1)).unwrap_err().class, ErrorClass::Body);
    assert_eq!(a.raw("bad").unwrap_err().class, ErrorClass::Body);
}

#[test]
fn body_deflate_trailing_bytes() {
    let mut e = RawEntry::deflated("bad", b"hello hello hello");
    e.body.push(0);
    body_err(e);
}

#[test]
fn body_deflate_truncated() {
    let mut e = RawEntry::deflated("bad", b"hello hello hello");
    e.body.pop();
    body_err(e);
}

#[test]
fn body_deflate_wrong_size() {
    let mut e = RawEntry::deflated("bad", b"hello hello hello");
    e.usize += 1;
    body_err(e);
}

#[test]
fn body_stored_sizes_differ() {
    let mut e = RawEntry::stored("bad", b"hello");
    e.usize = 4;
    body_err(e);
}

#[test]
fn body_outside_file() {
    let mut e = RawEntry::stored("bad", b"hello");
    e.csize = Some(1 << 30);
    e.usize = 1 << 30;
    body_err(e);
}

#[test]
fn body_error_via_key_source_is_resolution() {
    let mut e = RawEntry::deflated("__vz__/h", b"hello hello hello");
    e.body.push(0);
    let r = RawEntry::reference("r", 0x7A76, &range_payload(Range { source: 0, offset: 0, length: 1, data: None }));
    let a = open_raw(&RawSpec::new(vec![e, r], &[src(SourceKind::Key("__vz__/h".into()))])).unwrap();
    assert_eq!(get_err(&a, "r"), ErrorClass::Resolution);
}

// ---------------------------------------------------------------- payload

fn payload_err(id: u16, payload: &[u8]) {
    let a = open_raw(&RawSpec::new(vec![RawEntry::reference("bad", id, payload)], &[src(SourceKind::Data(vec![1, 2, 3]))])).unwrap();
    assert_eq!(a.classify("bad").unwrap(), vzip::Kind::Reference);
    assert_eq!(get_err(&a, "bad"), ErrorClass::Payload);
    assert_eq!(a.get("bad", Request::Range(0, 0)).unwrap_err().class, ErrorClass::Payload);
    assert_eq!(a.raw("bad").unwrap().unwrap(), b"");
}

#[test]
fn payload_wire_type_7() {
    payload_err(0x7A76, &[0x0f]);
}

#[test]
fn payload_group_in_unknown_field() {
    payload_err(0x7A76, &[0x33, 0x34]);
}

#[test]
fn payload_truncated() {
    payload_err(0x7A76, &[0x18]);
}

#[test]
fn payload_wrong_wire_type() {
    payload_err(0x7A76, &[0x19, 0, 0, 0, 0, 0, 0, 0, 0]);
}

#[test]
fn payload_uint32_overflow() {
    payload_err(0x7A76, &[0x08, 0x80, 0x80, 0x80, 0x80, 0x10]);
}

#[test]
fn payload_source_out_of_bounds_outside_window() {
    let parts = [
        Range { data: Some(vec![1]), ..Default::default() },
        Range { source: 5, offset: 0, length: 0, data: None },
    ];
    payload_err(0x7A77, &proto::encode_concat(&parts));
}

#[test]
fn payload_literal_with_offset() {
    payload_err(0x7A76, &range_payload(Range { source: 0, offset: 1, length: 0, data: Some(vec![]) }));
}

#[test]
fn payload_offset_plus_length_overflow() {
    payload_err(0x7A76, &range_payload(Range { source: 0, offset: u64::MAX, length: 1, data: None }));
}

#[test]
fn payload_total_size_overflow() {
    let parts = [
        Range { source: 0, offset: 0, length: u64::MAX, data: None },
        Range { data: Some(vec![1]), ..Default::default() },
    ];
    payload_err(0x7A77, &proto::encode_concat(&parts));
}

#[test]
fn payload_invalid_utf8_irrelevant_but_bad_concat_part() {
    // Concat field 1 with VARINT wire type.
    payload_err(0x7A77, &[0x08, 0x01]);
}

// ---------------------------------------------------------------- resolution

struct Fx {
    dir: std::path::PathBuf,
    a: Archive,
}

/// An archive with one reference "r" = range(source 0, offset, length) and
/// the given source.
fn one_ref(source: Source, offset: u64, length: u64, extra_entries: Vec<RawEntry>) -> Fx {
    let dir = tmpdir();
    std::fs::write(dir.join("data.bin"), b"0123456789").unwrap();
    let mut entries = vec![RawEntry::reference("r", 0x7A76, &range_payload(Range { source: 0, offset, length, data: None }))];
    entries.extend(extra_entries);
    let b = build_raw(&RawSpec::new(entries, &[source]));
    std::fs::write(dir.join("a.vzip"), b).unwrap();
    let a = Archive::open_path(&dir.join("a.vzip")).unwrap();
    Fx { dir, a }
}

fn url(u: &str) -> Source {
    src(SourceKind::Url(u.into()))
}

#[test]
fn resolution_ok_cases() {
    let fx = one_ref(url("data.bin"), 2, 3, vec![]);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap().unwrap(), b"234");
    let abs = format!("file://{}", fx.dir.join("data.bin").display());
    let fx = one_ref(url(&abs), 0, 10, vec![]);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap().unwrap(), b"0123456789");
    let mut s = url("./sub/../data.bin");
    s.size = Some(10);
    s.modified_not_after = Some(i64::MAX);
    let fx = one_ref(s, 9, 1, vec![]);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap().unwrap(), b"9");
    // Fragment-only reference resolves to the archive itself.
    let fx = one_ref(url("#x"), 0, 4, vec![]);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap().unwrap(), b"PK\x03\x04");
    // Hidden key source.
    let fx = one_ref(src(SourceKind::Key("__vz__/h".into())), 1, 2, vec![RawEntry::deflated("__vz__/h", b"abcd")]);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap().unwrap(), b"bc");
    // Window not touching the range: no resolution.
    let fx = one_ref(url("missing.bin"), 0, 0, vec![]);
    assert_eq!(fx.a.get("r", Request::Whole).unwrap().unwrap(), b"");
}

fn res_err(fx: Fx) {
    assert_eq!(get_err(&fx.a, "r"), ErrorClass::Resolution);
    assert_eq!(fx.a.get("r", Request::Range(0, 0)).unwrap().unwrap(), b"");
}

#[test]
fn resolution_missing_file() {
    res_err(one_ref(url("missing.bin"), 0, 1, vec![]));
}

#[test]
fn resolution_source_too_short() {
    res_err(one_ref(url("data.bin"), 5, 6, vec![]));
}

#[test]
fn resolution_size_pin_fails() {
    let mut s = url("data.bin");
    s.size = Some(11);
    res_err(one_ref(s, 0, 1, vec![]));
}

#[test]
fn resolution_etag_pin_on_file() {
    let mut s = url("data.bin");
    s.etag = Some("\"x\"".into());
    res_err(one_ref(s, 0, 1, vec![]));
}

#[test]
fn resolution_mtime_pin_fails() {
    let mut s = url("data.bin");
    s.modified_not_after = Some(1000);
    res_err(one_ref(s, 0, 1, vec![]));
}

#[test]
fn resolution_mtime_pin_boundary() {
    let fx = one_ref(url("data.bin"), 0, 1, vec![]);
    let f = std::fs::File::options().write(true).open(fx.dir.join("data.bin")).unwrap();
    let t = std::time::UNIX_EPOCH + std::time::Duration::from_millis(1_000_999);
    f.set_modified(t).unwrap();
    for (pin, ok) in [(1000, true), (999, false)] {
        let mut s = url("data.bin");
        s.modified_not_after = Some(pin);
        let b = build_raw(&RawSpec::new(vec![RawEntry::reference("r", 0x7A76, &range_payload(Range { source: 0, offset: 0, length: 1, data: None }))], &[s]));
        std::fs::write(fx.dir.join("b.vzip"), b).unwrap();
        let a = Archive::open_path(&fx.dir.join("b.vzip")).unwrap();
        assert_eq!(a.get("r", Request::Whole).is_ok(), ok, "pin {pin}");
    }
}

#[test]
fn resolution_encoded_dot_segments() {
    res_err(one_ref(url("sub/%2E%2E/data.bin"), 0, 1, vec![]));
}

#[test]
fn resolution_encoded_slash() {
    res_err(one_ref(url("x%2Fdata.bin"), 0, 1, vec![]));
}

#[test]
fn resolution_file_query() {
    res_err(one_ref(url("data.bin?"), 0, 1, vec![]));
}

#[test]
fn resolution_file_remote_host() {
    res_err(one_ref(url("file://example.com/data.bin"), 0, 1, vec![]));
}

#[test]
fn resolution_unsupported_scheme() {
    res_err(one_ref(url("ftp://example.com/data.bin"), 0, 1, vec![]));
}

#[test]
fn resolution_invalid_url() {
    res_err(one_ref(url("data bin"), 0, 1, vec![]));
}

#[test]
fn resolution_key_source_missing() {
    res_err(one_ref(src(SourceKind::Key("nope".into())), 0, 1, vec![]));
}

#[test]
fn resolution_key_source_reference() {
    res_err(one_ref(src(SourceKind::Key("r".into())), 0, 1, vec![]));
}

#[test]
fn resolution_key_source_format_entry() {
    res_err(one_ref(src(SourceKind::Key("__vz__/sources".into())), 0, 1, vec![]));
}

#[test]
fn resolution_key_source_entry_error() {
    let mut e = RawEntry::stored("bad", b"abc");
    e.method = 3;
    res_err(one_ref(src(SourceKind::Key("bad".into())), 0, 1, vec![e]));
}

#[test]
fn resolution_data_source_too_short() {
    res_err(one_ref(src(SourceKind::Data(vec![1, 2])), 1, 2, vec![]));
}
