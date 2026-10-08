# vzip's Zarr conventions

A Zarr hierarchy made from a source in another format (a TIFF, an ND2 file, an
N5 store, ...) looks like any other Zarr hierarchy. These conventions make
it describe itself: which source it presents, which format that source is
in, and everything the source's own header or metadata says. There is one
convention per source format, each specified in `<format>/README.md` next to
its JSON Schema `<format>/schema.json`:

| format | convention | UUID |
|---|---|---|
| TIFF, OME-TIFF, Aperio SVS | [tiff](tiff/README.md) | `48e9ac4e-1156-4a62-955e-20467d9c2700` |
| Hamamatsu NDPI | [ndpi](ndpi/README.md) | `6cac71ef-dbb2-4acd-b60c-00389aa4238a` |
| Nikon ND2 | [nd2](nd2/README.md) | `59612f14-e314-4207-ba00-8f422ba71490` |
| DICOM | [dicom](dicom/README.md) | `acf17198-e5a5-48d3-8187-22ec4bb40ea5` |
| NIfTI-1 and NIfTI-2 | [nifti](nifti/README.md) | `06e5809d-4d54-4b72-afd0-6bf61a7b4c85` |
| Imaris IMS | [ims](ims/README.md) | `5067a535-8261-4b25-a93c-1985ed333bde` |
| N5 | [n5](n5/README.md) | `ad5d4c39-c69e-48f7-a3ef-4cc8c607d416` |
| Zarr v2 | [zarr2](zarr2/README.md) | `8e792619-d671-4687-ab51-752885dd3ee6` |
| OME-Zarr 0.4 | [ome-zarr](ome-zarr/README.md) | `b74ea302-65bb-49ae-b81f-f9bb52cd4eed` |

A format's convention specifies the hierarchy's whole **Zarr layout**: which
groups and arrays it has and where, each array's metadata (shape, data
type, chunk grid, codecs, dimension names), the OME-NGFF metadata, and the
attributes under the convention's key, including the translation of the
source's header into JSON. It says what each chunk holds in terms of the
source (for example, "the chunk at `0/c/t/0/z/0/0` is frame `f`"), but not
how a store produces those bytes. vzip produces these hierarchies as
virtual stores whose chunks are byte ranges of the source; that is
specified separately, in [VIRTUALIZE.md](../VIRTUALIZE.md) and its
profiles, which refer to these conventions for the layout.

This document holds what every convention shares. The key words MUST, MUST
NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in RFC
2119. Numbers follow [VIRTUALIZE.md §1.3](../VIRTUALIZE.md#13-arithmetic)
(IEEE 754 binary64, correctly rounded).

## 1. Conventions

Each convention follows the [Zarr conventions
framework](https://github.com/zarr-conventions/zarr-conventions-spec).

**Versions.** Each convention has a **version**, a positive integer stated
in its document's header. It increases by 1 whenever the layout it
specifies, or the virtual store that vzip's profile for that format
specifies, changes for some source, including whether the source is
accepted; an editorial change, or a change to another format, leaves it as
it is. Version `N` of the convention for format `p` is tagged
`virtualize-<p>-v<N>` in the vzip repository.

**The convention metadata object.** Format `p` at version `N` is declared by
the Convention Metadata Object (CMO)

```json
{
  "uuid": "<the UUID above>",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/virtualize-<p>-v<N>/conventions/<p>/schema.json",
  "spec_url": "https://github.com/d-v-b/vzip/blob/virtualize-<p>-v<N>/conventions/<p>/README.md",
  "name": "vzip_virtualized",
  "description": "The Zarr layout of a <T> source virtualized by vzip, and the source's metadata"
}
```

with no other members, where `T` is the format's title (`TIFF`, `NDPI`,
`ND2`, `DICOM`, `NIfTI`, `IMS`, `N5`, `Zarr v2`, `OME-Zarr`). The UUID is
the same for every version; the version is in the URLs. All nine
conventions nest their property under one key, `vzip_virtualized`, which is
also their `name`: the framework asks a nesting convention's `name` to be
its key, and a key without a colon is not mistaken for a namespace prefix.

## 2. Attributes

**Which nodes.** The root node of the hierarchy (the document `zarr.json`)
always declares its format's convention. Any other node declares it when,
and only when, the node has source metadata (below). A node that declares
it has, in its attributes, the member `zarr_conventions`, whose value is
`[C]` with `C` the format's CMO, and the member `vzip_virtualized`.

**The property.** On the root, `vzip_virtualized` is the object

```json
{"profile": "<p>", "version": N, "source": {"url": "<U>"}, "<p>": S}
```

and on any other node `{"<p>": S}`, where:

- `p` and `N` are the format and its convention's version;
- `U` is the URL of the source: of the file for a file, and of the store,
  ending in `/`, for a store;
- `S` is the node's **source metadata**, an object whose content the
  format's convention specifies (its "Source metadata" section): what the
  source's header or metadata holds for that node, translated by §6. The
  member `"<p>"` is present only when `S` has at least one member.

There are no other members. The property holds no time and nothing about
the program that made the hierarchy, so that two conforming producers make
equivalent hierarchies from the same source.

**Nothing else.** Outside `vzip_virtualized`, a node's attributes hold only
members that the hierarchy's target formats define: `ome` (OME-NGFF 0.5,
§4), where the convention places it, and `zarr_conventions`. Everything
taken from the source goes under the key, on the node it belongs to,
including attributes a source store had (and the conventions they
declare), so it can neither collide with nor masquerade as the target
formats' metadata. A source that is itself such a hierarchy therefore
nests its own record.

## 3. Arrays

