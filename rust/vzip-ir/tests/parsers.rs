//! The TIFF and CZI parsers: every fixture they accept is a valid IR that rebuilds
//! its source; one test per rejected fixture (each the role the schema names); and
//! the round-3 probes built here (IFD loops, huge counts, too many IFDs).

use std::path::Path;
use vzip_ir::check;
use vzip_ir::czi::Czi;
use vzip_ir::parse_bytes;
use vzip_ir::tiff::Tiff;

fn fixtures() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures")
}

fn rebuilt(ir: &vzip_ir::ir::Ir, data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for (o, n) in check::leaves(ir).unwrap() {
        out.extend_from_slice(&data[o as usize..(o + n) as usize]);
    }
    out
}

fn accepted<P: vzip_ir::Parser>(dir: &str) -> usize {
    let mut n = 0;
    for e in std::fs::read_dir(fixtures().join(dir)).unwrap() {
        let path = e.unwrap().path();
        let data = std::fs::read(&path).unwrap();
        if let Ok((ir, _)) = parse_bytes::<P>(&data) {
            check::check(&ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
            assert!(rebuilt(&ir, &data) == data, "{path:?} does not rebuild");
            n += 1;
        }
    }
    n
}

#[test]
fn accepted_fixtures_are_valid_irs_that_rebuild() {
    assert_eq!(accepted::<Tiff>("tiff"), 33);
    assert_eq!(accepted::<Czi>("czi"), 32);
}

macro_rules! reject {
    ($name:ident, $p:ident, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            let data = std::fs::read(fixtures().join($file)).unwrap();
            match parse_bytes::<$p>(&data) {
                Err(m) => assert!(m.starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}

reject!(tiff_edge_reject_big_offset, Tiff, "tiff/edge_reject_big_offset.tif", "a value of tag 324 is more than 2^53 - 1");
reject!(tiff_edge_reject_bigtiff_reserved, Tiff, "tiff/edge_reject_bigtiff_reserved.tif", "invalid BigTIFF header");
reject!(tiff_edge_reject_candidate_planar3, Tiff, "tiff/edge_reject_candidate_planar3.tif", "PlanarConfiguration 3");
reject!(tiff_edge_reject_cycle, Tiff, "tiff/edge_reject_cycle.tif", "IFD offset 4136 read twice");
reject!(tiff_edge_reject_dimension_order, Tiff, "tiff/edge_reject_dimension_order.tif", "DimensionOrder 'XYZZT'");
reject!(tiff_edge_reject_fill_order, Tiff, "tiff/edge_reject_fill_order.tif", "the image at 4136 has FillOrder 2");
reject!(tiff_edge_reject_first_z, Tiff, "tiff/edge_reject_first_z.tif", "TiffData starts outside the planes");
reject!(tiff_edge_reject_jpeg_16bit, Tiff, "tiff/edge_reject_jpeg_16bit.tif", "unsupported JPEG: 16-bit, 3 samples, planar 1, p");
reject!(tiff_edge_reject_jpeg_no_photometric, Tiff, "tiff/edge_reject_jpeg_no_photometric.tif", "unsupported JPEG: 8-bit, 3 samples, planar 1, ph");
reject!(tiff_edge_reject_jpeg_planar, Tiff, "tiff/edge_reject_jpeg_planar.tif", "unsupported JPEG: 8-bit, 3 samples, planar 2, ph");
reject!(tiff_edge_reject_jpeg_short_tile, Tiff, "tiff/edge_reject_jpeg_short_tile.tif", "JPEG tile 0 of the IFD at 16 is too short");
reject!(tiff_edge_reject_jpeg_tables, Tiff, "tiff/edge_reject_jpeg_tables.tif", "the IFD at 834 has malformed JPEGTables");
reject!(tiff_edge_reject_multifile, Tiff, "tiff/edge_reject_multifile.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_multifile_uuid, Tiff, "tiff/edge_reject_multifile_uuid.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_nbsp_size, Tiff, "tiff/edge_reject_nbsp_size.tif", "SizeZ=' 2' is not an integer");
reject!(tiff_edge_reject_no_ifds, Tiff, "tiff/edge_reject_no_ifds.tif", "no images");
reject!(tiff_edge_reject_no_width, Tiff, "tiff/edge_reject_no_width.tif", "IFD at 4136 has no tag 256");
reject!(tiff_edge_reject_planar3, Tiff, "tiff/edge_reject_planar3.tif", "PlanarConfiguration 3");
reject!(tiff_edge_reject_plane_limit, Tiff, "tiff/edge_reject_plane_limit.tif", "100001 planes is more than 100000");
reject!(tiff_edge_reject_plane_size, Tiff, "tiff/edge_reject_plane_size.tif", "planes of one pyramid level differ in size, tili");
reject!(tiff_edge_reject_sample_format, Tiff, "tiff/edge_reject_sample_format.tif", "SampleFormat values are missing or differ");
reject!(tiff_edge_reject_scalar_count0, Tiff, "tiff/edge_reject_scalar_count0.tif", "tag 259 has no value");
reject!(tiff_edge_reject_shared_subifd, Tiff, "tiff/edge_reject_shared_subifd.tif", "IFD offset 1032 read twice");
reject!(tiff_edge_reject_signed_width, Tiff, "tiff/edge_reject_signed_width.tif", "tag 256 has field type 8");
reject!(tiff_edge_reject_size_sign, Tiff, "tiff/edge_reject_size_sign.tif", "SizeZ='+2' is not an integer");
reject!(tiff_edge_reject_size_zero, Tiff, "tiff/edge_reject_size_zero.tif", "SizeZ='0' is less than 1");
reject!(tiff_edge_reject_subifd_count, Tiff, "tiff/edge_reject_subifd_count.tif", "the IFD at 9832 has fewer SubIFDs than IFD 0");
reject!(tiff_edge_reject_tiffdata_steps, Tiff, "tiff/edge_reject_tiffdata_steps.tif", "the TiffData elements cover more than 1008 plane");
reject!(tiff_edge_reject_tile_count, Tiff, "tiff/edge_reject_tile_count.tif", "IFD at 32 has 3 tiles, expected 4");
reject!(tiff_edge_reject_tile_outside, Tiff, "tiff/edge_reject_tile_outside.tif", "tile 0 of the IFD at 40 is outside the file");
reject!(tiff_edge_reject_unmapped, Tiff, "tiff/edge_reject_unmapped.tif", "OME-XML planes do not match the TIFF's images");
reject!(tiff_edge_reject_unused_ifd_type, Tiff, "tiff/edge_reject_unused_ifd_type.tif", "tag 259 has field type 99");
reject!(tiff_edge_reject_unused_value_outside, Tiff, "tiff/edge_reject_unused_value_outside.tif", "the value of tag 324 is outside the file");
reject!(tiff_edge_reject_uuid_self_closing, Tiff, "tiff/edge_reject_uuid_self_closing.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_ycbcr_default, Tiff, "tiff/edge_reject_ycbcr_default.tif", "the image at 12334 is YCbCr with subsampling [2,");
reject!(tiff_edge_reject_ycbcr_subsampled, Tiff, "tiff/edge_reject_ycbcr_subsampled.tif", "the image at 12334 is YCbCr with subsampling [2,");
reject!(tiff_edge_reject_zero_tile, Tiff, "tiff/edge_reject_zero_tile.tif", "the image at 8 has an empty tile size");
reject!(tiff_unsupported_lzw, Tiff, "tiff/unsupported_lzw.tif", "unsupported compression 5");
reject!(tiff_unsupported_predictor, Tiff, "tiff/unsupported_predictor.tif", "unsupported predictor 2");
reject!(tiff_unsupported_strips, Tiff, "tiff/unsupported_strips.tif", "only tiled TIFFs are supported; the image at 8 i");
reject!(czi_reject_attachment_file_part, Czi, "czi/czi_reject_attachment_file_part.czi", "attachment entry 0 is in FilePart 1");
reject!(czi_reject_attachment_id, Czi, "czi/czi_reject_attachment_id.czi", "attachment 0 at 1440 is not a ZISRAWATTACH segme");
reject!(czi_reject_attachment_outside_file, Czi, "czi/czi_reject_attachment_outside_file.czi", "attachment 0's data is outside the file");
reject!(czi_reject_attdir_count_over_limit, Czi, "czi/czi_reject_attdir_count_over_limit.czi", "the attachment directory's EntryCount 65537 is n");
reject!(czi_reject_attdir_id, Czi, "czi/czi_reject_attdir_id.czi", "the attachment directory at 1760 is not a ZISRAW");
reject!(czi_reject_compression_chunked, Czi, "czi/czi_reject_compression_chunked.czi", "directory entry 0 has an unsupported Compression");
reject!(czi_reject_compression_lzw, Czi, "czi/czi_reject_compression_lzw.czi", "directory entry 0 has an unsupported Compression");
reject!(czi_reject_compression_raw_camera, Czi, "czi/czi_reject_compression_raw_camera.czi", "directory entry 0 has an unsupported Compression");
reject!(czi_reject_de_schema, Czi, "czi/czi_reject_de_schema.czi", "directory entry 0 has schema DE, not DV");
reject!(czi_reject_dimension_count_13, Czi, "czi/czi_reject_dimension_count_13.czi", "directory entry 0 has DimensionCount 13, not 2 t");
reject!(czi_reject_dimension_letter, Czi, "czi/czi_reject_dimension_letter.czi", "directory entry 0 has the dimension 'Q'");
reject!(czi_reject_dimension_lowercase, Czi, "czi/czi_reject_dimension_lowercase.czi", "directory entry 0 has the dimension '[122, 0, 0,");
reject!(czi_reject_dimension_padding, Czi, "czi/czi_reject_dimension_padding.czi", "directory entry 0 has the dimension '[90, 0, 0,");
reject!(czi_reject_directory_id, Czi, "czi/czi_reject_directory_id.czi", "the subblock directory at 2176 is not a ZISRAWDI");
reject!(czi_reject_directory_overrun, Czi, "czi/czi_reject_directory_overrun.czi", "directory entry 1 does not end within the direct");
reject!(czi_reject_directory_used, Czi, "czi/czi_reject_directory_used.czi", "directory entry 0 does not end within the direct");
reject!(czi_reject_duplicate_dimension, Czi, "czi/czi_reject_duplicate_dimension.czi", "directory entry 0 has the dimension C twice");
reject!(czi_reject_entry_count_negative, Czi, "czi/czi_reject_entry_count_negative.czi", "the directory's EntryCount -1 is not from 0 to 2");
reject!(czi_reject_entry_count_over_limit, Czi, "czi/czi_reject_entry_count_over_limit.czi", "the directory's EntryCount 2097153 is not from 0");
reject!(czi_reject_entry_file_part, Czi, "czi/czi_reject_entry_file_part.czi", "directory entry 0 is in FilePart 1");
reject!(czi_reject_extent, Czi, "czi/czi_reject_extent.czi", "an array dimension of 4294967296, more than 2^31");
reject!(czi_reject_file_part, Czi, "czi/czi_reject_file_part.czi", "FilePart 1: a CZI split over several files is no");
reject!(czi_reject_jpeg_gray16, Czi, "czi/czi_reject_jpeg_gray16.czi", "directory entry 0: Compression 1 with PixelType");
reject!(czi_reject_major_2, Czi, "czi/czi_reject_major_2.czi", "CZI file version 2.0 (Major MUST be 1)");
reject!(czi_reject_metadata_id, Czi, "czi/czi_reject_metadata_id.czi", "the metadata segment at 864 is not a ZISRAWMETAD");
reject!(czi_reject_metadata_negative, Czi, "czi/czi_reject_metadata_negative.czi", "the metadata segment has a negative XmlSize or A");
reject!(czi_reject_metadata_outside_file, Czi, "czi/czi_reject_metadata_outside_file.czi", "the metadata segment's parts is outside the file");
reject!(czi_reject_missing_x, Czi, "czi/czi_reject_missing_x.czi", "directory entry 0 lacks the X or Y dimension");
reject!(czi_reject_negative_sizes, Czi, "czi/czi_reject_negative_sizes.czi", "subblock 0 has a negative MetadataSize, Attachme");
reject!(czi_reject_no_directory, Czi, "czi/czi_reject_no_directory.czi", "the subblock directory at 0 is not a ZISRAWDIREC");
reject!(czi_reject_pixel_type_unknown, Czi, "czi/czi_reject_pixel_type_unknown.czi", "directory entry 0 has an unsupported PixelType 5");
reject!(czi_reject_plane_size_not_1, Czi, "czi/czi_reject_plane_size_not_1.czi", "directory entry 0's C Size and StoredSize must b");
reject!(czi_reject_segment_negative, Czi, "czi/czi_reject_segment_negative.czi", "the subblock directory's AllocatedSize -1 is not");
reject!(czi_reject_subblock_copy_count, Czi, "czi/czi_reject_subblock_copy_count.czi", "subblock 0's copy of its entry has DimensionCoun");
reject!(czi_reject_subblock_copy_schema, Czi, "czi/czi_reject_subblock_copy_schema.czi", "subblock 0's copy of its entry has schema DE, no");
reject!(czi_reject_subblock_id, Czi, "czi/czi_reject_subblock_id.czi", "subblock 0 at 544 is not a ZISRAWSUBBLOCK segmen");
reject!(czi_reject_subblock_outside_file, Czi, "czi/czi_reject_subblock_outside_file.czi", "subblock 0's parts is outside the file");
reject!(czi_reject_unknown_schema, Czi, "czi/czi_reject_unknown_schema.czi", "directory entry 0 has schema XX, not DV");
reject!(czi_reject_zero_size, Czi, "czi/czi_reject_zero_size.czi", "directory entry 0's X Size and StoredSize must b");

// ---- the round-3 TIFF probes, built here

/// A little-endian classic TIFF: `blobs` after the header, then the IFDs (sorted
/// entries (tag, type, count, value)), each IFD's next given by `next` (an index).
fn tiff(ifds: &[Vec<(u16, u16, u32, u32)>], blobs: &[u8], next: &[Option<usize>]) -> Vec<u8> {
    let start = 8 + blobs.len();
    let mut offs = Vec::new();
    let mut at = start;
    for e in ifds {
        offs.push(at as u32);
        at += 2 + 12 * e.len() + 4;
    }
    let mut out = b"II*\0".to_vec();
    out.extend_from_slice(&offs[0].to_le_bytes());
    out.extend_from_slice(blobs);
    for (k, e) in ifds.iter().enumerate() {
        let mut e = e.clone();
        e.sort();
        out.extend_from_slice(&(e.len() as u16).to_le_bytes());
        for (t, ty, c, v) in e {
            out.extend_from_slice(&t.to_le_bytes());
            out.extend_from_slice(&ty.to_le_bytes());
            out.extend_from_slice(&c.to_le_bytes());
            out.extend_from_slice(&v.to_le_bytes());
        }
        out.extend_from_slice(&next[k].map(|j| offs[j]).unwrap_or(0).to_le_bytes());
    }
    out
}

/// A tiled 32 x 32 8-bit image's entries and its four tiles and tables.
fn image() -> (Vec<(u16, u16, u32, u32)>, Vec<u8>) {
    let mut blobs = vec![0u8; 1024];
    let at = 8 + blobs.len() as u32;
    for k in 0..4u32 {
        blobs.extend_from_slice(&(8 + 256 * k).to_le_bytes());
    }
    for _ in 0..4 {
        blobs.extend_from_slice(&256u32.to_le_bytes());
    }
    let e = vec![
        (256, 4, 1, 32), (257, 4, 1, 32), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1), (277, 3, 1, 1),
        (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, 4, at), (325, 4, 4, at + 16),
    ];
    (e, blobs)
}

fn rejects(data: &[u8], msg: &str) {
    match parse_bytes::<Tiff>(data) {
        Err(m) => assert!(m.contains(msg), "{m}"),
        Ok(_) => panic!("accepted"),
    }
}

#[test]
fn tiff_probes_parse() {
    let (e, blobs) = image();
    // one IFD, then IFD 0 with an EXIF pointer to a second IFD that points back to both: aliases
    let first = 8 + blobs.len() as u32;
    let second = first + 2 + 12 * 11 + 4;
    let mut with_exif = e.clone();
    with_exif.push((34665, 4, 1, second));
    let cases = [
        tiff(&[e.clone()], &blobs, &[None]),
        tiff(&[with_exif, vec![(34665, 4, 1, first), (34853, 4, 1, second)]], &blobs, &[None, None]),
        // a tag the profile does not read, with a count whose value would be 16 GiB: not read
        tiff(&[[e.clone(), vec![(65000, 7, u32::MAX, 64)]].concat()], &blobs, &[None]),
    ];
    for data in cases {
        let (ir, facts) = parse_bytes::<Tiff>(&data).unwrap();
        check::check(&ir).unwrap();
        assert_eq!(rebuilt(&ir, &data), data);
        assert_eq!(facts["levels"][0]["w"], 32);
        assert!(facts["requested"].as_u64().unwrap() < 4 * data.len() as u64);
    }
}

#[test]
fn a_chain_that_loops_is_rejected() {
    let (e, blobs) = image();
    rejects(&tiff(&[e.clone(), e], &blobs, &[Some(1), Some(0)]), "read twice");
}

#[test]
fn an_ifd_that_is_its_own_next_is_rejected() {
    let (e, blobs) = image();
    rejects(&tiff(&[e], &blobs, &[Some(0)]), "read twice");
}

#[test]
fn a_subifd_that_is_its_parent_is_rejected() {
    let (mut e, blobs) = image();
    e.push((330, 4, 1, 8 + blobs.len() as u32));
    rejects(&tiff(&[e], &blobs, &[None]), "read twice");
}

#[test]
fn a_bigtiff_entry_count_past_the_file_is_rejected() {
    let mut data = b"II+\0".to_vec();
    data.extend_from_slice(&8u16.to_le_bytes());
    data.extend_from_slice(&0u16.to_le_bytes());
    data.extend_from_slice(&16u64.to_le_bytes());
    data.extend_from_slice(&(1u64 << 40).to_le_bytes());
    data.extend_from_slice(&[0; 64]);
    rejects(&data, "not within the file");
}

#[test]
fn a_tile_table_count_past_the_file_is_rejected() {
    let (mut e, blobs) = image();
    e[8] = (324, 4, u32::MAX, 64);
    rejects(&tiff(&[e], &blobs, &[None]), "the value of tag 324 is outside the file");
}

#[test]
fn more_than_100000_ifds_are_rejected() {
    let ifds = vec![vec![(256u16, 4u16, 1u32, 1u32)]; 100001];
    let next: Vec<Option<usize>> = (0..ifds.len()).map(|k| (k + 1 < ifds.len()).then_some(k + 1)).collect();
    rejects(&tiff(&ifds, &[], &next), "too many IFDs");
}

#[test]
fn a_segment_id_with_bytes_after_its_nul_is_not_the_id() {
    // spec/virtualize/czi.md §2.1: the 16 bytes of the id, NUL-padded; a byte after the NUL makes another id
    let mut data = std::fs::read(fixtures().join("czi/czi_gray8_single.czi")).unwrap();
    let at = (0..data.len() - 16).find(|&k| data[k..k + 15] == *b"ZISRAWSUBBLOCK\0").unwrap();
    data[at + 15] = 0xDF;
    match parse_bytes::<Czi>(&data) {
        Err(m) => assert!(m.contains("is not a ZISRAWSUBBLOCK segment"), "{m}"),
        Ok(_) => panic!("accepted"),
    }
}
