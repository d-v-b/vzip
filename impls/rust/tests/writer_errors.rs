//! One test per writer rejection (spec §9.1 and HARNESS.md).

use vzip::desc::parse_description;
use vzip::write_archive;

fn rejected(d: &str) -> bool {
    match parse_description(d) {
        Err(_) => true,
        Ok(spec) => write_archive(&spec).is_err(),
    }
}

macro_rules! reject {
    ($($name:ident: $d:expr;)*) => {$(
        #[test]
        fn $name() {
            assert!(rejected($d), "description should be rejected: {}", $d);
        }
    )*};
}

#[test]
fn accepted_baseline() {
    for d in [
        r#"{}"#,
        r#"{"page_size": null, "mirror": false, "unknown": null}"#,
        r#"{"sources": [{"url": "http://x/y?q#f", "size": 0, "etag": "\"\"", "modified_not_after": -5}],
            "entries": [{"key": "a", "ranges": [{"source": 0, "length": 3}], "compress": false}]}"#,
        r#"{"page_size": 1, "entries": [{"key": "__vz__/h", "bytes": "00", "pinned": true}]}"#,
        r#"{"sources": [{"key": "__vz__/h"}], "entries": [{"key": "__vz__/h", "bytes": ""}, {"key": "r", "ranges": [{}]}]}"#,
        r#"{"entries": [{"key": "r", "ranges": [{"data": ""}, {"data": "ab"}]}]}"#,
    ] {
        assert!(!rejected(d), "{d}");
    }
}

reject! {
    empty_key: r#"{"entries": [{"key": "", "bytes": ""}]}"#;
    duplicate_key: r#"{"entries": [{"key": "a", "bytes": ""}, {"key": "a", "bytes": "00"}]}"#;
    format_key_sources: r#"{"entries": [{"key": "__vz__/sources", "bytes": ""}]}"#;
    format_key_index: r#"{"entries": [{"key": "__vz__/index", "bytes": ""}]}"#;
    lone_surrogate_key: r#"{"entries": [{"key": "\ud800", "bytes": ""}]}"#;
    key_too_long: &format!(r#"{{"entries": [{{"key": "{}", "bytes": ""}}]}}"#, "a".repeat(65536));
    source_index_no_sources: r#"{"entries": [{"key": "a", "ranges": [{}]}]}"#;
    source_index_out_of_bounds: r#"{"sources": [{"data": ""}], "entries": [{"key": "a", "ranges": [{"source": 1}]}]}"#;
    empty_url: r#"{"sources": [{"url": ""}]}"#;
    bad_url_space: r#"{"sources": [{"url": "a b"}]}"#;
    bad_url_non_ascii: r#"{"sources": [{"url": "é.bin"}]}"#;
    bad_url_colon_first_segment: r#"{"sources": [{"url": "1a:b"}]}"#;
    key_source_absent: r#"{"sources": [{"key": "nope"}]}"#;
    key_source_reference: r#"{"sources": [{"key": "r"}], "entries": [{"key": "r", "ranges": []}]}"#;
    key_source_format_entry: r#"{"sources": [{"key": "__vz__/sources"}]}"#;
    pin_on_key_source: r#"{"sources": [{"key": "a", "size": 1}], "entries": [{"key": "a", "bytes": "00"}]}"#;
    pin_on_data_source: r#"{"sources": [{"data": "00", "etag": "\"x\""}]}"#;
    weak_etag: r#"{"sources": [{"url": "a", "etag": "W/\"x\""}]}"#;
    unquoted_etag: r#"{"sources": [{"url": "a", "etag": "x"}]}"#;
    etag_with_space: r#"{"sources": [{"url": "a", "etag": "\"a b\""}]}"#;
    payload_too_large: &format!(r#"{{"entries": [{{"key": "a", "ranges": [{{"data": "{}"}}]}}]}}"#, "00".repeat(65520));
    pinned_reference: r#"{"page_size": 10, "entries": [{"key": "a", "ranges": [], "pinned": true}]}"#;
    pinned_without_index: r#"{"entries": [{"key": "a", "bytes": "", "pinned": true}]}"#;
    compress_on_reference: r#"{"entries": [{"key": "a", "ranges": [], "compress": true}]}"#;
    page_size_zero: r#"{"page_size": 0}"#;
    page_size_float: r#"{"page_size": 1.0}"#;
    page_size_string: r#"{"page_size": "1"}"#;
    mirror_null: r#"{"mirror": null}"#;
    mirror_int: r#"{"mirror": 1}"#;
    sources_null: r#"{"sources": null}"#;
    compress_null: r#"{"entries": [{"key": "a", "bytes": "", "compress": null}]}"#;
    source_two_kinds: r#"{"sources": [{"url": "a", "data": "00"}]}"#;
    source_no_kind: r#"{"sources": [{"size": 1}]}"#;
    entry_bytes_and_ranges: r#"{"entries": [{"key": "a", "bytes": "", "ranges": []}]}"#;
    entry_neither: r#"{"entries": [{"key": "a"}]}"#;
    range_mix: r#"{"sources": [{"data": "00"}], "entries": [{"key": "a", "ranges": [{"data": "00", "source": 0}]}]}"#;
    hex_uppercase: r#"{"entries": [{"key": "a", "bytes": "AB"}]}"#;
    hex_odd: r#"{"entries": [{"key": "a", "bytes": "abc"}]}"#;
    negative_offset: r#"{"sources": [{"data": ""}], "entries": [{"key": "a", "ranges": [{"offset": -1}]}]}"#;
    float_length: r#"{"sources": [{"data": ""}], "entries": [{"key": "a", "ranges": [{"length": 1.0}]}]}"#;
    negative_size_pin: r#"{"sources": [{"url": "a", "size": -1}]}"#;
    not_an_object: r#"[]"#;
}

fn spec_with(parts: Vec<vzip::proto::Range>) -> vzip::WriteSpec {
    use vzip::proto::{Source, SourceKind};
    use vzip::writer::{Value, WEntry};
    vzip::WriteSpec {
        sources: vec![Source { kind: Some(SourceKind::Data(vec![])), size: None, etag: None, modified_not_after: None }],
        entries: vec![WEntry { key: "a".into(), value: Value::Ranges(parts), pinned: false }],
        ..Default::default()
    }
}

#[test]
fn offset_plus_length_overflow() {
    let r = vzip::proto::Range { source: 0, offset: u64::MAX, length: 1, data: None };
    assert!(write_archive(&spec_with(vec![r])).is_err());
}

#[test]
fn total_size_overflow() {
    let r = vzip::proto::Range { source: 0, offset: 0, length: u64::MAX, data: None };
    let lit = vzip::proto::Range { data: Some(vec![1]), ..Default::default() };
    assert!(write_archive(&spec_with(vec![r, lit])).is_err());
}