Every array's `zarr.json` is the JSON object:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [...],
  "data_type": "...",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [...]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [...],
  "dimension_names": [...],
  "attributes": {}
}
```

with no other members. Chunk keys are `<array path>/c/<i0>/<i1>/...`. A chunk
whose data the file does not contain has no entry (it reads as the fill
value).

**Axes.** An image's axes are a subsequence of `t, c, z, y, x`, in that
order: `t` (time), `c` (channel), `z`, `y`, `x` (space). `y` and `x` are
always present; the format's convention says when the others are.

**Codecs.** `codecs` is built from these, in this order:

1. `{"name": "transpose", "configuration": {"order": [...]}}` when a chunk's
   bytes hold the channel axis last ("interleaved"). `order` lists the array's
   axis indices in stored order: every axis except `c` in array order, then
   `c`.
2. The array-to-bytes codec, one of:
   - `{"name": "bytes"}` (no `configuration`) for 1-byte data types;
   - `{"name": "bytes", "configuration": {"endian": "little"}}` or
     `"big"` for larger ones;
   - `{"name": "imagecodecs_jpeg2k"}` for JPEG 2000 (one codestream per
     chunk, decoding to the chunk's `[y, x]`, or, when interleaved, to
     `[y, x, c]`, after which `transpose` applies). Decoding follows OpenJPEG,
     as imagecodecs does: a 3-component codestream whose first component is
     at full resolution and whose other two are subsampled holds YCbCr, and
     decodes to RGB (as in Aperio's compression 33003);
   - `{"name": "imagecodecs_jpeg"}` for JPEG (one complete JPEG stream, ISO/IEC
     10918-1 with the JFIF/Adobe color conventions, per chunk, decoding to
     the chunk's `[y, x]` or `[y, x, c]` like JPEG 2000).
3. A compressor, when the bytes are compressed:
   - `{"name": "zlib", "configuration": {"level": 1}}` (zlib streams);
   - `{"name": "zstd", "configuration": {"level": 0, "checksum": false}}`.

## 4. Images

An image is a group whose `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": {"ome": M}}`, where `M`
is the OME-NGFF 0.5 object below. The image's other
attributes are those of §2 (the convention's declaration and property), and
no other member is allowed. `M` is:

```json
{
  "version": "0.5",
  "multiscales": [{
    "name": "...",
    "axes": [{"name": "t", "type": "time", "unit": "second"}, ...],
    "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [...]}]}, ...]
  }]
}
```

- `name` is present only when the format's convention gives one.
- Each axis has `name` and `type` (`"time"`, `"channel"` or `"space"`), and
  `unit` only when the format's convention gives one.
- `datasets` lists the pyramid levels, full resolution first, with paths
  `"0"`, `"1"`, ...; each has a `scale` transformation, one number per axis,
  followed by a `{"type": "translation", "translation": [...]}`
  transformation (one number per axis) only where the format's convention gives one.
- `M` has an `"omero"` member, next to `multiscales`, only where the format's
  convention says so.

## 5. Units

OME-XML unit symbols map to OME-NGFF units:

| symbol | unit |
|---|---|
| `µm` (U+00B5), `μm` (U+03BC), `um` | `micrometer` |
| `nm` | `nanometer` |
| `mm` | `millimeter` |
| `cm` | `centimeter` |
| `m` | `meter` |
| `Å` (U+00C5), `Å` (U+212B) | `angstrom` |
| `pm` | `picometer` |
| `in` | `inch` |
| `ft` | `foot` |
| `s` | `second` |
| `ms` | `millisecond` |
| `min` | `minute` |
| `h` | `hour` |

Any other symbol gives no unit (the scale is still used).

A **length** in one of these units converts to another by the units'
sizes in metres: micrometer 1e-6, nanometer 1e-9, millimeter 1e-3,
centimeter 1e-2, meter 1, angstrom 1e-10, picometer 1e-12, inch 0.0254,
foot 0.3048. A value `v` in unit `a` is `v × (size(a) / size(b))` in unit
`b` (the division first).

**Translations.** Where a convention places an image in space (§4), the
translation applies to every level, and is 0 for every axis it does not
name. A position given for the centre of an image of level-0 size
`W0 × H0` with x and y scales `sx`, `sy` becomes the translation
`x = cx − W0 × sx / 2`, `y = cy − H0 × sy / 2` (products first).

## 6. Source metadata as JSON

A convention's "Source metadata" section lists, for each node, the source's
fields and how each becomes a member of `S`. Unless it says otherwise:

- **Names.** A field's member is named as the format's own specification
  names the field, and members appear in the order the section lists them.
  A field the source does not have is absent (not `null`).
- **Integers** become JSON numbers when their magnitude is at most
  2^53 − 1, and otherwise the string of their decimal digits (with a
  leading `-` if negative).
- **Floating-point numbers** become JSON numbers, the source's value
  converted to binary64 exactly (a binary32 value included). NaN, +∞ and
  −∞ become the strings `"NaN"`, `"Infinity"` and `"-Infinity"`, as Zarr
  fill values do.
- **Text** fields become JSON strings: for a fixed-size character field,
  its bytes up to the first NUL (all of them if there is none); decoded as
  UTF-8 when they are valid UTF-8, and otherwise as ISO 8859-1 (each byte
  one character), so that every byte string has exactly one translation.
- **Arrays** of values become JSON arrays, in order.
- **Opaque bytes** (binary fields and payloads a convention does not
  decode) become the string of their base64 encoding (RFC 4648 §4, with
  padding).

Producers compare these as JSON values: numbers by their binary64 values,
strings by their characters (VIRTUALIZE.md §1.1).
