//! A virtualizer's output as an archive, as Python writes it: the output's source table and
//! references (python/src/refkv/virtualize/common.py `Output.to_store`, and
//! python/src/refkv/ir/store.py `StoreIrOutput.to_store` for a store), laid out by
//! `reference_store` (the references first, in order of their keys, then the bytes entries, a
//! late one, read when the archive opens, the others compressed, 64 KiB pages), and saved by
//! `ReferenceStore.save` (python/src/refkv/refstore.py): its sources checked against the reader
//! policy first, then written by the archive writer (rust/refkv-vzip).

use crate::policy::Policy;
use crate::Fail;
use refkv_ir::out::{Out, Part};
use refkv_vzip::{Options, Range, Source, Writer, INDEX_KEY, RESERVED_PREFIX, SOURCES_KEY};
use std::fs::File;
use std::collections::HashMap;
use std::io::BufWriter;

/// The page index of a virtualized archive (spec/bindings/vzip.md §12
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>).
const PAGE_SIZE: u64 = 1 << 16;

/// An archive to write: the source table, the references (key, ranges) and the bytes entries
/// (key, bytes, late), each in the order written.
pub struct Archive {
    pub sources: Vec<Source>,
    pub refs: Vec<(String, Vec<Range>)>,
    pub entries: Vec<(String, Vec<u8>, bool)>,
}

/// The bytes entries of an output, in order of their keys: a document is late (read when the
/// archive opens) when its key ends in `zarr.json` and is not under a lazy prefix.
fn entries(out: &mut Out) -> Vec<(String, Vec<u8>, bool)> {
    let mut e: Vec<(String, Vec<u8>)> = std::mem::take(&mut out.entries);
    e.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    e.into_iter()
        .map(|(k, v)| {
            let late = k.ends_with("zarr.json") && !out.lazy.iter().any(|p| k.starts_with(p.as_str()));
            (k, v, late)
        })
        .collect()
}

/// The archive of a file's output: source 0 the file, pinning its size (and its ETag, when given),
/// then the data sources.
pub fn file_archive(mut out: Out, size: u64, etag: Option<String>) -> Result<Archive, Fail> {
    let mut sources = vec![Source { size: Some(size), etag, ..Source::url(out.url.as_str()) }];
    sources.extend(out.data.iter().map(|d| Source::data(d.as_slice())));
    let mut refs = std::mem::take(&mut out.refs);
    refs.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let refs = refs
        .into_iter()
        .map(|(k, parts)| {
            let ranges = parts
                .into_iter()
                .map(|p| match p {
                    Part::Src(a, n) => Ok(Range::of(0, a, n)),
                    Part::Data(s, a, n) => Ok(Range::of(s, a, n)),
                    Part::Lit(b) => Ok(Range::literal(b)),
                    Part::Obj(..) => Err(Fail::Other(format!("{k}: a reference to a store's object in a file's output"))),
                })
                .collect::<Result<Vec<_>, _>>()?;
            Ok((k, ranges))
        })
        .collect::<Result<Vec<_>, Fail>>()?;
    Ok(Archive { sources, refs, entries: entries(&mut out) })
}

/// The archive of a store's output, and the object key of each url source: one url source per
/// object some reference uses, its size pinned, in order of first use (the references in order of
/// their keys, each one's ranges in order), then the data sources, also in order of first use
/// (spec/virtualize.md §1.4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#14-store-inputs>).
pub fn store_archive(mut out: Out, sizes: &HashMap<String, u64>) -> Result<(Archive, Vec<String>), Fail> {
    let mut refs = std::mem::take(&mut out.refs);
    refs.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let mut objects: Vec<String> = Vec::new();
    let mut object_at: HashMap<String, u32> = HashMap::new();
    for (_, parts) in &refs {
        for p in parts {
            if let Part::Obj(k, ..) = p
                && !object_at.contains_key(k) {
                    object_at.insert(k.clone(), objects.len() as u32);
                    objects.push(k.clone());
                }
        }
    }
    let mut data: Vec<u32> = Vec::new(); // the core's data source numbers, in order of first use
    let mut data_at: HashMap<u32, u32> = HashMap::new();
    let mut numbered = Vec::with_capacity(refs.len());
    for (k, parts) in refs {
        let mut ranges = Vec::with_capacity(parts.len());
        for p in parts {
            ranges.push(match p {
                Part::Lit(b) => Range::literal(b),
                Part::Obj(o, a, n) => Range::of(object_at[&o], a, n),
                Part::Data(s, a, n) => {
                    let i = *data_at.entry(s).or_insert_with(|| {
                        data.push(s);
                        (objects.len() + data.len() - 1) as u32
                    });
                    Range::of(i, a, n)
                }
                Part::Src(..) => return Err(Fail::Other(format!("{k}: a reference to source 0 in a store's output"))),
            });
        }
        numbered.push((k, ranges));
    }
    let mut sources: Vec<Source> =
        objects.iter().map(|k| Source { size: sizes.get(k).copied(), ..Source::url(crate::store::object_url(&out.url, k)) }).collect();
    for s in &data {
        let d = out.data.get(*s as usize - 1).ok_or_else(|| Fail::Other(format!("no data source {s}")))?;
        sources.push(Source::data(d.as_slice()));
    }
    Ok((Archive { sources, refs: numbered, entries: entries(&mut out) }, objects))
}

