# Virtualizing Zarr v2 hierarchies

The Zarr v2 profile of [spec/virtualize.md](../../virtualize.md) (revision 16),
numbered as its §10. §1 is in spec/virtualize.md and applies here, in particular
the store input rules of §1.4–§1.6, and so does §2.

Profile version: 1 · Convention: [spec/virtualize/zarr2.md](../zarr2.md),
version 1. The output has the convention's layout for the input
([spec/virtualize.md §2](../../virtualize.md#2-the-zarr-layout)); this profile says
how the store is read, which inputs are rejected, and how each chunk
references the store. "The convention §n" below is a section of
spec/virtualize/zarr2.md.

## 10. Zarr v2 profile

The profile was chosen because the store's root has a `.zarray` or a
`.zgroup` and the store does not declare OME-NGFF 0.4 (§1.4); a store that
does is read by the OME-Zarr profile (§11, [ome-zarr.md](../ome-zarr/profile.md)),
which builds on this one. The store is listed (§1.5), and the documents
that [the convention §2](../zarr2.md#2-nodes) reads are
read as JSON (§1.6).

**Consolidated metadata.** A `.zmetadata` object (consolidated metadata) is
never read: nodes come from the listing and documents from their own
objects, so stale consolidated metadata cannot change the output. (Chunks
must come from the listing anyway, since `.zmetadata` does not list them.)

**Numbers.** The documents' integer literals are kept exactly where the
convention copies them ([its §4](../zarr2.md#4-source-metadata)),
beyond 2^53 − 1 too, in place of §1.6's last paragraph; every number the
convention uses is its binary64 value, as §1.6 says.

### 10.1 Rejection

The input is rejected when a rule of §1.4–§1.6 fails, and when the
convention gives it no layout: wherever it says that the input is
rejected, or that something MUST hold and it does not.

### 10.2 Chunks and other objects

Every chunk that the convention says is present
([the convention §3.2](../zarr2.md#32-chunks)) is an
entry under its own key, referencing the whole chunk object through its own
`url` source (§1.4). A chunk object of size 0 has no entry: its key is
one of the convention §5's empty objects' keys.

Every other object of [the convention §5](../zarr2.md#5-other-objects)
is referenced whole in the same way, by an entry under the key
`vzip_source/objects/<k>` (with a `~` appended when the convention §5
escapes `k`'s last segment) whose `url` source is the URL of the object `k`
(§1.4). (This is the one place where an entry's key is not its object's
key; §1.4's rules otherwise apply: the sources are in ascending order of
their entries' keys, each entry referencing its object's whole range.)
Under a root array, the empty objects' keys and the ignored keys are the
document `vzip_source/empty.json` (the convention §5), written as UTF-8
compact JSON, as `JSON.stringify` writes it. The documents under
`vzip_source/` (which hold these keys) are deflated and, like the chunks, not
among the documents a reader fetches when it opens the archive
([conventions §2](../../conventions.md#2-attributes)).

### 10.3 Summary

This section is informative. Both implementations print a one-line JSON
summary: `groups` (explicit and implicit), `arrays`, `chunks` (chunk
entries), `emptyChunks` (chunk objects of size 0), `objects` (the store's
objects), `otherObjects` (the entries under `vzip_source/objects/`) and
`listingRequests`.
