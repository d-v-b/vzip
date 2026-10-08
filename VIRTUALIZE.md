# Virtualizing image files and stores as OME-Zarr in vzip

Profiles version: 0 (**draft**) · Revision: 16

## 1. Introduction

A **virtualizer** reads the structure of an image file (TIFF, Hamamatsu NDPI,
Nikon ND2, DICOM, NIfTI or Imaris IMS) or of a chunked array store (N5 or
Zarr v2, including OME-Zarr 0.4), and writes a vzip archive ([SPEC.md](SPEC.md))
that presents the pixels as an OME-Zarr dataset, or for a store as a Zarr v3
hierarchy (for an OME-Zarr 0.4 store, an OME-Zarr 0.5 one). The
pixels stay where they are: each Zarr chunk is a reference to byte ranges of
the file, or to a whole object of the store. This document specifies, for
each supported input format (a **profile**), exactly which archive a
virtualizer produces, so that independent virtualizers produce the same
output from the same input.

This document holds what every profile shares: the input, the output and how
outputs compare (§1), and the common parts of the output (§2). Each profile
is a document of its own, numbered as a section of this one:

| § | profile | inputs |
|---|---|---|
| 3 | [TIFF](profiles/tiff.md) | TIFF and BigTIFF, including OME-TIFF and JPEG-tiled slides such as Aperio SVS |
| 4 | [NDPI](profiles/ndpi.md) | Hamamatsu NDPI slides, a TIFF variant with 64-bit offsets |
| 5 | [ND2](profiles/nd2.md) | Nikon ND2, format version 3 and later |
| 6 | [DICOM](profiles/dicom.md) | DICOM Part 10 files with native or JPEG-encapsulated pixel data, including whole-slide images |
| 7 | [NIfTI](profiles/nifti.md) | NIfTI-1 and NIfTI-2 single files (`.nii`) |
| 8 | [IMS](profiles/ims.md) | Imaris IMS files (HDF5) |
| 9 | [N5](profiles/n5.md) | N5 containers (store input), default-mode blocks |
| 10 | [Zarr v2](profiles/zarr2.md) | Zarr v2 hierarchies (store input) |
| 11 | [OME-Zarr](profiles/ome-zarr.md) | OME-Zarr 0.4 hierarchies on Zarr v2 (store input), migrated to OME-Zarr 0.5 |

§3–§8 read a single file (a **file input**); §9–§11 read a store of many
objects (a **store input**, §1.4). §12 is informative: the implementations
and how they are compared.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be
interpreted as described in RFC 2119.

### 1.1 Output and equivalence

A virtualizer's **output** is an archive description:

- a **source table**: a list of `url` sources and `data` sources (SPEC.md
  §6), and