/// Records on each range of a url source the CRC-32C of its bytes (spec/refkv.md §4.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), which `read(source, offset,
/// length)` reads, at most `workers` reads at a time.
pub fn checksums(a: &mut Archive, workers: usize, read: impl Fn(usize, u64, u64) -> Result<Vec<u8>, Fail> + Sync) -> Result<(), Fail> {
    let mut jobs = Vec::new();
    for (i, (_, ranges)) in a.refs.iter().enumerate() {
        for (j, r) in ranges.iter().enumerate() {
            if r.data.is_none() && r.crc32c.is_none() && a.sources[r.source as usize].url_str().is_some() {
                jobs.push((i, j, r.source as usize, r.offset, r.length));
            }
        }
    }
    let crcs = crate::run::map(&jobs, workers, |&(_, _, s, o, n)| read(s, o, n).map(|b| crc32c::crc32c(&b)));
    for ((i, j, ..), c) in jobs.into_iter().zip(crcs) {
        a.refs[i].1[j].crc32c = Some(c?);
    }
    Ok(())
}

/// The scheme and authority an absolute URL starts with (RFC 3986 §3), if it has them.
fn origin(url: &str) -> Option<&str> {
    let b = url.as_bytes();
    if !b.first()?.is_ascii_alphabetic() {
        return None;
    }
    let i = url.find("://")?;
    if !url[..i].bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'.' | b'-')) {
        return None;
    }
    let end = url[i + 3..].find(['/', '?', '#']).map_or(url.len(), |j| i + 3 + j);
    Some(&url[..end])
}

/// Writes the archive at `path` (`ReferenceStore.save`), after checking every url source, resolved
/// against the archive's own URI, against the reader policy: a source a reader under it could not
/// read refuses the whole archive, and nothing is written.
/// What `convert` writes: a vzip archive, a standalone phkv store, or a vzip archive with a phkv
/// container in `__vz__/phkv` (spec/bindings/phkv.md §16.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#162-profiles>).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Container {
    #[default]
    Vzip,
    Phkv,
    Hybrid,
}

impl Container {
    pub fn parse(s: &str) -> Option<Container> {
        match s {
            "vzip" => Some(Container::Vzip),
            "phkv" => Some(Container::Phkv),
            "hybrid" => Some(Container::Hybrid),
            _ => None,
        }
    }
}

pub fn save(a: &Archive, path: &str, policy: &Policy, container: Container) -> Result<(), Fail> {
    let base = refkv_vzip::uri::file_uri(std::path::Path::new(path)).map_err(Fail::Other)?;
    let mut by_origin: HashMap<String, Option<String>> = HashMap::new();
    let mut refused = Vec::new();
    for s in &a.sources {
        let Some(url) = s.url_str() else { continue };
        // the policy decides a valid absolute URL by its scheme and host: one check per origin
        let o = origin(url);
        let error = match o.and_then(|o| by_origin.get(o)) {
            Some(e) if refkv_vzip::uri::is_uri_reference(url) => e.clone(),
            _ => {
                
                match refkv_vzip::uri::resolve(&base, url) {
                    Err(e) => Some(e),
                    Ok(resolved) => {
                        let e = policy.check(&base, &resolved).err();
                        if let Some(o) = o {
                            by_origin.insert(o.to_string(), e.clone());
                        }
                        e
                    }
                }
            }
        };
        if let Some(e) = error {
            refused.push(e);
        }
    }
    if let Some(first) = refused.first() {
        return Err(Fail::Refused(format!(
            "not saving {path}: the reader policy given refuses {} of its {} sources, so a reader could not read them. The first: {first}",
            refused.len(),
            a.sources.len()
        )));
    }
    for (k, _) in &a.refs {
        check_key(k)?;
    }
    for (k, ..) in &a.entries {
        check_key(k)?;
    }
    let f = std::fs::File::create(path).map_err(|e| Fail::Other(format!("{path}: {e}")))?;
    let out = BufWriter::with_capacity(1 << 20, f);
    let zip = Options { mirror_refs: true, page_size: Some(PAGE_SIZE) };
    let written = match container {
        Container::Vzip => write(a, Writer::new(out, zip)),
        Container::Hybrid => {
            let w = refkv_phkv::HybridWriter::new(out, zip, refkv_phkv::Options::default()).map_err(|e| Fail::Other(e.to_string()))?;
            write(a, w)
        }
        Container::Phkv => write(a, refkv_phkv::Writer::new(out, refkv_phkv::Options::default())),
    };
    if let Err(e) = written {
        let _ = std::fs::remove_file(path); // no partial archive
        return Err(e);
    }
    Ok(())
}

