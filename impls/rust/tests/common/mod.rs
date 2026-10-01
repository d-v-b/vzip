#![allow(dead_code)]
//! Low-level archive builder for crafting valid and invalid archives.

use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use vzip::proto::{self, Source, SourceKind, SourceTable};

pub fn deflate(data: &[u8]) -> Vec<u8> {
    let mut e = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

#[derive(Clone)]
pub struct E {
    pub name: Vec<u8>,
    pub method: u16,
    pub flags: u16,
    pub body: Vec<u8>,
    pub usize_: u32,
    pub csize: Option<u32>,
    pub extra: Vec<u8>,
    pub lho: Option<u32>,
}

pub fn e_stored(name: &str, data: &[u8]) -> E {
    E {
        name: name.as_bytes().to_vec(),
        method: 0,
        flags: 0x800,
        body: data.to_vec(),
        usize_: data.len() as u32,
        csize: None,
        extra: vec![],
        lho: None,
    }
}

pub fn e_deflate(name: &str, data: &[u8]) -> E {
    E {
        method: 8,
        body: deflate(data),
        usize_: data.len() as u32,
        ..e_stored(name, b"")
    }
}

pub fn block(id: u16, data: &[u8]) -> Vec<u8> {
    let mut v = id.to_le_bytes().to_vec();
    v.extend_from_slice(&(data.len() as u16).to_le_bytes());
    v.extend_from_slice(data);
    v
}

pub fn e_ref(name: &str, id: u16, payload: &[u8]) -> E {
    E { extra: block(id, payload), ..e_stored(name, payload) }
}

pub fn e_range(name: &str, r: proto::Range) -> E {
    e_ref(name, 0x7A76, &proto::encode_range(&r))
}

pub fn src_range(source: u32, offset: u64, length: u64) -> proto::Range {
    proto::Range { source, offset, length, data: None }
}

pub fn lit(d: &[u8]) -> proto::Range {
    proto::Range { data: Some(d.to_vec()), ..Default::default() }
}

pub fn e_concat(name: &str, parts: Vec<proto::Range>) -> E {
    e_ref(name, 0x7A77, &proto::encode_concat(&proto::Concat { parts }))
}

pub fn url_src(u: &str) -> Source {
    Source { kind: Some(SourceKind::Url(u.into())), ..Default::default() }
}
pub fn key_src(k: &str) -> Source {
    Source { kind: Some(SourceKind::Key(k.into())), ..Default::default() }
}
pub fn data_src(d: &[u8]) -> Source {
    Source { kind: Some(SourceKind::Data(d.to_vec())), ..Default::default() }
}

pub fn table(s: Vec<Source>) -> Vec<u8> {
    proto::encode_source_table(&SourceTable { sources: s })
}

/// Info passed to an index builder: (name, offset in CD, record length, body offset, csize, usize, method).
pub type RecInfo = (String, u64, u64, u64, u64, u64, u16);

fn local(buf: &mut Vec<u8>, e: &E) -> u64 {
    let lho = buf.len() as u64;
    buf.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
    buf.extend_from_slice(&20u16.to_le_bytes());
    buf.extend_from_slice(&e.flags.to_le_bytes());
    buf.extend_from_slice(&e.method.to_le_bytes());
    buf.extend_from_slice(&[0, 0, 0x21, 0]);
    buf.extend_from_slice(&0u32.to_le_bytes()); // crc (not checked)
    buf.extend_from_slice(&(e.csize.unwrap_or(e.body.len() as u32)).to_le_bytes());
    buf.extend_from_slice(&e.usize_.to_le_bytes());
    buf.extend_from_slice(&(e.name.len() as u16).to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&e.name);
    buf.extend_from_slice(&e.body);
    lho
}

fn record(e: &E, lho: u64) -> Vec<u8> {
    let mut r = Vec::new();
    r.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
    r.extend_from_slice(&20u16.to_le_bytes());
    r.extend_from_slice(&20u16.to_le_bytes());
    r.extend_from_slice(&e.flags.to_le_bytes());
    r.extend_from_slice(&e.method.to_le_bytes());
    r.extend_from_slice(&[0, 0, 0x21, 0]);
    r.extend_from_slice(&0u32.to_le_bytes());
    r.extend_from_slice(&(e.csize.unwrap_or(e.body.len() as u32)).to_le_bytes());
    r.extend_from_slice(&e.usize_.to_le_bytes());
    r.extend_from_slice(&(e.name.len() as u16).to_le_bytes());
    r.extend_from_slice(&(e.extra.len() as u16).to_le_bytes());
    r.extend_from_slice(&[0; 6]);
    r.extend_from_slice(&0u32.to_le_bytes());
    r.extend_from_slice(&(e.lho.unwrap_or(lho as u32)).to_le_bytes());
    r.extend_from_slice(&e.name);
    r.extend_from_slice(&e.extra);
    r
}

pub struct Built {
    pub bytes: Vec<u8>,
    pub cd_offset: u64,
    pub eocd_offset: u64,
}

/// Builds an archive. Entries' CD records appear in the given order. If
/// `index` is given, it computes the raw (un-deflated) CdIndex from the body
/// records and the archive is paged. `force_zip64` writes zip64 end records.
pub fn build_full(
    entries: &[E],
    sources_raw: &[u8],
    index: Option<&dyn Fn(&[RecInfo]) -> Vec<u8>>,
    force_zip64: bool,
) -> Built {
    let mut buf = Vec::new();
    let mut lhos = Vec::new();
    for e in entries {
        lhos.push(local(&mut buf, e));
    }
    let src_e = E { method: 8, body: deflate(sources_raw), usize_: sources_raw.len() as u32, ..e_stored("__vz__/sources", b"") };
    let src_lho = local(&mut buf, &src_e);
    let mut cd = Vec::new();
    let mut infos = Vec::new();
    for (e, &l) in entries.iter().zip(&lhos) {
        let r = record(e, l);
        infos.push((
            String::from_utf8_lossy(&e.name).into_owned(),
            cd.len() as u64,
            r.len() as u64,
            l + 30 + e.name.len() as u64,
            e.body.len() as u64,
            e.usize_ as u64,
            e.method,
        ));
        cd.extend_from_slice(&r);
    }
    let mut idx = None;
    if let Some(f) = index {
        let raw = f(&infos);
        let ie = E { method: 8, body: deflate(&raw), usize_: raw.len() as u32, ..e_stored("__vz__/index", b"") };
        let l = local(&mut buf, &ie);
        idx = Some((ie, l));
    }
    cd.extend_from_slice(&record(&src_e, src_lho));
    if let Some((ie, l)) = &idx {
        cd.extend_from_slice(&record(ie, *l));
    }
    let cd_offset = buf.len() as u64;
    buf.extend_from_slice(&cd);
    let n = (entries.len() + 1 + idx.is_some() as usize) as u64;
    if force_zip64 {
        let z = buf.len() as u64;
        buf.extend_from_slice(&0x0606_4b50u32.to_le_bytes());
        buf.extend_from_slice(&44u64.to_le_bytes());
        buf.extend_from_slice(&[45, 0, 45, 0]);
        buf.extend_from_slice(&[0; 8]);
        buf.extend_from_slice(&n.to_le_bytes());
        buf.extend_from_slice(&n.to_le_bytes());
        buf.extend_from_slice(&(cd.len() as u64).to_le_bytes());
        buf.extend_from_slice(&cd_offset.to_le_bytes());
        buf.extend_from_slice(&0x0706_4b50u32.to_le_bytes());
        buf.extend_from_slice(&0u32.to_le_bytes());
        buf.extend_from_slice(&z.to_le_bytes());
        buf.extend_from_slice(&1u32.to_le_bytes());
    }
    let mut comment = b"vzip/0".to_vec();
    comment.extend_from_slice(&(src_lho + 30 + 14).to_le_bytes());
    comment.extend_from_slice(&(src_e.body.len() as u64).to_le_bytes());
    if let Some((ie, l)) = &idx {
        comment.extend_from_slice(&(l + 30 + 12).to_le_bytes());
        comment.extend_from_slice(&(ie.body.len() as u64).to_le_bytes());
    }
    let eocd_offset = buf.len() as u64;
    buf.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    buf.extend_from_slice(&[0; 4]);
    if force_zip64 {
        buf.extend_from_slice(&[0xff; 4]);
    } else {
        buf.extend_from_slice(&(n as u16).to_le_bytes());
        buf.extend_from_slice(&(n as u16).to_le_bytes());
    }
    buf.extend_from_slice(&(cd.len() as u32).to_le_bytes());
    buf.extend_from_slice(&(cd_offset as u32).to_le_bytes());
    buf.extend_from_slice(&(comment.len() as u16).to_le_bytes());
    buf.extend_from_slice(&comment);
    Built { bytes: buf, cd_offset, eocd_offset }
}

pub fn build(entries: &[E], sources_raw: &[u8]) -> Vec<u8> {
    build_full(entries, sources_raw, None, false).bytes
}

/// One page per record, no pins.
pub fn one_page_per_record(infos: &[RecInfo]) -> Vec<u8> {
    let ix = proto::CdIndex {
        pages: infos
            .iter()
            .map(|i| proto::Page { first_key: i.0.clone(), offset: i.1, length: i.2 })
            .collect(),
        pinned: vec![],
    };
    proto::encode_cd_index(&ix)
}

static COUNTER: AtomicUsize = AtomicUsize::new(0);

pub fn tmpdir() -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let p = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!(
        "t-{}-{}-{}",
        std::process::id(),
        n,
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

pub fn write_tmp(dir: &std::path::Path, name: &str, data: &[u8]) -> PathBuf {
    let p = dir.join(name);
    std::fs::write(&p, data).unwrap();
    p
}

pub fn open_bytes(data: &[u8]) -> Result<vzip::Archive, vzip::Error> {
    let d = tmpdir();
    let p = write_tmp(&d, "a.vzip", data);
    vzip::Archive::open(&p)
}

pub fn class<T: std::fmt::Debug>(r: Result<T, vzip::Error>) -> &'static str {
    match r {
        Ok(v) => panic!("expected error, got {:?}", v),
        Err(e) => e.class(),
    }
}