- a set of **entries**: keys, each with either bytes or a list of ranges
  ([SPEC.md §2](SPEC.md#2-data-model), [§5](SPEC.md#5-messages)).

Two outputs are **equivalent** when they have the same source table (the
same sources in the same order: `url` sources with the same URLs, `data`
sources with identical bytes), the same set of keys, and for every key:

- **reference entries:** the same list of ranges, in order. A range is a
  source range `(i, offset, length)` of source `i`, or a **literal** range
  of given bytes ([SPEC.md §5.2](SPEC.md#52-range)), and two ranges are the same only if both are
  source ranges with equal sources, offsets and lengths, or both are literal
  ranges with identical bytes;
- **JSON documents** (keys ending in `zarr.json`): equal JSON values. Objects
  compare by key, arrays by order, and numbers as IEEE 754 binary64 values;
  whitespace and key order do not matter;
- **other bytes entries:** identical bytes.

The archive's byte layout (entry order, compression, page index) is the
writer's choice and is not part of the output. Two virtualizers conform to
the same profile revision if they produce equivalent outputs for every input
that the profile accepts, and both reject every input it rejects.

### 1.2 Input

A virtualizer is given a URL, `U`. `U` MUST be a valid absolute URI;
virtualizers do not normalize it. If `U`'s path ends in `/`, the input is a
**store input**, read as §1.4 says, and the profile is chosen there; the
rest of this section is about **file inputs**, where `U` is the URL of one
file. (An implementation MAY also accept a local file or directory, with `U`
the URL it will be served from: a directory is a store input, listed as
§1.5 says.)

For a file input, the output's source table is source 0, a `url` source
whose value is `U` as given, without pins, followed by the output's **data
sources**, if its profile defines any. A data source is a `data` source
([SPEC.md §6](SPEC.md#6-source-table)) holding a byte string that a profile names as **shared**: one
that many references would otherwise each read from the file, or each carry
as a literal, such as a JPEG header. The data sources are the distinct
shared byte strings of the output, numbered from 1 **in order of first
use**: in the order in which the profile lists the references, and within a
reference in order of its ranges, the first range that uses a byte string
not yet in the table adds it as the next source. A range that uses a shared
byte string `d` in data source `i` is `(i, 0, len(d))`, the whole source.
Every other range of a reference is a range of source 0 or a literal range.
The TIFF (§3, JPEG tiles) and NDPI (§4) profiles define shared byte strings;
the others define none, so their table is source 0 alone.

**Choosing the profile.** The file's first bytes decide, by the first row
of this table that matches. `H` is the file's first `min(552, size)` bytes;
a test that needs a byte beyond `H` does not match.

| test on `H` | |
|---|---|
| bytes 128–131 are `44 49 43 4D` (`DICM`) | DICOM profile (§6) |
| bytes 0–3 are `49 49 2A 00`, `4D 4D 00 2A`, `49 49 2B 00` or `4D 4D 00 2B` | NDPI profile (§4) if the file passes its detection test (§4), else TIFF profile (§3) |
| bytes 0–3 are `DA CE BE 0A` (the ND2 chunk magic, §5.1) | ND2 profile (§5) |
| bytes 0–7 are `89 48 44 46 0D 0A 1A 0A` (the HDF5 signature) | IMS profile (§8) |
| bytes 0–3 are the 32-bit integer 348 in either byte order, and bytes 344–347 are `6E 2B 31 00` (`n+1`) | NIfTI profile (§7), NIfTI-1 |
| bytes 0–3 are the 32-bit integer 540 in either byte order, and bytes 4–11 are `6E 2B 32 00 0D 0A 1A 0A` (`n+2`) | NIfTI profile (§7), NIfTI-2 |
| anything else, including the JPEG 2000 signature box `00 00 00 0C 6A 50 20 20 0D 0A 87 0A` that starts legacy ND2 files, and NIfTI header-and-image pairs (`ni1`, `ni2`) | rejected |

A DICOM file is read by the DICOM profile even when its 128-byte preamble
holds a TIFF header, as dual-personality files and some writers' preambles
do. An HDF5 file that is not an Imaris file is rejected by the IMS profile.

**Rejection.** A virtualizer MUST reject an input the profile does not
accept, producing no output. That includes every input that is not well
formed as this document describes it: a read outside the file, a structure
that is truncated or inconsistent, a value of the wrong type or out of range,
an offset or length above 2^53 − 1. Being unable to read the input (a
network error) is not a rejection; the virtualizer fails instead. An
implementation MAY also fail, rather than reject, when an input exceeds a
resource limit that it states (§12); a failure is not an output, so it does
not affect equivalence.

**Structure only.** The output MUST NOT depend on the file's pixel data.
(Reading blocks that happen to include pixel bytes is fine.) Coding
parameters are structure, not pixel data, and an output may copy them: the
data sources and literals of the TIFF and NDPI profiles hold JPEG markers
and tables (quantization and Huffman tables, frame and scan headers, restart
intervals), which describe how the pixels are coded but encode none of them.
Entropy-coded data, from which pixels are decoded, is only ever referenced
as ranges of source 0.

**Evaluation order.** Whether an input is rejected never depends on the
order in which a virtualizer reads or checks things: each profile lists what
is checked, and every listed check applies whether or not its value ends up
in the output.

**References stay in the file.** Every range of source 0 MUST lie within
the file (`offset + length ≤` the file's size; a range of a data source is
the whole source), and every reference entry's
payload MUST be at most 65519 bytes; otherwise the input is rejected. The
payload ([SPEC.md §4.3](SPEC.md#43-reference-entries), [§5](SPEC.md#5-messages)) of a single range is its `Range` message: for a
source range `(i, o, n)`, `0x08 varint(i)` if `i > 0`, then `0x18 varint(o)`
if `o > 0`, then `0x20 varint(n)` if `n > 0` (fields 1, 3 and 4); for a
literal range of bytes `d`, `0x2A varint(len(d)) d`. The payload of several
ranges is a `Concat` message: for each range, `0x0A varint(len(r)) r`, where
`r` is that range's `Range` message. `varint` is the protobuf base-128
encoding (1 byte below 2^7, 2 below 2^14, and so on).

### 1.3 Arithmetic

Where this document computes a number (a scale, a size), it is computed in
IEEE 754 binary64 arithmetic, in the order written, with each operation
rounded to nearest. Decimal strings are converted to binary64 by correct
rounding. Integers are exact. A number that would be infinite or NaN rejects
the input. In JSON documents, integers (shapes, counts, codec parameters) are
written as integers; a computed number such as a scale MAY be written either
way (`1` or `1.0`), since outputs compare numbers by value (§1.1).

**Whitespace** in this document means the XML whitespace characters: space,
tab, CR and LF (U+0020, U+0009, U+000D, U+000A). **Digits** are the ASCII
digits `0`–`9`.

### 1.4 Store inputs

A **store** is a set of **objects**, each a byte string named by a
**key**, as in an S3 bucket or a directory tree. A store input is given by
its **store URL** `U` (§1.2), whose path ends in `/`; it names the store
whose objects are the objects under `U`.

**The store URL.** `U` MUST have the scheme `http` or `https` (in any
case), an authority without userinfo, and no query or fragment component
(not even an empty `?` or `#`); otherwise the input is rejected. (An
implementation that reads a local directory uses the URL the directory will
be served from, which MUST satisfy the same rules.)

**The objects.** The store is listed as §1.5 says. Each listed key `K`
begins with the prefix `P` (§1.5); its **relative key** is `K` without
`P`. A listed object is **ignored** when its relative key is empty, ends in
`/`, contains an empty segment (`//`), or has a segment `.` or `..`
(segments are the parts between `/`). The store's objects are the listed
objects that are not ignored, each with its relative key and its **size**,
the listed size. From here on "key" means the relative key. A **directory**
is a key prefix that ends just before a `/`: the store's directories are
the root (the empty path) and every `d` such that some key starts with
`d/`. The path of a node of the output is a directory's path.

**Choosing the profile.** The keys at the root (keys without a `/`)
decide, by the first row that matches:

| root keys | |
|---|---|
| `.zarray` or `.zgroup` | OME-Zarr profile (§11) if the store declares OME-NGFF 0.4 (below), else Zarr v2 profile (§10) |
| `attributes.json` | N5 profile (§9) |
| anything else, including an empty store | rejected |

**Declaring OME-NGFF 0.4.** The test reads at most three documents, by
§1.6, in this order; a document that §1.6 rejects rejects the input. In
it, a **relative path** is a string of one or more segments separated by
`/`, none of them empty, `.` or `..`; the **attributes** of a path `p` are
read only if the store has both objects `p/.zgroup` and `p/.zattrs`
(`.zgroup` and `.zattrs` for the root), and are then the document
`p/.zattrs` if it is an object (otherwise there are none). A set of
attributes `A` **declares 0.4 multiscales** if its member `multiscales` is
an array with an element that is an object whose member `version` is the
string `"0.4"`.

1. If the store has an object `.zarray`, or the root has no attributes, the
   store does not declare OME-NGFF 0.4. Otherwise let `A` be the root's
   attributes.
2. The store declares OME-NGFF 0.4 if `A` declares 0.4 multiscales, or `A`'s
   member `plate` or `well` is an object whose member `version` is the
   string `"0.4"`.
3. Otherwise, if `A` has no member `bioformats2raw.layout`, it does not.
   If it has one, the **first image** `q` is found:
   - if `A` has a member `plate`: if `plate` is an object whose member
     `wells` is a nonempty array whose first element is an object whose
     member `path` is a relative path `w`, and the attributes of `w` have a
     member `well` that is an object whose member `images` is a nonempty
     array whose first element is an object whose member `path` is a
     relative path `f`, then `q` is `w/f`; otherwise there is none;
   - otherwise `q` is the first element of the member `series` of the
     attributes of `OME`, if that is a nonempty array whose first element
     is a relative path, and `0` if not.

   The store declares OME-NGFF 0.4 if there is a `q` and its attributes
   declare 0.4 multiscales.

(These are the forms OME-NGFF 0.4 gives an image, a plate, a well and a
bioformats2raw collection at the root. A 0.4 group below a root that does
not declare 0.4 is read by §10, with its attributes unchanged.)

**Reading an object.** A virtualizer reads an object whole: the bytes
`[0, size)` of the object's URL (below), with HTTP range requests, or from
the local file. A response that does not have the listed size is a
failure, not a rejection (the store changed while it was read). A profile
reads only the metadata documents it names; it never reads a chunk object.
(**Structure only**, §1.2, applies: the output never depends on a chunk's
bytes, only on its key and size.)

**Object URLs.** The URL of the object with key `k` is `U` followed by
`k`'s UTF-8 bytes with every byte that is not an unreserved character
(`A`–`Z`, `a`–`z`, `0`–`9`, `-`, `.`, `_`, `~`), a sub-delim
(`!$&'()*+,;=`), `:`, `@` or `/` written as `%` and two uppercase hex
digits (the encoding of [SPEC.md §6](SPEC.md#6-source-table) for local paths). For example, the key
`a b/0.0` of the store `https://h/x/` has the URL `https://h/x/a%20b/0.0`.

**Source table and chunk references.** A profile names the **chunk
objects** of each array: objects whose keys are chunk keys of the array,
and the other objects it references whole (§11.6, the only one that does).
Of these, an object of size 0 is not used: it has no entry (so the chunk
reads as the fill value) and no source. Every other chunk object `k` gives
one entry, whose key is `k` itself, referencing the whole object. The
source table is one `url` source per such entry, without pins: the URL of
its object. The sources are in ascending order of their entries' keys,
compared as UTF-8 byte strings (which is the order of Unicode code points),
and the entry with key `k` at position `i` of that order has the single
range `(i, 0, size)`. No other entry is a reference, and a store input's
output has no data sources (§1.2).

These rules replace §1.2's **References stay in the file**: each range
lies within its object by construction, and its payload is far below 65519
bytes. An output key MUST be at most 65535 bytes in UTF-8 and MUST NOT
start with `__vz__/`; otherwise the input is rejected.

**Rejection and failure.** §1.2's rejection rule applies to every object a
profile reads and to the listing. A network error, an HTTP status of 408,
429 or 5xx, and an object that is not of its listed size are failures. §1.5
says which listing responses reject the input.

### 1.5 Listing a store

**Local directories.** A directory is listed by walking it: its objects are
its regular files, at any depth, with keys the relative paths joined by `/`
and sizes the file sizes. Symbolic links and files whose names are not
valid UTF-8 are not objects. `P` is empty.

**HTTP stores** are listed with the Amazon S3 `ListObjectsV2` operation,
which S3 and S3-compatible servers answer anonymously for public buckets.
The listing endpoint and the key prefix `P` come from `U`:

- Let `host` be `U`'s host in lower case. `U` is **virtual-hosted** if
  `host` ends in `.amazonaws.com` and, of its labels (the parts between
  `.`), one that is not the first is `s3` or starts with `s3-`. For example
  `janelia-cosem-datasets.s3.amazonaws.com` and
  `bucket.s3.us-east-1.amazonaws.com` are virtual-hosted, while
  `s3.amazonaws.com`, `s3.us-east-1.amazonaws.com` and every other host
  (such as `127.0.0.1` or `storage.googleapis.com`) are **path-style**.
  (A bucket name with a label `s3` or `s3-…` in a path-style URL is
  therefore read as virtual-hosted; buckets so named are not supported.)
- Virtual-hosted: the **endpoint** is `U`'s scheme and authority followed
  by `/`, and the **prefix path** is `U`'s path without its first `/`.
- Path-style: `U`'s path is `/b/r`, where the **bucket** `b` is a nonempty
  segment and `r` is the rest (possibly empty). A path of `/` alone rejects
  the input. The endpoint is `U`'s scheme and authority followed by `/b/`,
  and the prefix path is `r`.
- `P` is the prefix path percent-decoded to bytes, which MUST be valid
  UTF-8 (otherwise the input is rejected). It ends in `/` unless it is
  empty.

For example, `https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/em/`
has the endpoint `https://janelia-cosem-datasets.s3.amazonaws.com/` and
`P = jrc_hela-2/jrc_hela-2.n5/em/`; the same store as
`https://s3.amazonaws.com/janelia-cosem-datasets/jrc_hela-2/jrc_hela-2.n5/em/`
has the endpoint `https://s3.amazonaws.com/janelia-cosem-datasets/`.

**Requests.** The first request is a `GET` of the endpoint followed by
`?list-type=2&prefix=` and `enc(P)`; each later one appends
`&continuation-token=` and `enc(T)`, where `T` is the previous response's
continuation token. `enc` writes the UTF-8 bytes of its argument with every
byte that is not an unreserved character written as `%` and two uppercase
hex digits. Redirects are followed. A response is then:

- status 200: its body is parsed as below; a body that is not a listing
  rejects the input;
- status 408 or 429, any 5xx status, or no response: a failure;
- any other status: the input is rejected. (A plain web server, which has
  no listing operation, answers with 404 or 403, or with 200 and an HTML
  page, and its store inputs are therefore rejected.)

**Listing bodies.** A body MUST be UTF-8 (a byte sequence that is not
UTF-8 rejects the input; a byte order mark is not allowed) in the following
subset of XML 1.0, where `S` is whitespace (§1.3):

- The document is optional `S`, an optional XML declaration (`<?xml`
  followed by `S` and then any text up to the first `?>`), then any mix of
  `S` and comments, one element, and any mix of `S` and comments. A comment
  is `<!--`, then any text not containing `--`, then `-->`.
- An element is `<N`, attributes, optional `S`, then either `/>` (empty) or
  `>`, its content and `</N` with the same name, optional `S` and `>`. A
  name `N` is a letter (`A`–`Z`, `a`–`z`), `_` or `:`, followed by letters,
  ASCII digits, `_`, `:`, `.` and `-`.
- An attribute is `S`, a name, optional `S`, `=`, optional `S`, and a value
  in `"` or `'` quotes holding character data and references but not `<` or
  its quote. Attributes are not otherwise read (namespaces are not
  processed; names compare as written).
- Content is any sequence of character data (text not containing `<` or
  `&`, nor the sequence `]]>`), references, comments and elements.
- A reference is `&lt;`, `&gt;`, `&amp;`, `&quot;`, `&apos;` (standing for
  `<`, `>`, `&`, `"`, `'`), or `&#` and decimal ASCII digits, or `&#x` and
  hex digits (either case), then `;`, standing for that code point.
- Every character, written or referenced, MUST be an XML `Char`: U+0009,
  U+000A, U+000D, U+0020–U+D7FF, U+E000–U+FFFD or U+10000–U+10FFFF.
- Nothing else is allowed: no CDATA sections, document type declarations
  or processing instructions other than the declaration. Line ends are
  not normalized.

An element's **text** is its character data and references, in order,
with references replaced by what they stand for; it is only taken of an
element whose content has no element. The body is a **listing** when:

- the element is named `ListBucketResult`;
- of its child elements, exactly one is named `IsTruncated`, whose text is
  `true` or `false`; at most one is named `NextContinuationToken`, and if
  `IsTruncated` is `true` there is one, with nonempty text, which is the
  continuation token `T`; every child named `Contents` has exactly one
  child `Key` and exactly one child `Size`, neither of which has a child
  element; other children, at any depth, are not read;
- each `Contents`' `Size` text is one or more ASCII digits, of value at
  most 2^53 − 1, and its `Key` text starts with `P`.

Each `Contents` lists an object: its key is the `Key` text and its size the
`Size` value. The listing is complete after a response whose `IsTruncated`
is `false`. A key listed twice, in one response or two, rejects the input.
(The order of the listed keys is not checked: the output's order comes from
§1.4.)

### 1.6 JSON documents

The metadata documents of a store (N5's `attributes.json`, Zarr's
`.zarray`, `.zgroup` and `.zattrs`) are read as JSON, with these rules, in
both implementations:

- The object MUST be at most 16777216 (2^24) bytes. Its bytes MUST be UTF-8
  and MUST NOT start with a byte order mark (`EF BB BF`).
- The text MUST be one JSON value as RFC 8259 defines it (whitespace is
  space, tab, LF and CR). There are no other literals: `NaN`, `Infinity`,
  comments and trailing commas reject the input. Arrays and objects MUST
  NOT nest more than 256 deep (a top-level object is depth 1).
- Every number is converted to binary64 by correct rounding (§1.3), and a
  number whose value is then infinite rejects the input. A value that this
  document calls an **integer** is a number whose binary64 value is
  integral and at most 2^53 − 1 in magnitude (so `64`, `64.0` and `6.4e1`
  are the same integer); a larger magnitude is not an integer.
- When an object has a member name more than once, the last member with
  that name is used, and the others are ignored.
- A string escape for a lone surrogate (such as `"\ud800"`) is kept as
  that code unit.

A JSON value that this document copies into the output (attributes) is
copied as these rules read it: its numbers as binary64 values (§1.1 compares
them as such).

## 2. The Zarr layout

A virtualizer's output presents its input as a Zarr hierarchy whose layout
is specified by the input format's **convention**, in
[conventions/](conventions/README.md): the groups and arrays and their
metadata, the OME-NGFF metadata, and the attributes under the key
`vzip_virtualized`, including the translation of the input's header. The
profiles (§3–§11) specify how to read and check the input, and how each
chunk of that layout becomes a reference to the input's bytes; for the
layout itself they refer to the convention, which builds on what all
conventions share ([conventions/README.md](conventions/README.md): §1 the
conventions, §2 their attributes, §3 arrays, §4 images, §5 units, §6 source
metadata as JSON).

A virtualizer MUST produce exactly the layout the input format's convention
specifies for the input, at the convention's version that the profile
states, with:

- `source.url` (conventions §2) the input URL `U` as given (§1.2): for a
  file input the URL of source 0, for a store input the store URL (§1.4);
- every chunk entry the convention says is present, with the reference the
  profile specifies, and no other chunk entry;
- the documents as JSON (`zarr.json`, compared by §1.1), and no other
  entries than those the profile names.

## 3–11. Profiles

Each profile is a separate document:

- §3, the TIFF profile: [profiles/tiff.md](profiles/tiff.md);
- §4, the NDPI profile: [profiles/ndpi.md](profiles/ndpi.md);
- §5, the ND2 profile: [profiles/nd2.md](profiles/nd2.md);
- §6, the DICOM profile: [profiles/dicom.md](profiles/dicom.md);
- §7, the NIfTI profile: [profiles/nifti.md](profiles/nifti.md);
- §8, the IMS profile: [profiles/ims.md](profiles/ims.md);
- §9, the N5 profile: [profiles/n5.md](profiles/n5.md);
- §10, the Zarr v2 profile: [profiles/zarr2.md](profiles/zarr2.md);
- §11, the OME-Zarr profile: [profiles/ome-zarr.md](profiles/ome-zarr.md).

## 12. Conformance

This section is informative. There are two maintained implementations:
- the Python reference, `python -m vzip.virtualize <url> <out.vzip>`
  (`src/vzip/virtualize/`);
- the browser one, `web/src/virtualize/`, run under Node by
  `web/conformance/virtualize.ts`.

Both are organized by profile: `tiff/`, `ndpi/`, `nd2/`, `dicom/`, `nifti/`,
`ims/`, `n5/`, `zarr2/` and `ome_zarr/` (`ome-zarr/` in the browser one),
next to the parts they share (`common`, and `store` for store inputs: the
listing, the object reader and the JSON reader of §1.4–§1.6). The OME-Zarr
profile reuses the Zarr v2 profile's hierarchy reader.

**Resource limit.** The browser implementation fails (it does not reject)
a store whose listing has more than 100000 objects (counting every listed
`Contents`, ignored or not). Each chunk becomes a source and an entry, about
250 bytes of archive held in memory, and the listing takes one request per
1000 keys, so the limit bounds the archive at about 25 MB and the listing at
100 sequential requests. The Python reference has no limit; its run on a
470000-chunk OpenOrganelle level is recorded in
`conformance/virtualize/REVISIONS.md` (revision 12).

Implementations written from this document alone, round by round, are in
`impls/virtualize/`; `conformance/virtualize/REVISIONS.md` records what each
round found and how this document changed.

`conformance/virtualize/compare.py` runs implementations on a corpus and
compares their outputs by §1.1 (`conformance/virtualize/HARNESS.md`
describes the command an implementation provides). The corpus has:
- the synthetic files in `web/test/fixtures/<profile>/`, including inputs
  each profile rejects, and the synthetic stores in
  `web/test/fixtures/n5/`, `web/test/fixtures/zarr2/` and
  `web/test/fixtures/ome-zarr/` (one directory each);
- the 205 OME-TIFFs of IDR idr0096;
- public files and stores of every profile, listed in
  `conformance/virtualize/corpus_*.txt`: TIFF, SVS and NDPI
  (`corpus_tiff.txt`), ND2, DICOM (including whole-slide levels from the NCI
  Imaging Data Commons), NIfTI, IMS, N5 and Zarr v2 stores from
  OpenOrganelle (`corpus_n5.txt`, `corpus_zarr2.txt`), and OME-Zarr 0.4
  images, label images and plates from the IDR (`corpus_ome_zarr.txt`).

The harness's proxy (`conformance/virtualize/proxy.py`) serves the synthetic
stores, and forwards remote ones, with the listing operation of §1.5.

Pixel correctness is checked separately, against independent readers, by
`web/test/<profile>/verify.py`: TIFF and NDPI against tifffile, ND2 against
the synthetic files' known pixels (and `experiments/verify_nd2_vzip.py`
against the `nd2` package on public files), DICOM against pydicom, NIfTI
against nibabel, IMS against h5py, N5 against an independent block reader in
the script (and `zarr-n5`), Zarr v2 against zarr-python's own Zarr v2
reader, and OME-Zarr against zarr-python's Zarr v2 reader and the
`ome-zarr-models` package (every output group validated as OME-Zarr 0.5,
every accepted input as 0.4).

The conventions of [conventions §2](conventions/README.md#2-attributes) are checked by `tests/test_virtualize_conventions.py`:
it runs every synthetic file and store through both implementations,
validates every node that declares a convention against that convention's
JSON Schema (`conventions/<p>/schema.json`, written by
`conventions/generate_schemas.py`), and checks that every node with source
metadata declares it. `just tag-conventions` creates the
`virtualize-<p>-v<N>` tags that the convention URLs name, on `main`.

The DICOM, NIfTI and IMS profiles (revision 11), the N5 and Zarr v2
profiles (revision 12), the OME-Zarr profile (revision 13) and the data
sources of the TIFF and NDPI profiles (revision 14) have not yet been
through an independent implementation round; the round implementations in
`impls/virtualize/` still write JPEG headers as literals and ranges of the
file, and predate the conventions (revisions 15 and 16).