fn check_key(k: &str) -> Result<(), Fail> {
    if k.is_empty() || k == SOURCES_KEY || k == INDEX_KEY {
        return Err(Fail::Other(format!("{k:?} is not a key a store may hold")));
    }
    Ok(())
}

/// What the archive is written to: the vzip writer, or one of phkv's.
trait Sink: Sized {
    fn source(&mut self, s: Source) -> Result<u32, String>;
    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String>;
    /// A bytes entry, compressed unless `late` (a small document a reader fetches when opening).
    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String>;
    fn add_hidden(&mut self, name: &str, data: &[u8], late: bool) -> Result<(), String>;
    fn finish(self) -> Result<BufWriter<File>, String>;
}

impl Sink for Writer<BufWriter<File>> {
    fn source(&mut self, s: Source) -> Result<u32, String> {
        Writer::source(self, s).map_err(|e| e.to_string())
    }

    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String> {
        Writer::add_ranges(self, key, ranges).map_err(|e| e.to_string())
    }

    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String> {
        Writer::add_bytes(self, key, data, !late, late).map_err(|e| e.to_string())
    }

    fn add_hidden(&mut self, name: &str, data: &[u8], late: bool) -> Result<(), String> {
        Writer::add_hidden(self, name, data, !late, late).map(|_| ()).map_err(|e| e.to_string())
    }

    fn finish(self) -> Result<BufWriter<File>, String> {
        self.close().map_err(|e| e.to_string())
    }
}

impl Sink for refkv_phkv::HybridWriter<BufWriter<File>> {
    fn source(&mut self, s: Source) -> Result<u32, String> {
        refkv_phkv::HybridWriter::source(self, s).map_err(|e| e.to_string())
    }

    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String> {
        refkv_phkv::HybridWriter::add_ranges(self, key, ranges).map_err(|e| e.to_string())
    }

    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String> {
        refkv_phkv::HybridWriter::add_bytes(self, key, data, !late, late).map_err(|e| e.to_string())
    }

    fn add_hidden(&mut self, name: &str, data: &[u8], late: bool) -> Result<(), String> {
        refkv_phkv::HybridWriter::add_hidden(self, name, data, !late, late).map(|_| ()).map_err(|e| e.to_string())
    }

    fn finish(self) -> Result<BufWriter<File>, String> {
        self.close().map_err(|e| e.to_string())
    }
}

/// A standalone phkv store: values Zstandard-compressed, but the small documents a reader fetches
/// when opening, which are stored as they are, inline in their records when short.
impl Sink for refkv_phkv::Writer<BufWriter<File>> {
    fn source(&mut self, s: Source) -> Result<u32, String> {
        refkv_phkv::Writer::source(self, s).map_err(|e| e.to_string())
    }

    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String> {
        refkv_phkv::Writer::add_ranges(self, key, &ranges).map_err(|e| e.to_string())
    }

    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String> {
        let codec = if late { refkv_phkv::Codec::None } else { refkv_phkv::Codec::Zstd };
        refkv_phkv::Writer::add_bytes_with(self, key, data, codec).map_err(|e| e.to_string())
    }

    fn add_hidden(&mut self, name: &str, _: &[u8], _: bool) -> Result<(), String> {
        Err(format!("{RESERVED_PREFIX}{name}: a standalone phkv store has no hidden entries"))
    }

    fn finish(self) -> Result<BufWriter<File>, String> {
        self.close().map_err(|e| e.to_string())
    }
}

fn write(a: &Archive, mut w: impl Sink) -> Result<(), Fail> {
    let invalid = Fail::Other;
    let mut keys = std::collections::HashSet::new();
    for k in a.refs.iter().map(|r| &r.0).chain(a.entries.iter().map(|e| &e.0)) {
        if !keys.insert(k.as_str()) {
            return Err(Fail::Other(format!("duplicate key {k:?}")));
        }
    }
    // equal sources share an index
    let index = a.sources.iter().map(|s| w.source(s.clone())).collect::<Result<Vec<u32>, _>>().map_err(invalid)?;
    for (k, ranges) in &a.refs {
        let ranges = ranges
            .iter()
            .map(|r| {
                if r.data.is_some() {
                    return Ok(r.clone());
                }
                match index.get(r.source as usize) {
                    Some(&i) => Ok(Range { source: i, ..r.clone() }),
                    None => Err(Fail::Other(format!("{k:?}: a range uses source {}; the table has {}", r.source, index.len()))),
                }
            })
            .collect::<Result<Vec<_>, _>>()?;
        w.add_ranges(k, ranges).map_err(invalid)?;
    }
    for (k, v, late) in &a.entries {
        if let Some(name) = k.strip_prefix(RESERVED_PREFIX) {
            w.add_hidden(name, v, *late).map_err(invalid)?;
        } else {
            w.add_bytes(k, v, *late).map_err(invalid)?;
        }
    }
    let out = w.finish().map_err(invalid)?;
    out.into_inner().map_err(|e| Fail::Other(e.to_string()))?.sync_all().map_err(|e| Fail::Other(e.to_string()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins() {
        assert_eq!(origin("http://h:1/a?b"), Some("http://h:1"));
        assert_eq!(origin("https://h"), Some("https://h"));
        assert_eq!(origin("file:///x"), Some("file://"));
        assert_eq!(origin("a/b"), None);
        assert_eq!(origin("1a://h/"), None);
    }

    #[test]
    fn store_numbering() {
        let mut out = Out::new("http://h/s/");
        out.refs("b", vec![Part::Obj("o2".into(), 0, 4), Part::Data(1, 0, 2)]);
        out.refs("a", vec![Part::Obj("o1".into(), 1, 2), Part::Lit(vec![7]), Part::Data(2, 0, 1)]);
        out.refs("c", vec![Part::Obj("o1".into(), 0, 3)]);
        out.shared(b"xy");
        out.shared(b"z");
        out.json("zarr.json", &serde_json::json!({}));
        out.json("refkv_source/zarr.json", &serde_json::json!({}));
        out.bytes("c/0", vec![1]);
        let sizes = HashMap::from([("o1".to_string(), 3), ("o2".to_string(), 4)]);
        let (a, objects) = store_archive(out, &sizes).unwrap();
        assert_eq!(objects, vec!["o1", "o2"]);
        assert_eq!(
            a.sources,
            vec![
                Source { size: Some(3), ..Source::url("http://h/s/o1") },
                Source { size: Some(4), ..Source::url("http://h/s/o2") },
                Source::data(b"z".as_slice()),
                Source::data(b"xy".as_slice()),
            ]
        );
        assert_eq!(a.refs[0], ("a".into(), vec![Range::of(0, 1, 2), Range::literal(vec![7]), Range::of(2, 0, 1)]));
        assert_eq!(a.refs[1], ("b".into(), vec![Range::of(1, 0, 4), Range::of(3, 0, 2)]));
        let late: Vec<(&str, bool)> = a.entries.iter().map(|(k, _, l)| (k.as_str(), *l)).collect();
        assert_eq!(late, vec![("c/0", false), ("refkv_source/zarr.json", false), ("zarr.json", true)]);
    }

    #[test]
    fn refuses_source_0_in_a_store_output() {
        let mut out = Out::new("http://h/s/");
        out.refs("a", vec![Part::Src(0, 1)]);
        assert!(store_archive(out, &HashMap::new()).is_err());
    }

    #[test]
    fn refuses_an_object_in_a_file_output() {
        let mut out = Out::new("f.tif");
        out.refs("a", vec![Part::Obj("o".into(), 0, 1)]);
        assert!(file_archive(out, 1, None).is_err());
    }

    #[test]
    fn refuses_a_private_source_without_writing() {
        let dir = std::env::temp_dir().join(format!("refkv-cli-save-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("a.vzip").to_string_lossy().into_owned();
        let a = Archive { sources: vec![Source::url("http://127.0.0.1/x")], refs: vec![], entries: vec![] };
        assert!(matches!(save(&a, &path, &Policy::default(), Container::Vzip), Err(Fail::Refused(_))));
        assert!(!std::path::Path::new(&path).exists());
        save(&a, &path, &Policy { allow_private_hosts: true }, Container::Vzip).unwrap();
        assert!(std::path::Path::new(&path).exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn refuses_an_invalid_source_url() {
        let a = Archive { sources: vec![Source::url("a b.tif")], refs: vec![], entries: vec![] };
        assert!(matches!(save(&a, "never.vzip", &Policy::default(), Container::Vzip), Err(Fail::Refused(_))));
    }

    #[test]
    fn refuses_a_duplicate_key() {
        let dir = std::env::temp_dir().join(format!("refkv-cli-dup-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("a.vzip").to_string_lossy().into_owned();
        let a = Archive { sources: vec![], refs: vec![("k".into(), vec![Range::literal(vec![1])])], entries: vec![("k".into(), vec![], false)] };
        assert!(matches!(save(&a, &path, &Policy::default(), Container::Vzip), Err(Fail::Other(_))));
        assert!(!std::path::Path::new(&path).exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
